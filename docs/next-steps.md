# pbr2rad — Current State & Next-Step Plan

## Context

The design doc `Radiance Bitmap Texture Automation Layer` described a
standalone Python CLI that converts PBR texture sets (Poly Haven /
ambientCG) into Radiance material library folders usable by CPU
Radiance, Accelerad, and ClimateStudio's GPU Radiance engine.

All planned features from the original MVP and post-MVP roadmap have
been implemented. 200+ pytest cases pass and end-to-end renders with
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
├── fetch.py      # Poly Haven API client + shared download helpers
├── ambientcg.py  # ambientCG API client (zip packs, selective extract)
├── sources.py    # registry of texture sources (CLI --source, web)
├── convert.py    # per-set orchestration + manifest.json (+ provenance)
└── cli.py        # argparse entry point (`pbr2rad` / `pbr2rad fetch`)
tests/            # 200+ passing tests
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
| Roughness (spatially vary) | legacy opt-in (brightdata scales diffuse, not roughness); true varying roughness = mixdata of two plastics (todo) |
| Metalness (plastic/metal)  | done    |
| Normal map                 | done (texdata, GL/DX) |
| Manifest                   | done    |
| RLE compression            | done    |
| Poly Haven fetcher         | done    |
| ambientCG fetcher          | done (v3 API, zip packs, `--source ambientcg`) |
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

5. **~~`.dat` sampled 90° transposed relative to HDR.~~** `write_dat_2d`
   declared dims `(W, H)` while writing row-major — fast axis in the
   file was W but Radiance expects the last declared dim to be fast (H).
   On square sources the bytes are identical so it still rendered, just
   with cross-hatch artifacts between normal and albedo. Now writes
   column-major. See `CHANGELOG.md` for details.

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

### Web UI feature roadmap

1. **Primitive override.** Today the material primitive is inferred
   from metalness (`>= 0.5` → `metal`, otherwise `plastic`). Expose a
   dropdown to force `plastic` / `metal` / `trans` / `mirror`
   regardless of what the maps suggest. Add to `ConvertOptions.primitive`
   (currently implicit); thread through `rad.py::generate`.

2. ~~Roughness / specularity slider with live preview~~ — done (2026-08):
   "Tune material" sliders (specularity, roughness, diffuse ×) in the
   Output card call `POST /api/v1/jobs/{job_id}/rerender`, which
   re-converts the job's kept source maps with overrides (the `.hdr`
   must be re-baked when specularity/diffuse change) and re-renders the
   preview in a few seconds. Job state lives in `<job>/job.json`.

3. **Metalness slider with live preview.** Same pattern as (2) but for
   metalness (`metalness_override` already exists in `ConvertOptions`);
   crossing 0.5 swaps the primitive (tie into item 1 so the user sees
   plastic → metal transition live). Add a fourth slider to the Tune
   panel once the primitive override (1) exists.

4. **Preview geometry picker (plane / sphere / box).** Current preview
   always renders on a tilted flat plane — good for reading the texture
   honestly and matches Poly Haven, but loses the 3D "shader ball"
   context that's useful for judging specular response and curvature
   behavior. Bring back the sphere as a selectable option, add a cube
   as a third option for seeing box/triplanar projections across
   multiple faces at once. UI: radio buttons or tabs above the output
   preview. Preview-only geometry swap (regenerates `preview_scene.rad`
   and re-renders without re-downloading source maps).

5. **Resolution cap: default 1k, max 4k.** 8k / 16k sources cost
   >100 MB per material and don't meaningfully improve Radiance
   renders — they just make `.dat` files huge and `rpict` slow. Change
   the Poly Haven UI default from 2k to 1k and remove the 8k / 16k
   buttons (or grey them out with a note). Backend-side, add a guard
   in `fetch.download_texture_set` that rejects `resolution > 4k`
   unless an explicit override flag is set.

6. **Physical size → projection scale.** ambientCG publishes real-world
   dimensions (cm) for roughly a quarter of its materials and the fetcher
   already records them in `pbr2rad_source.json` (`dims_cm`) and the
   manifest. Offer a "use real-world size" option that sets
   `u_scale = 100 / dims_cm[0]`, `v_scale = 100 / dims_cm[1]`
   (`cal.py`: `u_scale = 1 / texture_width_meters`) so a 180 cm plank
   texture tiles at 180 cm in a metre-unit model. Poly Haven's `/info`
   also carries `dimensions` (mm) for textures — wire that into the
   sidecar the same way.

7. ~~Catalog prefetch on startup~~ — done (`PBR2RAD_PREFETCH_CATALOGS=1`,
   background thread in `web/app.py`), together with `persist_rootfs =
   "always"` in `fly.toml` so the disk copy actually survives auto-stop.

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
