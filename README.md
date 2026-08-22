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

### Fetch materials from Poly Haven or ambientCG

```bash
# Download a Poly Haven texture set (the default source):
pbr2rad fetch wood_floor_03 -o /materials --resolution 2k -v

# Download an ambientCG material (ids are case-insensitive; the folder
# uses the canonical id, e.g. Bricks104):
pbr2rad fetch --source ambientcg bricks104 -o /materials -v

# Resolution is 1k (default) or 2k; format is png (default) or jpg
# (Poly Haven also offers exr). ambientCG ships one zip per pack — only the
# maps pbr2rad consumes (color, roughness, metalness, normal) are extracted.
pbr2rad fetch --source ambientcg WoodFloor051 -o /materials --resolution 2k --format jpg
```

Both sources are CC0. Every fetched folder gets a `pbr2rad_source.json`
sidecar (source, asset id, asset URL, license, physical size when known)
that `pbr2rad convert` carries into `manifest.json` and the `.rad` header.
Non-square textures (common on ambientCG, e.g. 1024×512) are handled: the
projection `.cal` scales the picture lookup by the aspect (`pic_u`/`pic_v`)
so the albedo and the normal/roughness data stay aligned.
Downloads are cached under `~/.cache/pbr2rad/<source>/`
(`$PBR2RAD_CACHE_DIR` overrides the root).

### Convert to Radiance

```bash
# One PBR set:
pbr2rad /materials/wood_floor -o /rad_materials

# A parent folder containing many sets (each subfolder is one material):
pbr2rad /materials -o /rad_materials

# Irregular geometry / unknown orientations (default — triplanar):
pbr2rad /materials/concrete -o /rad_materials --projection box --u-scale 0.5 --v-scale 0.5

# Native Radiance geometry on a fixed axis pair:
pbr2rad /materials/wood_floor -o /rad_materials \
    --projection planar --planar-axis xy \
    --u-scale 1.0 --v-scale 1.0

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
| `box`          | **Primary.** Any geometry, any orientation (triplanar). Default. |
| `planar`       | Native Radiance geometry aligned to one axis pair.            |
| `cylindrical`  | Columns, pipes, cylinders (wraps around Z axis).              |
| `spherical`    | Domes, globes, spheres (equirectangular mapping).             |
| `uv`           | Experimental — requires a mesh-UV path that bypasses `obj2mesh` (see note). |

`box` uses `Px/Py/Pz` plus the surface normal to pick a projection plane
per-hit; works on floors, walls, columns, anything. `planar` uses
`Px/Py/Pz` on a fixed axis pair you choose (`xy`/`xz`/`yz`).
`cylindrical` uses `atan2` around Z. `spherical` uses `atan2`/`asin` for
equirectangular mapping. `uv` emits a trivial `u = Lu; v = Lv;`
passthrough. Tune all modes with `--u-scale` / `--v-scale` (world-unit
tiles per texture).

**Why `box` is the default and `uv` is experimental:** in current Radiance
(6.x), `obj2mesh` strips the textured material chain when building a
`.rtm` from an OBJ — `colorpict` / `texdata` / `brightdata` modifiers
silently fail to bind, so surfaces render as default grey. That breaks the
intended `uv` workflow for OBJ → mesh imports. Until that's resolved
upstream, drive pbr2rad with `box` projection (no mesh UVs needed) and
emit raw Radiance polygons for the surfaces under test. The
`scripts/render_office_inline.py` script in this repo demonstrates the
working path against a real Rhino-exported office scene.

## Input

A folder containing a PBR material set. Both Poly Haven
(`wood_floor_03_diff_2k.png`) and ambientCG (`Bricks104_1K-JPG_Color.jpg`)
naming conventions are recognised automatically, as is anything using the
keywords `diff/albedo/color`, `rough`, `metal`, `nor`/`normal`,
`disp`/`height`.

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

217 tests covering: texture discovery, all five projection modes, material
generation (plastic/metal/normal/brightdata chains), RGBE encode/decode
round-trip, RLE compression, sRGB-to-linear correctness, 16-bit PNG handling,
Poly Haven and ambientCG API mocking (incl. zip extraction safety), the web
API, and end-to-end CLI runs.

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

## Running the web UI (dev notes)

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn pbr2rad.web.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000/>.

### Why the `PYTHONPATH=src` prefix?

On this machine, something at the OS level (likely Spotlight/quarantine or a
similar daemon) sets the macOS `hidden` flag on `.pth` files inside the venv
~1 second after they're created or cleared. Python 3.13's `site.py` skips
`.pth` files marked hidden (UF_HIDDEN), so the editable install of `pbr2rad`
isn't picked up automatically and `import pbr2rad` fails with
`ModuleNotFoundError`.

Things that did **not** stick:
- `chflags nohidden …__editable__.pbr2rad-0.1.0.pth` — re-hidden within ~1s.
- Renaming the `.pth` file to something without the `__editable__` prefix —
  still re-hidden.
- `xattr -c` to strip `com.apple.provenance` — still re-hidden.
- `pip install -e . --config-settings editable_mode=compat` — setuptools
  still emits the same filename.

Workaround: launch with `PYTHONPATH=src` so the import works regardless of
whether the `.pth` file is being honored. The non-editable case (`pip install .`)
is unaffected since it doesn't rely on a `.pth` file.
