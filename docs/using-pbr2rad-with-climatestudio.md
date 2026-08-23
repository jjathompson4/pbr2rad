# Using pbr2rad with ClimateStudio + Rhino

A practical guide for authoring PBR-derived Radiance materials with pbr2rad and
applying them to Rhino/ClimateStudio models. Captures the workflow we
validated against a real Rhino office scene (`scenes/cs-office-test.obj`),
plus the gotchas we hit along the way.

> **Status (2026-08-22):** pbr2rad materials will likely only appear correctly
> in the ClimateStudio material browser from the **latest CS v2.4x stable
> release candidate** onward; earlier builds may not display textured
> materials properly in the material browser (see "What the custom-material
> import dialog shows" below). The web app's About dialog says the same under
> "Known limitations".

> **Thumbnail (`.pvw`) note:** pbr2rad.com's `.pvw` carries the web preview
> render — the texture wrapped on a sphere the way the texture sites render
> their reference spheres (illustrative), not the exported box/planar
> projection. The CLI's `.pvw` is the albedo swatch.

---

## How pbr2rad changes the Radiance texturing workflow

In Radiance 5.x and earlier, applying a bitmap to geometry meant authoring a
custom `.cal` file by hand for every surface — a small math function that
mapped world-space hit-point coordinates (`Px`, `Py`, `Pz`) into UVs in
[0, 1]. Different surface orientations needed different functions, and any
tiling, rotation, or offset was tuned by editing the `.cal`. Per-vertex UVs
weren't supported at all.

pbr2rad replaces that step. The generated `.cal` is a **generic triplanar
function**: it inspects the surface normal at the hit point, picks the
dominant world axis, and projects onto the other two. The same `.cal`
covers floors, walls, columns, and slanted surfaces from one material
definition — no per-surface authoring.

Two practical consequences:

- For **box / planar / cylindrical / spherical** projection modes, the
  manual coordinate-assignment step is fully eliminated. World position +
  normal at the hit point determine the UV.
- For **`uv` projection mode**, pbr2rad emits a trivial `u = Lu; v = Lv;`
  passthrough. `Lu` / `Lv` are mesh-embedded UVs available in Radiance 6.0+,
  but only on geometry built via `obj2mesh` from a UV-mapped OBJ. In most
  Rhino/CS workflows the geometry doesn't carry UVs, so **box projection is
  the default you want**.

---

## Workflow: pbr2rad → ClimateStudio

### 1. Generate the material

```bash
pbr2rad /path/to/wood_floor_pbr_set -o /path/to/rad_materials \
    --projection box \
    --u-scale 0.5 --v-scale 0.5
```

Choose `--u-scale` / `--v-scale` based on the *physical tile size in your
model's units*. Examples:

| Model units | Desired tile size | `--u-scale` / `--v-scale` |
|-------------|-------------------|---------------------------|
| feet        | 2 ft cobbles      | `0.5`                     |
| feet        | 1 ft wood plank   | `1.0`                     |
| meters      | 0.6 m tile        | `1.67`                    |
| millimeters | 600 mm tile       | `0.00167`                 |

`u-scale = 1 / tile_size_in_model_units` is the mental model.

Pick projection by geometry source:

| Mode          | When to use                                                |
|---------------|------------------------------------------------------------|
| `box`         | **Default for Rhino/CS.** Any surface, any orientation.    |
| `uv`          | Meshes with explicit UV unwrap (rare in Rhino).            |
| `planar`      | Single-axis surfaces where you want consistent direction.  |
| `cylindrical` | Columns, pipes (wraps around Z).                           |
| `spherical`   | Domes, spheres (equirectangular).                          |

Output structure for one material:

```
rad_materials/wood_floor/
├── wood_floor.rad            # the modifier chain
├── wood_floor.cal            # projection function (box triplanar)
├── wood_floor.hdr            # albedo as Radiance RGBE
├── wood_floor_nor_r.dat      # normal map R channel
├── wood_floor_nor_g.dat
├── wood_floor_nor_b.dat
├── wood_floor_normal.cal     # normal perturbation function
├── wood_floor_rough.dat      # spatially varying roughness
└── wood_floor_rough.cal
```

### 2. Drop the material into ClimateStudio's library

ClimateStudio reads Radiance material definitions from its library folder.
Copy the entire material folder (not just the `.rad` file) into:

| OS      | Path                                                                  |
|---------|-----------------------------------------------------------------------|
| Windows | `%APPDATA%/SolemmaLLC/ClimateStudio/Materials/`                       |
| macOS   | `~/Library/Application Support/SolemmaLLC/ClimateStudio/Materials/`   |

(Confirm the exact path against your CS install — Solemma may rename or
reorganize between versions.)

The folder name becomes the material modifier name, which is what you'll
pick from inside CS.

### 3. Assign in Rhino via CS material picker

In Rhino with the ClimateStudio panel open:

1. Open the material picker.
2. Find the pbr2rad-authored material by its folder name.
3. Assign it to a Rhino layer or directly to objects.

CS treats the material like any built-in one — daylight studies, annual
glare, and renderings all run against your full PBR-derived chain.

### 4. Verify outside CS before committing

Before pushing a material into a production CS workflow, render it against
a real architectural scene using the standalone scripts in this repo (see
below). This is much faster to iterate on than running CS simulations.

---

## What the custom-material import dialog shows (and doesn't)

Observed in practice (mid-2026, CS custom material import per
https://docs.solemma.com/custom-material): pbr2rad materials **render
correctly** in ClimateStudio once assigned, but the import dialog itself
can look broken — blank or missing parameter values, no preview sphere,
and possibly extra rows or missing entries. This is expected, not a
defect in the generated files:

> "A rendered preview is currently supported only for plastic, metal,
> or glass materials **without texture modifiers**." — Solemma docs

A pbr2rad material is exactly the thing that limitation excludes: a
`plastic`/`metal` at the end of a `colorpict → texdata → brightdata`
texture chain. Its material line is deliberately `5 1 1 1 <spec>
<rough>` — the *color* lives in the `.hdr` texture (pre-scaled for
energy conservation) and the *varying roughness* lives in a `.dat`
file. There is no single reflectance number in the `.rad` for CS's
dialog to display, so it displays nothing (or a misleading white).
The raw-text panel in the dialog is the reliable view: the header
comments state the primitive, metalness, and roughness.

### Confirmed behavior (tested 2026-07, CS custom library on Windows)

Observed with a 9-material pbr2rad library (`Desktop\Rad-Mat-Tests`):

| Aspect | Result |
|---|---|
| Materials listed | ✅ All appear, one row each, Type "Radiance plastic" |
| Junk rows | ✅ None — `*_pat`/`*_tex`/`*_rough` are correctly folded in |
| Raw text panel | ✅ Shows the full chain |
| Roughness column | ✅ Real values — but note it is the *Radiance* α (perceptual², e.g. 0.79 perceptual shows as 0.62) |
| Preview sphere | ❌ Blank (the documented CS limitation above) |
| VLR columns | ❌ **Every material reads VLR(tot) 100% / diff 95% / spec 5%** |

The VLR row is the meaningful defect: CS derives reflectance from the
material line's RGB, which pbr2rad deliberately sets to `1 1 1` (the
real color is in the `.hdr` pattern). The dialog therefore reports a
physically wrong 100% reflectance for every textured material. The
materials still *simulate/render* with the correct textured
reflectances — only the dialog's summary is wrong.

### What pbr2rad.com shows instead (2026-08)

The web Output panel now lists the numbers CS can't derive from a textured
chain, computed with Radiance's own semantics (`convert.material_reflectance`,
photopic weights 0.265/0.670/0.065): VLR total with the diffuse/specular
split, diffuse/specular RGB, specularity, roughness as Radiance α and as the
perceptual value, and the tile size. The same fields are in `manifest.json`
(`reflectance`, `specularity`, `roughness_radiance`, `avg_srgb_hex`) for CLI
libraries. Note the convention: for a pbr2rad `plastic` with pattern colour C
and specularity s, diffuse = C·(1−s) and specular = s — i.e. exactly the
"diff 95 % / spec 5 %" CS reports for RGB `1 1 1`, which is why the converter
no longer pre-scales the `.hdr` by (1−s) (that had been applied twice).

### Candidate fix — NOT YET IMPLEMENTED

If the re-test confirms the dialog problems, the plan is to have
pbr2rad additionally emit an **untextured summary twin** per material:

```
void plastic <name>_avg
0
0
5 <R> <G> <B> 0.05 <rough²>
```

with `R G B` = the manifest's `avg_linear_rgb` × the same
energy-conservation scale applied to the `.hdr` (0.95 for plastics),
and a `metal` variant using the unscaled average. CS can fully parse
and preview that twin, giving the dialog sensible numbers and a
thumbnail, while the real textured chain remains the material you
actually assign for simulations. Holding off on implementing until the
checklist above confirms which behaviors are real.

### Texture scale is tied to model units

`--u-scale` is multiplied by world coordinates. If you author a material
for a feet-based model and then someone imports it into a meters-based
project, tiles will be 0.3 m instead of 1 ft. Either standardize on one
unit when authoring, or regenerate the material for each unit system.

### Roughness / metalness are baked

pbr2rad picks `plastic` vs `metal` based on the source PBR set's
metalness map. To override after generation, either:

- Regenerate with `--roughness FLOAT` / `--metalness FLOAT` flags.
- Edit the final line of the `.rad` file directly:
  `5 1 1 1 <specularity> <roughness>` (plastic) or
  `5 <R> <G> <B> <specularity> <roughness>` (metal).

### CS GPU Radiance may not support all modifiers

The full chain emitted by pbr2rad (`colorpict` → `texdata` → [`brightdata`, opt-in] →
`plastic`) renders correctly on CPU Radiance and Accelerad. CS's GPU
Radiance engine has historically supported a subset of Radiance primitives;
varying-roughness via `brightdata` is the most likely thing to degrade or
silently flatten. Test against a known scene before relying on it.

### obj2mesh strips textured material chains

If you ever convert OBJ → `.rtm` via `obj2mesh` to use the `mesh` primitive,
the material binding for textured chains gets dropped — surfaces render as
default grey instead of your authored material. We hit this with the
office scene and worked around it by emitting raw Radiance polygons
inline (see `scripts/render_office_inline.py`). For CS this isn't
relevant — CS doesn't go through `obj2mesh` — but if you ever export to
external Radiance workflows that do, be aware.

---

## Standalone verification scripts in this repo

For sanity-checking materials before pushing into CS:

### `scripts/verify_box.py`

Synthetic checkerboard → pbr2rad with `--projection box` → render on a
unit cube. Validates the projection pipeline end-to-end without any real
PBR set. Run after any change to projection math.

```bash
python scripts/verify_box.py
# output: output/verify_box.bmp
```

### `scripts/verify_interior.py`

Generates a small interior scene with a multi-step staircase as the
material target. The staircase has treads, risers, and side panels —
multiple orientations — to exercise the triplanar projection.

```bash
python scripts/verify_interior.py <material_dir>
```

### `scripts/render_office_inline.py`

The serious one. Takes a Rhino-exported OBJ and a pbr2rad material
directory, extracts only the architectural shell (Floor, Wall_Brick,
Ceiling, Columns, Mullion, Glazing), and applies the material to whichever
layer(s) you target. Skips `obj2mesh` entirely and emits raw Radiance
polygons. Includes an IES downlight grid in the ceiling and tone-mapping
via `pcond -a`.

```bash
python scripts/render_office_inline.py materials/office_test/wood_floor \
    --target "1_ARCHITECTURE Floor" \
    --target "1_ARCHITECTURE Wall_Brick" \
    --target "1_ARCHITECTURE Columns" \
    --vp=-55,25,-30 --vd=1.0,-0.05,0.6 --res 1200x800
# output: output/render_office_inline.png
```

Quality tuning flags:

| Flag        | Purpose                       | Fast preview | High quality |
|-------------|-------------------------------|--------------|--------------|
| `--ab`      | ambient bounces               | `2`          | `4`          |
| `--ad`      | ambient divisions             | `1024`       | `2048+`      |
| `--as_`     | ambient supersamples          | `256`        | `512+`       |
| `--aa`      | ambient accuracy (lower=more) | `0.15`       | `0.08`       |
| `--ps`      | pixel sampling (lower=more)   | `4`          | `1`          |
| `--pt`      | pixel threshold               | `0.05`       | `0.04`       |

Going from preview to high-quality is roughly 20-30× the render time.

---

## IES luminaire integration

ClimateStudio handles IES files natively, but if you're rendering outside
CS for verification, the same files work via Radiance's `ies2rad`:

```bash
ies2rad -o downlight my_luminaire.ies
# produces: downlight.rad, downlight.dat
```

This emits a `brightdata light ring` chain at the origin pointing -Z.
For Rhino's Y-up coordinate system, place instances directly in your
scene `.rad` rather than relying on the generated geometry — example
inside `scripts/render_office_inline.py:downlight_grid()`.

---

## Setup notes specific to this repo

The Python venv in `.venv/` has had a stale `.pth` issue where editable
installs aren't picked up automatically. If `python -c "import pbr2rad"`
fails despite `pip install -e .` claiming success, work around it with:

```bash
PYTHONPATH=/Users/jeffthompson/Documents/Rad-stuff/src .venv/bin/python ...
```

Or recreate the venv cleanly:

```bash
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,web]"
```
