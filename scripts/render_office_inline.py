#!/usr/bin/env python3
"""Render the office's architectural shell with materials bound directly via
inline Radiance polygons (no obj2mesh).

Only the layers we care about (Floor, Wall_Brick, Ceiling, Columns, Mullion,
Glazing) are emitted. Furniture is skipped — keeps the scene small enough to
render fast and avoids the obj2mesh path that was eating the materials.

Usage:
    python scripts/render_office_inline.py <material_dir> [--target GROUP ...]

The material modifier name is auto-detected from the .rad file.
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


SHELL_GROUPS = {
    "1_ARCHITECTURE Floor",
    "1_ARCHITECTURE Ceiling",
    "1_ARCHITECTURE Wall_Brick",
    "1_ARCHITECTURE Columns",
    "1_ARCHITECTURE Mullion",
    "1_ARCHITECTURE Glazing",
}


def detect_material_name(rad_file: Path) -> str:
    m = re.findall(r"^\S+\s+(plastic|metal)\s+(\S+)\s*$",
                   rad_file.read_text(), re.M)
    if not m:
        raise ValueError(f"no plastic/metal primitive in {rad_file}")
    return m[-1][1]


def parse_obj_shell(obj_path: Path) -> dict[str, list[list[list[float]]]]:
    """Return {group_name: [face_verts, ...]} for SHELL_GROUPS only."""
    verts: list[list[float]] = []
    faces_by_group: dict[str, list[list[list[float]]]] = {}
    current_group: str | None = None

    with obj_path.open() as f:
        for line in f:
            if line.startswith("v "):
                verts.append([float(x) for x in line.split()[1:4]])
            elif line.startswith("g "):
                name = line[2:].rstrip()
                current_group = name if name in SHELL_GROUPS else None
            elif current_group and line.startswith("f "):
                idx = [int(tok.split("/")[0]) - 1 for tok in line.split()[1:]]
                faces_by_group.setdefault(current_group, []).append(
                    [verts[i] for i in idx]
                )
    return faces_by_group


def write_scene(out_path: Path, faces_by_group, target_groups: set[str],
                target_mod: str, ies_dat: Path | None) -> None:
    lines: list[str] = []

    # Sky + sun (only when no IES — otherwise let interior lighting dominate
    # so dynamic range stays manageable for tone-mapping).
    if not ies_dat:
        sky_r = 12
        sun_r = 120
        lines.append(f"""
# --- Daylight (no IES present) ---
void light sky_glow
0
0
3 {sky_r} {sky_r + 1} {sky_r + 3}

sky_glow source sky
0
0
4 0 1 0 180

void light sun_emit
0
0
3 {sun_r} {sun_r * 0.8} {sun_r * 0.55}

sun_emit source sun
0
0
4 0.5 0.7 0.6 0.5
""")
    else:
        # Faint sky glow only — windows visible but not blown out.
        lines.append("""
# --- Faint sky (IES handles main illumination) ---
void light sky_glow
0
0
3 0.5 0.6 0.8

sky_glow source sky
0
0
4 0 1 0 180
""")

    # IES downlight grid on ceiling
    if ies_dat:
        lines.append(f"""
void brightdata ies_dist
5 flatcorr {ies_dat.name} source.cal src_phi src_theta
0
1 71.4784

ies_dist light ies_emit
0
0
3 1 1 1
""")
        n = 0
        for x in (-58, -46, -34, -22, -10):
            for z in (-28, -20, -12, -4):
                lines.append(f"""ies_emit ring downlight_{n}
0
0
8
  {x} 31.74 {z}
  0 -1 0
  0 0.185
""")
                n += 1

    # Stub materials for non-target shell groups
    lines.append("""
# --- Stub materials ---
void plastic stub_wall
0
0
5 0.78 0.74 0.68 0 0

void plastic stub_ceiling
0
0
5 0.92 0.92 0.90 0 0

void plastic stub_floor
0
0
5 0.55 0.50 0.45 0 0

void plastic stub_column
0
0
5 0.70 0.68 0.64 0 0

void glass stub_glass
0
0
3 0.85 0.88 0.90
""")

    # Default modifier per group (used when group is NOT a target)
    group_default_mod = {
        "1_ARCHITECTURE Floor": "stub_floor",
        "1_ARCHITECTURE Ceiling": "stub_ceiling",
        "1_ARCHITECTURE Wall_Brick": "stub_wall",
        "1_ARCHITECTURE Columns": "stub_column",
        "1_ARCHITECTURE Mullion": "stub_column",
        "1_ARCHITECTURE Glazing": "stub_glass",
    }

    # Emit polygons
    poly_id = 0
    for group, faces in faces_by_group.items():
        mod = target_mod if group in target_groups else group_default_mod[group]
        for face in faces:
            lines.append(f"{mod} polygon p{poly_id}\n0\n0\n{len(face)*3}\n")
            for v in face:
                lines.append(f"  {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}\n")
            lines.append("\n")
            poly_id += 1

    out_path.write_text("".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("material_dir", type=Path)
    p.add_argument("--obj", type=Path, default=Path("scenes/cs-office-test.obj"))
    p.add_argument("--target", action="append", default=[],
                   help=f"group to receive material (default: floor). "
                        f"Available: {sorted(SHELL_GROUPS)}")
    p.add_argument("--ies", type=Path,
                   default=Path("materials/ies/downlight.dat"),
                   help="IES photometric .dat file (set to empty to disable)")
    p.add_argument("--out", type=Path,
                   default=Path("output/render_office_inline"))
    p.add_argument("--res", default="900x600")
    p.add_argument("--ab", type=int, default=3, help="ambient bounces")
    p.add_argument("--ad", type=int, default=1024, help="ambient divisions")
    p.add_argument("--as_", "--asuper", type=int, default=256,
                   dest="as_", help="ambient supersamples")
    p.add_argument("--aa", type=float, default=0.15, help="ambient accuracy")
    p.add_argument("--ps", type=int, default=4, help="pixel sampling rate")
    p.add_argument("--pt", type=float, default=0.05, help="pixel threshold")
    p.add_argument("--vp", default="-40,27.5,-20")
    p.add_argument("--vd", default="0.8,-0.2,0.6")
    args = p.parse_args()

    for cmd in ("oconv", "rpict", "ra_bmp", "pcond"):
        if not shutil.which(cmd):
            print(f"error: {cmd} missing", file=sys.stderr)
            return 1

    mat_dir = args.material_dir.resolve()
    rad_file = next(mat_dir.glob("*.rad"), None)
    if rad_file is None:
        print(f"error: no .rad in {mat_dir}", file=sys.stderr)
        return 1
    target_mod = detect_material_name(rad_file)
    targets = set(args.target) if args.target else {"1_ARCHITECTURE Floor"}
    print(f"material modifier: {target_mod}")
    print(f"target groups: {sorted(targets)}")

    print(f"parsing {args.obj}…")
    faces_by_group = parse_obj_shell(args.obj.resolve())
    for g in sorted(faces_by_group):
        print(f"  {g}: {len(faces_by_group[g])} faces")

    work = Path(tempfile.mkdtemp(prefix="office_inline_"))
    print(f"working in {work}")

    ies_dat: Path | None = None
    if args.ies and str(args.ies).strip():
        ies_path = args.ies.resolve()
        if ies_path.exists():
            ies_dat = ies_path
            shutil.copy(ies_dat, work / ies_dat.name)

    scene_rad = work / "scene.rad"
    write_scene(scene_rad, faces_by_group, targets, target_mod, ies_dat)

    octree = work / "scene.oct"
    env = {**os.environ,
           "RAYPATH": f"{mat_dir}:{work}:.:/usr/local/radiance/lib"}
    print("oconv…")
    with open(octree, "wb") as out:
        subprocess.run(["oconv", str(rad_file), str(scene_rad)],
                       check=True, stdout=out, env=env)

    w, h = (int(x) for x in args.res.lower().split("x"))
    vp = args.vp.split(",")
    vd = args.vd.split(",")
    render_hdr = work / "render.hdr"
    print(f"rpict {w}×{h}  ab={args.ab}  ad={args.ad}  ps={args.ps}…")
    with open(render_hdr, "wb") as out:
        subprocess.run([
            "rpict", "-vp", *vp, "-vd", *vd, "-vu", "0", "1", "0",
            "-vh", "65", "-vv", "45", "-x", str(w), "-y", str(h),
            "-ab", str(args.ab),
            "-aa", str(args.aa),
            "-ad", str(args.ad),
            "-as", str(args.as_),
            "-ps", str(args.ps),
            "-pt", str(args.pt),
            "-dj", "0.7",
            "-dt", "0.05",
            str(octree),
        ], check=True, stdout=out, env=env)

    # Adaptive tone-mapping: pcond -a does luminance adaptation without the
    # aggressive dark-region crushing that -h causes.
    tm_hdr = work / "render_tm.hdr"
    with open(tm_hdr, "wb") as out:
        subprocess.run(["pcond", "-a", str(render_hdr)],
                       check=True, stdout=out)
    bmp = work / "render.bmp"
    subprocess.run(["ra_bmp", str(tm_hdr), str(bmp)], check=True)

    out_base = args.out.resolve()
    out_base.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(render_hdr, out_base.with_suffix(".hdr"))
    shutil.copy(bmp, out_base.with_suffix(".bmp"))
    try:
        from PIL import Image
        Image.open(bmp).save(out_base.with_suffix(".png"))
    except ImportError:
        pass

    print(f"\nwrote:\n  {out_base.with_suffix('.hdr')}\n  "
          f"{out_base.with_suffix('.bmp')}\n  "
          f"{out_base.with_suffix('.png')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
