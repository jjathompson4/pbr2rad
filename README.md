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

```bash
# One PBR set:
pbr2rad /polyhaven/wood_floor_03 -o /rad_materials

# A parent folder containing many sets (each subfolder is one material):
pbr2rad /polyhaven_downloads -o /rad_materials --projection uv

# Native Radiance geometry without UVs — use a planar projection:
pbr2rad /polyhaven/wood_floor_03 -o /rad_materials \
    --projection planar --planar-axis xy \
    --u-scale 1.0 --v-scale 1.0

# Irregular geometry (no UVs, multiple surface orientations):
pbr2rad /polyhaven/concrete -o /rad_materials --projection box --u-scale 0.5 --v-scale 0.5
```

### Projection modes

| Mode     | When to use                                                   |
|----------|---------------------------------------------------------------|
| `uv`     | **Primary.** Any mesh with UVs (OBJ imported via `obj2mesh`). |
| `planar` | Native Radiance geometry on a known axis pair.                |
| `box`    | Native geometry with irregular orientation (triplanar).       |

`uv` emits a trivial `u = Lu; v = Lv;` passthrough. `planar` and `box` use
`Px/Py/Pz` and the surface normal; tune with `--u-scale` / `--v-scale`
(world-unit tiles per texture).

## Input

A folder containing a PBR material set. Both Poly Haven and ambientCG naming
conventions are recognised automatically, as is anything using the keywords
`diff/albedo/color`, `rough`, `metal`, `nor`/`normal`, `disp`/`height`.

```
wood_floor_03/
├── wood_floor_03_diff_2k.png    (albedo, sRGB)
├── wood_floor_03_rough_2k.png   (roughness)
├── wood_floor_03_nor_gl_2k.png  (normal, OpenGL) — not used in MVP
└── wood_floor_03_disp_2k.png    (displacement)   — not used in MVP
```

## Output

```
rad_materials/wood_floor_03/
├── wood_floor_03.rad    # colorpict + plastic/metal material definition
├── wood_floor_03.cal    # projection function
└── wood_floor_03.hdr    # albedo converted to linear Radiance HDR
rad_materials/manifest.json
```

Example `.rad` output:

```radiance
void colorpict wood_floor_03_pat
7 red green blue wood_floor_03.hdr wood_floor_03.cal u v
0
0

wood_floor_03_pat plastic wood_floor_03
0
0
5 1 1 1 0.05 0.25
```

## PBR → Radiance mapping

| PBR channel                    | Radiance                                 | MVP?              |
|--------------------------------|------------------------------------------|-------------------|
| Albedo (diffuse, sRGB)         | `colorpict` on linear `.hdr`             | yes               |
| Roughness                      | `plastic`/`metal` roughness (α = r²)     | yes (mean value)  |
| Roughness (spatially varying)  | `brightdata` modifier                    | post-MVP          |
| Metalness                      | `plastic` vs `metal` primitive           | yes (mean value)  |
| Normal map                     | `texfunc`/`texdata` perturbation         | post-MVP          |
| Displacement                   | geometry modification (not material)     | no                |

Roughness follows the common `α = r²` perceptual→microfacet mapping. Dielectric
specularity defaults to `0.05` (F0 ≈ 4 %), which is the standard neutral value
for non-metals. `metal` uses the albedo itself as specular reflectance and
passes specularity 1.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Tests cover: texture discovery (Poly Haven + ambientCG + resolution tiebreak),
`.cal` generation for all three projection modes, `.rad` emission for plastic
and metal, RGBE encode/decode round-trip, sRGB→linear correctness, and an
end-to-end CLI run over a small synthetic library.
