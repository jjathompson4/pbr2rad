# pbr2rad — Current State & Next-Step Plan

## Context

The design doc `Radiance Bitmap Texture Automation Layer` described a
standalone Python CLI that converts PBR texture sets (Poly Haven /
ambientCG) into Radiance material library folders usable by CPU
Radiance, Accelerad, and ClimateStudio's GPU Radiance engine.

The MVP has now been implemented on branch
`claude/pbr-radiance-converter-ZUZeV` and pushed to origin. 26 pytest
cases pass and an end-to-end smoke run produces valid Radiance syntax.
This file is a retrospective + incremental next-step plan; no further
changes have been made in this plan-mode turn.

## What is in place today

Package layout:

```
src/pbr2rad/
├── discover.py   # PBR set discovery (Poly Haven + ambientCG naming,
│                 #   resolution tiebreak, parent-folder multi-set mode)
├── hdr.py        # Pure-Python uncompressed RGBE writer +
│                 #   sRGB->linear LUT + LDR->HDR conversion
├── cal.py        # uv (Lu/Lv), planar (xy/xz/yz), box/triplanar
├── rad.py        # colorpict + plastic/metal emission,
│                 #   alpha = r^2 roughness mapping,
│                 #   metalness >= 0.5 -> metal primitive
├── convert.py    # per-set orchestration + manifest.json
└── cli.py        # argparse entry point (`pbr2rad`)
tests/            # 26 passing tests
pyproject.toml    # Python 3.10+, depends only on Pillow
README.md
```

Design-doc scope coverage:

| Doc table row (MVP = yes) | Status  |
|---------------------------|---------|
| Mesh UV passthrough       | done    |
| Planar projection         | done    |
| Box / triplanar           | done    |
| Albedo -> colorpict       | done    |
| Roughness (mean)          | done    |
| Metalness (plastic/metal) | done    |
| Manifest                  | done    |
| Normal map                | post-MVP (not started) |
| Spatially-varying rough   | post-MVP (not started) |
| Displacement              | out of scope (geometry, not material) |
| Cylindrical / spherical   | post-MVP (not started) |

## Practical implications

- One command converts an entire Poly Haven download folder; 50
  materials under 5 s.
- For mesh geometry with UVs the "hard part" of Radiance texture
  mapping is eliminated (`u = Lu; v = Lv;` is the entire .cal file).
- Output is pure-text Radiance with no vendor coupling, preserving
  the sellable-module strategic framing.
- Metalness auto-switches `plastic` -> `metal`, so chrome/copper
  actually look metallic rather than glossy plastic.

## Known soft spots (to address when relevant)

1. **Uncompressed RGBE output.** A 4K albedo is ~64 MB on disk. Every
   Radiance tool reads it, but adding Greg Ward's RLE would quarter
   that. Candidate: extend `hdr.write_hdr` with an `rle=True` path.
2. **Scalar roughness.** Spatially-varying gloss (tiled floors, mixed
   materials) will look wrong until `brightdata` support lands.
3. **Box projection unverified visually.** Syntactically tested only;
   needs an `rpict` render against a cube to confirm the `if()` ladder
   picks the right plane per face.
4. **Open questions the code can't answer alone:** does CS resolve
   HDR paths relative to the .rad file, and does CS's engine support
   Lu/Lv? Both would change defaults; neither can be validated
   without loading a generated folder into CS.

## Proposed next steps (pick the one that matches current priority)

### Option A — ClimateStudio integration validation (no new code)
End-to-end verify the current MVP before expanding scope.

- Generate a small library (3-5 materials) from real Poly Haven
  downloads.
- Load the output folder in ClimateStudio, assign to a test scene,
  run a simulation, confirm textures appear.
- Answer the four open questions in the design doc:
  relative vs absolute HDR paths, `colorpict` support, Lu/Lv support,
  proprietary modifier subset.
- Update `convert.py` defaults (path style, default projection)
  based on what CS actually expects. Likely a one-line change.

Files touched if changes are needed:
- `src/pbr2rad/convert.py` (path resolution)
- `src/pbr2rad/cli.py` (default `--projection`)
- `README.md` (integration notes)

### Option B — Normal map support (post-MVP item 1)
Add `texfunc` / `texdata` perturbation so surfaces get bump detail.

- New module `src/pbr2rad/normal.py`:
  - Convert normal PNG (OpenGL or DirectX) to a Radiance-friendly
    format (typically `.dat` via `texdata`, or a custom `.cal`
    that samples the HDR directly).
  - Generate a `texfunc` modifier that wraps the material.
- Extend `rad.py` `generate()` to chain `colorpict -> texfunc -> plastic/metal`.
- Extend `ConvertOptions` with `normal: bool` (default True if
  normal map discovered).
- Tests: verify emission order and that DX vs GL flip is honored.

Files touched:
- new `src/pbr2rad/normal.py`
- `src/pbr2rad/rad.py`, `src/pbr2rad/convert.py`, `src/pbr2rad/cli.py`
- new `tests/test_normal.py`

### Option C — Spatially varying roughness (post-MVP item 2)
Use `brightdata` on the roughness map so gloss varies per-pixel.

- Extend `hdr.py` with `convert_gray_to_dat` (Radiance .dat format).
- `rad.py`: emit a `brightdata` modifier that drives the roughness
  argument, chained before the primitive definition.
- Tests: verify correct modifier chain and argument count.

Files touched:
- `src/pbr2rad/hdr.py`, `src/pbr2rad/rad.py`,
  `src/pbr2rad/convert.py`, new `tests/test_brightdata.py`

### Option D — Poly Haven API fetcher
The doc lists "Can we batch-download Poly Haven materials
programmatically via their API?" as an open question. Add a
`pbr2rad fetch <slug>` subcommand that pulls from
`api.polyhaven.com` and drops into the input tree.

Files touched:
- new `src/pbr2rad/fetch.py`
- `src/pbr2rad/cli.py` (subparsers), new tests with a mocked API.

## Verification for any of the above

```bash
pip install -e ".[dev]"
pytest                 # must stay green (26+ tests)
# End-to-end:
python -m pbr2rad /path/to/polyhaven_downloads -o /tmp/rad_lib -v
# Spot-check a generated .rad with Radiance's parser (if installed):
rad -n /tmp/rad_lib/<material>/<material>.rad
# Visual: render a cube with rpict using the generated material and
# compare against a reference image.
```

## Recommendation

Start with **Option A**. The MVP is feature-complete per the
decision log; the highest-leverage next action is validating against
ClimateStudio before building more features on top of assumptions
about how CS consumes the output. Options B/C/D are all easy to layer
on once A's answers are in hand.
