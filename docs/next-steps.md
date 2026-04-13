# pbr2rad — Current State & Next-Step Plan

## Context

The design doc `Radiance Bitmap Texture Automation Layer` described a
standalone Python CLI that converts PBR texture sets (Poly Haven /
ambientCG) into Radiance material library folders usable by CPU
Radiance, Accelerad, and ClimateStudio's GPU Radiance engine.

All planned features from the original MVP and post-MVP roadmap have
been implemented. 78 pytest cases pass and end-to-end renders with
Radiance 6.1a confirm correct output for multiple materials and
projection modes.

## What is in place today

Package layout:

```
src/pbr2rad/
├── discover.py   # PBR set discovery (Poly Haven + ambientCG naming,
│                 #   resolution tiebreak, parent-folder multi-set mode)
├── hdr.py        # RGBE writer with adaptive RLE compression +
│                 #   sRGB->linear LUT + LDR->HDR conversion +
│                 #   16-bit PNG support
├── cal.py        # uv, planar, box, cylindrical, spherical projections
├── rad.py        # colorpict + texdata + brightdata + plastic/metal,
│                 #   alpha = r^2 roughness mapping,
│                 #   metalness >= 0.5 -> metal primitive
├── normal.py     # Normal map -> .dat files + .cal perturbation,
│                 #   roughness map -> .dat + brightdata modulation,
│                 #   GL/DX convention detection
├── fetch.py      # Poly Haven API client (download PBR sets)
├── convert.py    # per-set orchestration + manifest.json
└── cli.py        # argparse entry point (`pbr2rad` / `pbr2rad fetch`)
tests/            # 78 passing tests
scripts/          # verify_box.py (visual verification)
pyproject.toml    # Python 3.10+, depends only on Pillow
README.md
```

Design-doc scope coverage:

| Doc table row              | Status  |
|----------------------------|---------|
| Mesh UV passthrough        | done    |
| Planar projection          | done    |
| Box / triplanar            | done (visually verified with rpict) |
| Cylindrical projection     | done    |
| Spherical projection       | done    |
| Albedo -> colorpict        | done    |
| Roughness (mean)           | done    |
| Roughness (spatially vary) | done (brightdata) |
| Metalness (plastic/metal)  | done    |
| Normal map                 | done (texdata, GL/DX) |
| Manifest                   | done    |
| RLE compression            | done    |
| Poly Haven fetcher         | done    |
| 16-bit map support         | done    |
| Displacement               | out of scope (geometry, not material) |

## Resolved soft spots

1. **~~Uncompressed RGBE output.~~** Adaptive RLE compression is now
   the default. Reduces HDR size ~8-19% on photographic textures
   (more on synthetic). Controlled via `rle=True/False` in the API.

2. **~~Scalar roughness.~~** `brightdata` modifier now modulates
   specular reflectance per-pixel based on the roughness map. Rough
   areas have dimmer specular, smooth areas brighter.

3. **~~Box projection unverified visually.~~** Verified with
   `scripts/verify_box.py` — rpict render of a checkerboard cube
   confirms the triplanar `if()` ladder selects the correct plane
   per face.

4. **Open questions the code can't answer alone:** does CS resolve
   HDR paths relative to the .rad file, and does CS's engine support
   Lu/Lv? These require loading generated output into ClimateStudio.

## Remaining work

### ClimateStudio integration validation (no new code)

End-to-end verify against ClimateStudio before field deployment.

- Generate a small library (3-5 materials) from real Poly Haven
  downloads using `pbr2rad fetch` + `pbr2rad convert`.
- Load the output folder in ClimateStudio, assign to a test scene,
  run a simulation, confirm textures appear.
- Answer the open questions in the design doc:
  relative vs absolute HDR paths, `colorpict` support, Lu/Lv support,
  proprietary modifier subset.
- Update `convert.py` defaults (path style, default projection)
  based on what CS actually expects. Likely a one-line change.

Files potentially touched:
- `src/pbr2rad/convert.py` (path resolution)
- `src/pbr2rad/cli.py` (default `--projection`)
- `README.md` (integration notes)

## Verification

```bash
pip install -e ".[dev]"
pytest                 # must stay green (78+ tests)

# Fetch + convert end-to-end:
pbr2rad fetch wood_floor -o /tmp/materials --resolution 2k -v
pbr2rad /tmp/materials/wood_floor -o /tmp/rad_lib --projection box -v

# Render with Radiance:
oconv /tmp/rad_lib/wood_floor/wood_floor.rad scene.rad > scene.oct
rpict -vp 2 2 1.5 -vd -1 -1 -0.3 -vu 0 0 1 scene.oct > out.hdr
```
