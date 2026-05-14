#!/usr/bin/env python3
"""Render an interior scene with a staircase using a pbr2rad material library.

The staircase (treads, risers, side panels) gets your material applied so you
can see how it behaves across multiple surface orientations. The room itself
uses neutral plastic so the focus stays on the material under test.

Usage:
    python scripts/verify_interior.py <material_dir> [--out PATH] [--res WxH] [--ab N]

The material modifier name is auto-detected from the .rad file in <material_dir>.

Requires Radiance on PATH (oconv, rpict, ra_bmp).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def detect_material_name(rad_file: Path) -> str:
    text = rad_file.read_text()
    matches = re.findall(r"^\S+\s+(plastic|metal)\s+(\S+)\s*$", text, re.M)
    if not matches:
        raise ValueError(f"could not find plastic/metal material in {rad_file}")
    return matches[-1][1]


def check_radiance() -> bool:
    for cmd in ("oconv", "rpict", "ra_bmp"):
        if shutil.which(cmd) is None:
            print(f"error: {cmd!r} not found on PATH", file=sys.stderr)
            return False
    return True


def staircase(material: str,
              steps: int = 6,
              rise: float = 0.17,
              tread: float = 0.28,
              width: float = 1.6,
              base_x: float = -0.8,
              base_y: float = 0.5) -> str:
    """Generate Radiance polygons for a staircase ascending in +y."""
    lines = [f"# Staircase ({steps} steps) — material: {material}\n"]

    def poly(name: str, verts):
        s = f"{material} polygon {name}\n0\n0\n{len(verts) * 3}\n"
        for x, y, z in verts:
            s += f"  {x:.4f} {y:.4f} {z:.4f}\n"
        return s + "\n"

    x_l, x_r = base_x, base_x + width
    for i in range(steps):
        z_b, z_t = i * rise, (i + 1) * rise
        y_f, y_b = base_y + i * tread, base_y + (i + 1) * tread

        # Tread top (normal +z, CCW from above)
        lines.append(poly(f"step{i}_tread", [
            (x_l, y_f, z_t),
            (x_r, y_f, z_t),
            (x_r, y_b, z_t),
            (x_l, y_b, z_t),
        ]))

        # Riser front (normal -y, CCW seen from -y)
        lines.append(poly(f"step{i}_riser", [
            (x_l, y_f, z_b),
            (x_l, y_f, z_t),
            (x_r, y_f, z_t),
            (x_r, y_f, z_b),
        ]))

    # Side panels: stair-step profile, one big polygon each side
    def side_profile(x):
        verts = [(x, base_y, 0.0)]
        for i in range(steps):
            verts.append((x, base_y + i * tread, (i + 1) * rise))
            verts.append((x, base_y + (i + 1) * tread, (i + 1) * rise))
        verts.append((x, base_y + steps * tread, 0.0))
        return verts

    # Left side (normal -x); winding when viewed from -x
    lines.append(poly("stair_left_side", side_profile(x_l)))
    # Right side (normal +x); reverse winding
    lines.append(poly("stair_right_side", list(reversed(side_profile(x_r)))))

    return "".join(lines)


ROOM = """
# --- Room shell ---
void plastic floor_mat
0
0
5 0.5 0.48 0.45 0 0

floor_mat polygon floor
0
0
12
  -3 -2 0
   3 -2 0
   3  6 0
  -3  6 0

void plastic ceiling_mat
0
0
5 0.88 0.88 0.86 0 0

ceiling_mat polygon ceiling
0
0
12
  -3 -2 3
  -3  6 3
   3  6 3
   3 -2 3

void plastic wall_warm
0
0
5 0.72 0.66 0.58 0 0

wall_warm polygon back_wall
0
0
12
  -3 6 0
   3 6 0
   3 6 3
  -3 6 3

wall_warm polygon left_wall
0
0
12
  -3 -2 0
  -3 -2 3
  -3  6 3
  -3  6 0

# Right wall has a tall window cut out (y in [1.5, 4.5], z in [0.6, 2.6])
wall_warm polygon right_wall_bot
0
0
12
   3 -2 0
   3  6 0
   3  6 0.6
   3 -2 0.6

wall_warm polygon right_wall_top
0
0
12
   3 -2 2.6
   3  6 2.6
   3  6 3
   3 -2 3

wall_warm polygon right_wall_front
0
0
12
   3 -2 0.6
   3  1.5 0.6
   3  1.5 2.6
   3 -2 2.6

wall_warm polygon right_wall_back
0
0
12
   3  4.5 0.6
   3  6 0.6
   3  6 2.6
   3  4.5 2.6

void plastic wall_dark
0
0
5 0.32 0.32 0.34 0 0

wall_dark polygon front_wall
0
0
12
  -3 -2 0
  -3 -2 3
   3 -2 3
   3 -2 0
"""

LIGHTING = """
# --- Lighting: sky dome + sun through window ---
void light sky_glow
0
0
3 2.2 2.5 3.0

sky_glow source sky
0
0
4 0 0 1 180

void light sun_emit
0
0
3 18 16 13

sun_emit source sun
0
0
4 1 0.2 0.6 0.5
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("material_dir", type=Path,
                   help="pbr2rad output folder (must contain <name>.rad)")
    p.add_argument("--out", type=Path,
                   default=Path(__file__).resolve().parent.parent / "output" / "verify_interior",
                   help="output basename (writes .hdr and .bmp)")
    p.add_argument("--res", default="900x600",
                   help="render resolution WxH (default 900x600)")
    p.add_argument("--ab", type=int, default=2, help="ambient bounces (default 2)")
    p.add_argument("--material-name",
                   help="override auto-detected material modifier name")
    args = p.parse_args()

    if not check_radiance():
        return 1

    mat_dir = args.material_dir.resolve()
    rad_files = sorted(mat_dir.glob("*.rad"))
    if not rad_files:
        print(f"error: no .rad file in {mat_dir}", file=sys.stderr)
        return 1
    rad_file = rad_files[0]
    mat_name = args.material_name or detect_material_name(rad_file)
    print(f"material: {mat_name}  ({rad_file})")

    try:
        w, h = (int(x) for x in args.res.lower().split("x"))
    except ValueError:
        print(f"error: invalid --res {args.res!r}", file=sys.stderr)
        return 1

    work = Path(tempfile.mkdtemp(prefix="verify_interior_"))
    print(f"working in {work}")

    scene_rad = work / "scene.rad"
    scene_rad.write_text(ROOM + LIGHTING + staircase(mat_name))

    octree = work / "scene.oct"
    env = {**os.environ, "RAYPATH": f"{mat_dir}:.:/usr/local/radiance/lib"}

    print("oconv …")
    with open(octree, "wb") as out:
        subprocess.run(["oconv", str(rad_file), str(scene_rad)],
                       check=True, stdout=out, env=env)

    render_hdr = work / "render.hdr"
    print(f"rpict {w}×{h}  ab={args.ab} …")
    with open(render_hdr, "wb") as out:
        subprocess.run([
            "rpict",
            "-vp", "2.4", "-1.2", "1.65",
            "-vd", "-0.65", "0.7", "-0.30",
            "-vu", "0", "0", "1",
            "-vh", "60", "-vv", "42",
            "-x", str(w), "-y", str(h),
            "-ab", str(args.ab), "-aa", "0.1", "-ad", "1024", "-as", "256",
            str(octree),
        ], check=True, stdout=out, env=env)

    render_bmp = work / "render.bmp"
    subprocess.run(["ra_bmp", str(render_hdr), str(render_bmp)], check=True)

    out_base = args.out.resolve()
    out_base.parent.mkdir(parents=True, exist_ok=True)
    final_hdr = out_base.with_suffix(".hdr")
    final_bmp = out_base.with_suffix(".bmp")
    shutil.copy2(render_hdr, final_hdr)
    shutil.copy2(render_bmp, final_bmp)
    print(f"\nwrote:\n  {final_hdr}\n  {final_bmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
