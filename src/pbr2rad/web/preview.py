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

    # Scene: shader-ball sphere with three-point studio lighting + sky dome.
    # The dome (cool, broad) acts as a fake HDRI environment for ambient bounce;
    # key/fill/rim sources give crisp shading and edge definition.
    scene_rad = work / "preview_scene.rad"
    scene_rad.write_text(
        # Material on a flat 2x2 plane laid on the XY axis at z=0.
        # Matches Polyhaven's primary preview format and avoids spherical-UV
        # moire artifacts you get when projecting tile patterns onto a sphere.
        f"{name} polygon plate\n0\n0\n12\n"
        "  -1 -1 0\n   1 -1 0\n   1  1 0\n  -1  1 0\n\n"

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
                    # Slightly elevated front view of the tilted plane,
                    # roughly matching Polyhaven's preview camera angle.
                    "-vp", "0", "-1.6", "1.1",
                    "-vd", "0", "0.85", "-0.55",
                    "-vu", "0", "0", "1",
                    "-vh", "35", "-vv", "35",
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
            ["pfilt", "-1", "-e", "+2.2", str(hdr)],
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
