#!/usr/bin/env python3
"""Render a Rhino-exported OBJ scene with a pbr2rad material applied to
selected layer groups.

Pipeline:
  1. Rewrite OBJ usemtl directives so each `g <group>` block uses a chosen
     material name (cobblestone on Floor, glass on Glazing, default on rest).
  2. Build a Radiance material library with the pbr2rad chain + stubs.
  3. obj2mesh -> .rtm
  4. scene.rad references the mesh + adds lighting + camera.
  5. oconv -> rpict -> ra_bmp.

Example:
  python scripts/render_office.py scenes/cs-office-test.obj \
      materials/office_test/cobblestone_01 \
      --target "1_ARCHITECTURE Floor"

The model is assumed Y-up, units feet (typical Rhino export).
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


def check_radiance() -> bool:
    for cmd in ("obj2mesh", "oconv", "rpict", "ra_bmp"):
        if shutil.which(cmd) is None:
            print(f"error: {cmd!r} not found on PATH", file=sys.stderr)
            return False
    return True


def detect_material_name(rad_file: Path) -> str:
    text = rad_file.read_text()
    m = re.findall(r"^\S+\s+(plastic|metal)\s+(\S+)\s*$", text, re.M)
    if not m:
        raise ValueError(f"no plastic/metal primitive in {rad_file}")
    return m[-1][1]


# Radiance identifiers can't have spaces, parens, or punctuation.
_SANITIZE = re.compile(r"[^A-Za-z0-9_]+")


def sanitize(name: str) -> str:
    s = _SANITIZE.sub("_", name).strip("_")
    return s or "default"


def rewrite_obj(src: Path, dst: Path, target_groups: set[str],
                target_modifier: str, glazing_modifier: str = "glass_default",
                default_modifier: str = "neutral_default") -> dict[str, int]:
    """Strip existing usemtl lines, inject new one after every `g` directive.

    Returns face counts per modifier.
    """
    counts: dict[str, int] = {}
    current_mod = default_modifier
    last_emitted = None

    with src.open() as fin, dst.open("w") as fout:
        # Initial default so any face before the first `g` has a binding.
        fout.write(f"usemtl {default_modifier}\n")
        last_emitted = default_modifier

        for line in fin:
            if line.startswith("usemtl "):
                continue  # drop existing bindings
            if line.startswith("g "):
                fout.write(line)
                group = line[2:].rstrip()
                if group in target_groups:
                    current_mod = target_modifier
                elif "Glazing" in group:
                    current_mod = glazing_modifier
                else:
                    current_mod = default_modifier
                if current_mod != last_emitted:
                    fout.write(f"usemtl {current_mod}\n")
                    last_emitted = current_mod
                continue
            if line.startswith("f "):
                counts[current_mod] = counts.get(current_mod, 0) + 1
            fout.write(line)
    return counts


def write_material_lib(out_path: Path, mat_rad: Path,
                       target_modifier: str) -> None:
    """Combine the pbr2rad material with stub defaults into one library."""
    mat_text = mat_rad.read_text()
    stubs = f"""
# --- Stub materials for groups not under test ---
void plastic neutral_default
0
0
5 0.72 0.70 0.66 0 0

void glass glass_default
0
0
3 0.85 0.88 0.90
"""
    out_path.write_text(mat_text + "\n" + stubs)


def downlight_grid(ies_dat: str, ies_radius_ft: float,
                   ies_normalization: float = 71.4784) -> str:
    """Grid of IES downlights on the ceiling (y=31.75 ft, pointing down)."""
    cols = [-58, -46, -34, -22, -10]   # 5 cols  (x)
    rows = [-28, -20, -12, -4]         # 4 rows  (z)
    ceiling_y = 31.75
    out = [f"""
# IES luminaire photometric distribution (shared by all instances)
void brightdata ies_dist
5 flatcorr {ies_dat} source.cal src_phi src_theta
0
1 {ies_normalization}

ies_dist light ies_emit
0
0
3 1 1 1
"""]
    n = 0
    for x in cols:
        for z in rows:
            out.append(f"""
ies_emit ring downlight_{n}
0
0
8
  {x} {ceiling_y - 0.01} {z}
  0 -1 0
  0 {ies_radius_ft}
""")
            n += 1
    return "".join(out)


def scene_rad_text(mesh_name: str, mesh_file: Path,
                   ies_dat: str = "downlight.dat") -> str:
    lights = downlight_grid(ies_dat, ies_radius_ft=0.185)
    return f"""
# --- Geometry: pre-built triangle mesh from Rhino OBJ ---
void mesh {mesh_name}
1 {mesh_file}
0
0

# --- Daylight: dim overcast sky, modest sun through glazing ---
void light sky_glow
0
0
3 4 5 7

sky_glow source sky
0
0
4 0 1 0 180

void light sun_emit
0
0
3 40 32 22

sun_emit source sun
0
0
4 0.5 0.7 0.6 0.5

# --- IES recessed downlight grid on the ceiling ---
{lights}
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("obj", type=Path, help="OBJ scene file")
    p.add_argument("material_dir", type=Path,
                   help="pbr2rad output material folder")
    p.add_argument("--target", action="append", default=[],
                   help="group name to receive the material (repeatable; "
                        "default: '1_ARCHITECTURE Floor')")
    p.add_argument("--out", type=Path,
                   default=Path(__file__).resolve().parent.parent
                           / "output" / "render_office")
    p.add_argument("--res", default="1000x650")
    p.add_argument("--ab", type=int, default=2)
    p.add_argument("--vp", default="-50,27.5,-7", help="view position (x,y,z)")
    p.add_argument("--vd", default="0.7,-0.10,-0.7", help="view direction")
    p.add_argument("--keep-work", action="store_true",
                   help="don't delete working dir (debug)")
    args = p.parse_args()

    if not check_radiance():
        return 1

    targets = set(args.target) if args.target else {"1_ARCHITECTURE Floor"}

    obj = args.obj.resolve()
    mat_dir = args.material_dir.resolve()
    rad_files = sorted(mat_dir.glob("*.rad"))
    if not rad_files:
        print(f"error: no .rad in {mat_dir}", file=sys.stderr)
        return 1
    rad_file = rad_files[0]
    target_mod = detect_material_name(rad_file)
    print(f"material modifier: {target_mod}")
    print(f"target groups: {sorted(targets)}")

    try:
        w, h = (int(x) for x in args.res.lower().split("x"))
    except ValueError:
        print(f"error: invalid --res {args.res!r}", file=sys.stderr)
        return 1

    work = Path(tempfile.mkdtemp(prefix="render_office_"))
    print(f"working in {work}")

    sanitized_obj = work / "scene.obj"
    print("rewriting OBJ material bindings…")
    counts = rewrite_obj(obj, sanitized_obj, targets, target_mod)
    for m, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {m}: {c} faces")

    materials_rad = work / "materials.rad"
    write_material_lib(materials_rad, rad_file, target_mod)

    rtm = work / "scene.rtm"
    print("obj2mesh (this may take a minute)…")
    env = {**os.environ, "RAYPATH": f"{mat_dir}:{work}:.:/usr/local/radiance/lib"}
    subprocess.run(
        ["obj2mesh", "-a", str(materials_rad), str(sanitized_obj), str(rtm)],
        check=True, env=env,
    )
    print(f"  -> {rtm.stat().st_size // 1024} KB rtm")

    scene_rad = work / "scene.rad"
    scene_rad.write_text(scene_rad_text("office_mesh", rtm))

    octree = work / "scene.oct"
    print("oconv…")
    with open(octree, "wb") as out:
        subprocess.run(["oconv", str(materials_rad), str(scene_rad)],
                       check=True, stdout=out, env=env)

    vp = args.vp.split(",")
    vd = args.vd.split(",")
    if len(vp) != 3 or len(vd) != 3:
        print("error: --vp/--vd must be x,y,z", file=sys.stderr)
        return 1

    render_hdr = work / "render.hdr"
    print(f"rpict {w}×{h}  ab={args.ab} …")
    with open(render_hdr, "wb") as out:
        subprocess.run([
            "rpict",
            "-vp", *vp,
            "-vd", *vd,
            "-vu", "0", "1", "0",   # Y-up
            "-vh", "65", "-vv", "45",
            "-x", str(w), "-y", str(h),
            "-ab", str(args.ab), "-aa", "0.15", "-ad", "1024", "-as", "256",
            str(octree),
        ], check=True, stdout=out, env=env)

    tm_hdr = work / "render_tm.hdr"
    print("pcond (human-vision tone-mapping)…")
    with open(tm_hdr, "wb") as out:
        subprocess.run(["pcond", "-h", str(render_hdr)],
                       check=True, stdout=out)

    render_bmp = work / "render.bmp"
    subprocess.run(["ra_bmp", str(tm_hdr), str(render_bmp)], check=True)

    out_base = args.out.resolve()
    out_base.parent.mkdir(parents=True, exist_ok=True)
    final_hdr = out_base.with_suffix(".hdr")
    final_bmp = out_base.with_suffix(".bmp")
    final_png = out_base.with_suffix(".png")
    shutil.copy2(render_hdr, final_hdr)   # keep raw HDR for re-tone-mapping
    shutil.copy2(render_bmp, final_bmp)
    try:
        from PIL import Image
        Image.open(render_bmp).save(final_png)
    except ImportError:
        pass
    print(f"\nwrote:\n  {final_hdr}  (raw HDR)\n  {final_bmp}\n  {final_png}")

    if not args.keep_work:
        shutil.rmtree(work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
