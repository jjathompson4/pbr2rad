# Changelog

## Unreleased

### Fixed

- **Radiance preview intermediates shipped in the download zip**
  (`web/api.py`). `_zip_directory` stripped `preview_*` files and `.oct` /
  `.bmp` by extension, but `preview.hdr` — the raw 384x384 render — has
  neither the underscore nor a skipped extension, so it landed in the zip
  next to the material's real `<name>.hdr` albedo, where it reads as a
  texture map. The prefix set now covers `preview.` as well, with an
  explicit keep-list for `preview.png`, which is a deliverable (it is also
  the source for the material's `.pvw` and is served by
  `/api/v1/preview/{job_id}`, so it stays on disk in every case —
  `_zip_directory` only decides what enters the stream).

  Landed in the same commit as the `.pvw` work below, though it is an
  unrelated fix; see that commit for both.

- **`.dat` layout vs. declaration mismatch** (`normal.py`) — `write_dat_2d`
  declared dimensions as `(width, height)` but wrote the data row-major. Per
  Radiance's rule "the last declared dimension varies fastest in the file,"
  the file layout needed to have height as the fast axis, not width. Because
  of the mismatch, Radiance read every `.dat` as if transposed: the first
  sample coordinate `u` ended up indexing our y-axis and `v` indexing our
  x-axis, so the normal / roughness maps were sampled 90° rotated relative
  to the accompanying HDR albedo.

  On square images (e.g. all Poly Haven textures, which are always N×N) the
  file bytes are identical either way, so the renderer didn't error — it
  just produced visible cross-hatch / moiré artifacts where a source pattern
  (wood grain, tile grout, etc.) appeared to "double up" diagonally between
  color and lighting. It would have broken outright on non-square sources.

  Fix: write `.dat` files column-major (one file line per X column, `height`
  values per line) so the fast axis in the file matches the last declared
  dim. Tests in `tests/test_normal.py::TestWriteDat2D` updated to verify the
  column-major layout.

### Added

- **ClimateStudio preview files.** Every material folder now ships a
  `<name>.pvw` alongside `<name>.rad`, so ClimateStudio's material browser
  can show a thumbnail for imported custom materials. The image is a render
  of the material where a renderer is available (the web app), and a swatch
  of the albedo otherwise (the CLI). `--no-pvw` opts out. Listed in
  `manifest.json` under `files.pvw`.

- **Per-map rotation overrides** (web UI). On the Poly Haven panel, each
  discovered channel (albedo, normal, roughness, metalness, AO,
  displacement) is shown as a thumbnail; clicking cycles it through
  0°→90°→180°→270°. The chosen rotation is applied to that channel only,
  before the HDR / `.dat` conversion, so patterns in the final output align
  correctly when a source distributes maps at inconsistent orientations.
  Keyed by discover channel name in `ConvertOptions.rotate_per_map` (`dict[str, int]`).

- **Flat-plane preview render** replacing the shader-ball sphere. A tilted
  plane avoids the spherical-UV moiré that was mis-read as a normal-map
  alignment bug, matches Poly Haven's primary preview format, and shows
  texture detail more honestly. Three-point studio lighting (warm key, cool
  fill, sky dome) gives better contrast than the old sun+sky setup.

### Changed

- **Filename classifier** (`discover.py`) — `_classify` previously used bare
  substring matching on the whole filename. That mis-assigned every file of
  assets whose base name happened to contain a channel word (e.g.
  `box_profile_metal_sheet_diff_1k.jpg` was classified as `metalness` rather
  than `albedo` because "metal" appears in the base name). Classifier now
  splits on `_`/`-`/`.`, strips the resolution / extension tags, and matches
  tokens only against the last 1–2 segments of what remains (the channel
  specifier).
