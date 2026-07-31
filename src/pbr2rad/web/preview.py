"""Optional Radiance render preview.

If ``rpict`` is on PATH, renders a low-res preview sphere with the
converted material.  Falls back gracefully if Radiance is not installed.
"""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import os
from pathlib import Path

log = logging.getLogger("pbr2rad.web")


@functools.lru_cache(maxsize=1)
def radiance_available() -> bool:
    """Return True if Radiance tools are on PATH.

    Cached: the answer can't change within one process (the deployment bakes
    Radiance into the image), and this sits on the health-check hot path.
    """
    return shutil.which("rpict") is not None and shutil.which("oconv") is not None


def render_preview(
    mat_dir: Path,
    rad_file: Path,
    output_png: Path,
    *,
    size: int = 384,
) -> bool:
    """Render a preview sphere with the given material.

    Returns True on success, False on failure.
    """
    if not radiance_available():
        return False

    work = output_png.parent
    name = rad_file.stem

    # Scene: a unit sphere with the material under test, lit by three-point
    # studio lighting + sky dome. The sphere's continuously varying normal
    # reveals how the box/triplanar .cal switches between projection axes
    # (visible as three soft "seams" along the world-axis great circles).
    scene_rad = work / "preview_scene.rad"

    sphere_geom = f"{name} sphere ball\n0\n0\n4 0 0 0 1\n\n"

    scene_rad.write_text(
        sphere_geom +
        # Key light (warm, upper-front-left) - main shading source
        "void light key_l\n0\n0\n3 5.0 4.5 4.0\n\n"
        "key_l source key\n0\n0\n4 -0.6 -0.6 0.8 8\n\n"

        # Fill light (cool, lower-front-right) - softens shadows
        "void light fill_l\n0\n0\n3 1.0 1.2 1.5\n\n"
        "fill_l source fill\n0\n0\n4 0.7 -0.5 0.3 30\n\n"

        # Sky dome - hemispherical environment ambient (replaces HDRI)
        "void light sky_dome\n0\n0\n3 0.4 0.5 0.7\n\n"
        "sky_dome source sky\n0\n0\n4 0 0 1 180\n",
        encoding="ascii",
    )

    env = {
        **os.environ,
        "RAYPATH": f"{mat_dir}:.:{os.environ.get('RAYPATH', '/usr/local/radiance/lib')}",
    }

    octree = work / "preview.oct"
    hdr = work / "preview.hdr"

    try:
        # Compile
        with open(octree, "wb") as f:
            subprocess.run(
                ["oconv", str(rad_file), str(scene_rad)],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=30, env=env,
            )

        # Render
        with open(hdr, "wb") as f:
            subprocess.run(
                [
                    "rpict",
                    # 3/4 view aimed exactly at the origin. Camera distance
                    # and FOV tuned so the cube fills ~80% of the frame with
                    # all three visible faces clearly readable.
                    "-vp", "2.6", "-2.9", "2.1",
                    "-vd", "-0.584", "0.652", "-0.472",
                    "-vu", "0", "0", "1",
                    "-vh", "42", "-vv", "42",
                    "-x", str(size), "-y", str(size),
                    # Ambient settings tuned for a shared vCPU: good enough
                    # for a thumbnail, several times faster than the old
                    # -ab 3 / -ad 1024 / -as 512 settings which routinely
                    # approached the timeout on small hosts.
                    "-ab", "2",          # ambient bounces
                    "-aa", "0.1",        # ambient accuracy
                    "-ad", "512",        # ambient divisions
                    "-as", "256",        # ambient super-samples
                    "-ps", "1",          # no pixel sub-sampling
                    str(octree),
                ],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=60, env=env,
            )

        # Convert to PNG via pfilt + ra_bmp + Pillow
        filtered = work / "preview_filt.hdr"
        bmp = work / "preview.bmp"
        with open(filtered, "wb") as f:
            subprocess.run(
                ["pfilt", "-1", "-e", "+1.4", str(hdr)],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=30, env=env,
            )
        subprocess.run(
            ["ra_bmp", str(filtered), str(bmp)],
            stderr=subprocess.PIPE, check=True, timeout=30, env=env,
        )

        from PIL import Image
        img = Image.open(bmp)
        img.save(output_png)
        return True

    except subprocess.TimeoutExpired as exc:
        log.warning("preview render timed out (%s) for %s", exc.cmd[0], rad_file.name)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace").strip()
        log.warning(
            "preview render failed (%s, exit %d) for %s: %s",
            exc.cmd[0], exc.returncode, rad_file.name, stderr[-500:],
        )
        return False
    except Exception:
        log.exception("preview render failed for %s", rad_file.name)
        return False
