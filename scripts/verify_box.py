#!/usr/bin/env python3
"""Visual verification of pbr2rad box/triplanar projection.

Generates a synthetic checkerboard texture, converts it with pbr2rad using
box projection, builds a simple Radiance scene with a unit cube, renders
with rpict, and converts to BMP for easy viewing.

Requirements:
  - pbr2rad installed (pip install -e .)
  - Radiance 6.x on PATH (rpict, oconv, genbox, ra_bmp)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


def _check_radiance() -> bool:
    """Return True if Radiance tools are on PATH."""
    for cmd in ("oconv", "rpict", "genbox", "ra_bmp"):
        if shutil.which(cmd) is None:
            print(f"error: {cmd!r} not found on PATH — install Radiance 6.x", file=sys.stderr)
            return False
    return True


def _make_checkerboard(path: Path, size: int = 256, squares: int = 8) -> None:
    """Write a red/white checkerboard PNG — projection errors are obvious."""
    img = Image.new("RGB", (size, size))
    sq = size // squares
    for y in range(size):
        for x in range(size):
            if ((x // sq) + (y // sq)) % 2 == 0:
                img.putpixel((x, y), (220, 40, 40))   # red
            else:
                img.putpixel((x, y), (240, 240, 240))  # near-white
    img.save(path)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def main() -> int:
    if not _check_radiance():
        return 1

    work = Path(tempfile.mkdtemp(prefix="verify_box_"))
    print(f"working directory: {work}")

    # --- 1. Synthetic PBR set (checkerboard albedo only) ---
    pbr_dir = work / "checker"
    pbr_dir.mkdir()
    checker_png = pbr_dir / "checker_diff_2k.png"
    _make_checkerboard(checker_png)
    print(f"created checkerboard: {checker_png}")

    # --- 2. Convert with pbr2rad (box projection) ---
    rad_lib = work / "radlib"
    _run([
        sys.executable, "-m", "pbr2rad",
        str(pbr_dir), "-o", str(rad_lib),
        "--projection", "box",
        "--u-scale", "1", "--v-scale", "1",
        "--roughness", "0.15",
    ])

    mat_dir = rad_lib / "checker"
    rad_file = mat_dir / "checker.rad"
    cal_file = mat_dir / "checker.cal"
    hdr_file = mat_dir / "checker.hdr"
    for f in (rad_file, cal_file, hdr_file):
        if not f.exists():
            print(f"error: expected {f} not found", file=sys.stderr)
            return 1
    print("pbr2rad output OK")

    # --- 3. Build scene: cube + sky + view ---
    # Generate a box with the material applied.
    # genbox produces a box; we'll use xform to apply the material.
    scene_rad = work / "scene.rad"

    # Create a box primitive using genbox with our material
    genbox_result = subprocess.run(
        ["genbox", "checker", "cube", "1", "1", "1"],
        capture_output=True, text=True, check=True,
    )

    # Scene: sky light + the textured box
    scene_text = (
        "# Sky dome for illumination\n"
        "void light solar\n"
        "0\n"
        "0\n"
        "3 3 3 3\n"
        "\n"
        "solar source sun\n"
        "0\n"
        "0\n"
        "4 -0.5 0.7 1 0.5\n"
        "\n"
        "void light sky_glow\n"
        "0\n"
        "0\n"
        "3 0.8 0.9 1.0\n"
        "\n"
        "sky_glow source sky\n"
        "0\n"
        "0\n"
        "4 0 0 1 180\n"
        "\n"
        "# Ground plane for context\n"
        "void plastic grey_ground\n"
        "0\n"
        "0\n"
        "5 0.3 0.3 0.3 0 0\n"
        "\n"
        "grey_ground polygon floor\n"
        "0\n"
        "0\n"
        "12\n"
        "  -5 -5 0\n"
        "   5 -5 0\n"
        "   5  5 0\n"
        "  -5  5 0\n"
        "\n"
    )

    # The genbox output references "checker" as the material modifier,
    # which comes from our .rad file's material definition.
    scene_text += genbox_result.stdout

    scene_rad.write_text(scene_text, encoding="ascii")
    print("scene written")

    # --- 4. Compile octree ---
    octree = work / "scene.oct"
    _run(
        ["oconv", str(rad_file), str(scene_rad)],
        stdout=open(octree, "wb"),
        env={**__import__("os").environ, "RAYPATH": f"{mat_dir}:.:/usr/local/radiance/lib"},
    )
    print("octree compiled")

    # --- 5. Render with rpict ---
    render_hdr = work / "render.hdr"
    # View looking at the corner so 3 faces are visible
    _run(
        [
            "rpict",
            "-vp", "2.5", "2.5", "2.0",     # eye position
            "-vd", "-1", "-1", "-0.5",        # view direction
            "-vu", "0", "0", "1",             # up vector
            "-vh", "60", "-vv", "45",         # horizontal/vertical FOV
            "-x", "800", "-y", "600",         # resolution
            "-ab", "2",                       # ambient bounces
            "-aa", "0.1",                     # ambient accuracy
            "-ad", "512",                     # ambient divisions
            str(octree),
        ],
        stdout=open(render_hdr, "wb"),
        env={**__import__("os").environ, "RAYPATH": f"{mat_dir}:.:/usr/local/radiance/lib"},
    )
    print(f"rendered: {render_hdr}")

    # --- 6. Convert to BMP for easy viewing ---
    render_bmp = work / "render.bmp"
    _run(["ra_bmp", str(render_hdr), str(render_bmp)])
    print(f"BMP output: {render_bmp}")

    # Copy to repo for easy access
    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    final_bmp = out_dir / "verify_box.bmp"
    final_hdr = out_dir / "verify_box.hdr"
    shutil.copy2(render_bmp, final_bmp)
    shutil.copy2(render_hdr, final_hdr)
    print(f"\nFinal output copied to:")
    print(f"  {final_bmp}")
    print(f"  {final_hdr}")
    print(f"\nOpen {final_bmp} to inspect the box projection.")
    print("Each visible cube face should show a coherent checkerboard pattern")
    print("aligned to that face's coordinate plane.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
