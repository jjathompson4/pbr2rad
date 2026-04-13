# pbr2rad

Convert PBR texture sets (Poly Haven, ambientCG, or any CC0 PBR source) into
ready-to-use Radiance material library folders.

Output is standard Radiance — works with CPU Radiance, Accelerad, and
ClimateStudio's GPU Radiance engine.

## Why

Radiance does not use UV coordinates for texture mapping. Instead it relies on
`.cal` files: mathematical functions that project bitmaps onto geometry using
hit-point coordinates (`Px`, `Py`, `Pz`) or — as of Radiance 6.0 — mesh-embedded
UVs exposed as `Lu`/`Lv`. Writing `colorpict` / `colordata` / `.cal` files by
hand is error-prone and expert-only, so in practice most Radiance scenes stay
flat-coloured. `pbr2rad` automates the whole pipeline.

## Install

```bash
pip install -e .
```

Requires Python 3.10+ and Pillow. No dependency on Blender, Rhino, or any GUI.

## Usage

### Fetch materials from Poly Haven

```bash
# Download a PBR texture set:
pbr2rad fetch wood_floor -o /materials --resolution 2k -v

# Other resolutions / formats:
pbr2rad fetch cobblestone_01 -o /materials --resolution 4k --format png
```

### Convert to Radiance

```bash
# One PBR set:
pbr2rad /materials/wood_floor -o /rad_materials

# A parent folder containing many sets (each subfolder is one material):
pbr2rad /materials -o /rad_materials --projection uv

# Native Radiance geometry without UVs — use a planar projection:
pbr2rad /materials/wood_floor -o /rad_materials \
    --projection planar --planar-axis xy \
    --u-scale 1.0 --v-scale 1.0

# Irregular geometry (no UVs, multiple surface orientations):
pbr2rad /materials/concrete -o /rad_materials --projection box --u-scale 0.5 --v-scale 0.5

# Column or pipe geometry:
pbr2rad /materials/brick -o /rad_materials --projection cylindrical --u-scale 2 --v-scale 2

# Dome or sphere geometry:
pbr2rad /materials/stone -o /rad_materials --projection spherical --u-scale 1 --v-scale 1
```

### Options

```
--projection {uv,planar,box,cylindrical,spherical}
--planar-axis {xy,xz,yz}      Axis pair for planar mode
--u-scale, --v-scale           Texture tiling scale
--u-offset, --v-offset         Texture offset
--roughness FLOAT              Override roughness (0..1)
--metalness FLOAT              Override metalness (0..1)
--bump-scale FLOAT             Normal map strength (default: 1.0)
--no-normal                    Skip normal map processing
--no-varying-roughness         Use mean roughness instead of per-pixel
-v, --verbose                  Show detailed output
```

### Projection modes

| Mode           | When to use                                                   |
|----------------|---------------------------------------------------------------|
| `uv`           | **Primary.** Any mesh with UVs (OBJ imported via `obj2mesh`). |
| `planar`       | Native Radiance geometry on a known axis pair.                |
| `box`          | Native geometry with irregular orientation (triplanar).       |
| `cylindrical`  | Columns, pipes, cylinders (wraps around Z axis).              |
| `spherical`    | Domes, globes, spheres (equirectangular mapping).             |

`uv` emits a trivial `u = Lu; v = Lv;` passthrough. `planar` and `box` use
`Px/Py/Pz` and the surface normal. `cylindrical` uses `atan2` around Z.
`spherical` uses `atan2`/`asin` for equirectangular mapping. Tune all modes
with `--u-scale` / `--v-scale` (world-unit tiles per texture).

## Input

A folder containing a PBR material set. Both Poly Haven and ambientCG naming
conventions are recognised automatically, as is anything using the keywords
`diff/albedo/color`, `rough`, `metal`, `nor`/`normal`, `disp`/`height`.

```
wood_floor/
├── wood_floor_diff_2k.png     (albedo, sRGB)
├── wood_floor_rough_2k.png    (roughness)
├── wood_floor_nor_gl_2k.png   (normal, OpenGL)
└── wood_floor_disp_2k.png     (displacement — not used)
```

## Output

```
rad_materials/wood_floor/
├── wood_floor.rad             # full modifier chain
├── wood_floor.cal             # projection function (for colorpict)
├── wood_floor.hdr             # albedo (RLE-compressed Radiance HDR)
├── wood_floor_nor_r.dat       # normal map R channel
├── wood_floor_nor_g.dat       # normal map G channel
├── wood_floor_nor_b.dat       # normal map B channel
├── wood_floor_normal.cal      # normal perturbation functions
├── wood_floor_rough.dat       # roughness map data
└── wood_floor_rough.cal       # roughness modulation function
rad_materials/manifest.json
```

Example `.rad` output (full modifier chain):

```radiance
void colorpict wood_floor_pat
7 red green blue wood_floor.hdr wood_floor.cal u v
0
0

wood_floor_pat texdata wood_floor_tex
9 dx_func dy_func dz_func wood_floor_nor_r.dat ...
0
1 1

wood_floor_tex brightdata wood_floor_rough
5 rough_func wood_floor_rough.dat wood_floor_rough.cal u v
0
1 0.8

wood_floor_rough plastic wood_floor
0
0
5 1 1 1 0.05 0.22
```

## PBR to Radiance mapping

| PBR channel                    | Radiance                                  | Status            |
|--------------------------------|-------------------------------------------|-------------------|
| Albedo (diffuse, sRGB)         | `colorpict` on linear `.hdr` (RLE)        | done              |
| Roughness (mean)               | `plastic`/`metal` roughness arg (a = r^2) | done              |
| Roughness (spatially varying)  | `brightdata` specular modulation           | done              |
| Metalness                      | `plastic` vs `metal` primitive            | done              |
| Normal map (GL/DX)             | `texdata` perturbation (3x `.dat`)        | done              |
| Displacement                   | geometry modification (not material)      | out of scope      |

Roughness follows the common `a = r^2` perceptual-to-microfacet mapping.
Dielectric specularity defaults to `0.05` (F0 ~ 4%), which is the standard
neutral value for non-metals. `metal` uses the albedo itself as specular
reflectance and passes specularity 1.

Normal maps are automatically detected (OpenGL or DirectX convention) and
converted to Radiance `texdata` perturbation. The green channel is flipped
for DirectX maps.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

78 tests covering: texture discovery, all five projection modes, material
generation (plastic/metal/normal/brightdata chains), RGBE encode/decode
round-trip, RLE compression, sRGB-to-linear correctness, 16-bit PNG handling,
Poly Haven API mocking, and end-to-end CLI runs.

## Visual verification

```bash
# Render a checkerboard cube to verify box projection:
python scripts/verify_box.py

# Full pipeline: fetch, convert, render
pbr2rad fetch cobblestone_01 -o /tmp/materials --resolution 2k -v
pbr2rad /tmp/materials/cobblestone_01 -o /tmp/rad --projection box --u-scale 3 --v-scale 3
oconv /tmp/rad/cobblestone_01/cobblestone_01.rad scene.rad > scene.oct
rpict [view args] scene.oct > out.hdr
```
