"""Optional Radiance render preview.

If ``rpict`` is on PATH, renders a low-res preview sphere with the
converted material.  Falls back gracefully if Radiance is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
import os
from pathlib import Path


def radiance_available() -> bool:
    """Return True if Radiance tools are on PATH."""
    return shutil.which("rpict") is not None and shutil.which("oconv") is not None


def render_preview(
    mat_dir: Path,
    rad_file: Path,
    output_png: Path,
    *,
    size: int = 512,
) -> bool:
    """Render a preview sphere with the given material.

    Returns True on success, False on failure.
    """
    if not radiance_available():
        return False

    work = output_png.parent
    name = rad_file.stem

    # Scene: sphere + lighting
    scene_rad = work / "preview_scene.rad"
    scene_rad.write_text(
        f"{name} sphere ball\n0\n0\n4 0 0 0.5 0.5\n\n"
        "void light solar\n0\n0\n3 3 2.8 2.2\n\n"
        "solar source sun\n0\n0\n4 -0.4 0.8 0.8 0.5\n\n"
        "void light fill\n0\n0\n3 1.0 1.0 1.0\n\n"
        "fill source fill_src\n0\n0\n4 0.8 -0.5 0.5 80\n\n"
        "void light sky_glow\n0\n0\n3 0.5 0.6 0.8\n\n"
        "sky_glow source sky\n0\n0\n4 0 0 1 180\n\n"
        "void plastic ground\n0\n0\n5 0.25 0.25 0.25 0 0\n\n"
        "ground polygon floor\n0\n0\n12\n"
        "  -2 -2 0\n   2 -2 0\n   2  2 0\n  -2  2 0\n",
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
                    "-vp", "0", "-1.8", "0.6",
                    "-vd", "0", "1", "-0.05",
                    "-vu", "0", "0", "1",
                    "-vh", "40", "-vv", "40",
                    "-x", str(size), "-y", str(size),
                    "-ab", "3",          # ambient bounces
                    "-aa", "0.05",       # ambient accuracy (tighter)
                    "-ad", "1024",       # ambient divisions (less noise)
                    "-as", "512",        # ambient super-samples
                    "-ps", "1",          # no pixel sub-sampling
                    str(octree),
                ],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=120, env=env,
            )

        # Convert to PNG via pfilt + ra_bmp + Pillow
        filtered = work / "preview_filt.hdr"
        bmp = work / "preview.bmp"
        subprocess.run(
            ["pfilt", "-1", "-e", "+1.5", str(hdr)],
            stdout=open(filtered, "wb"), stderr=subprocess.PIPE,
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

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception):
        return False
