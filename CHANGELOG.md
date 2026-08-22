# Changelog

## Unreleased

### Added

- **ambientCG as a second texture source.** `pbr2rad fetch --source
  ambientcg Bricks104` downloads the material's zip pack from
  ambientcg.com (v3 API, CC0), extracts only the maps the conversion
  consumes (color / roughness / metalness / normal — displacement, AO
  and `.mtlx`/`.usdc` extras stay in the zip), and hands the folder to
  the existing discover → convert pipeline, which already understood
  ambientCG's `Id_1K-JPG_Map.jpg` naming. Ids are case-insensitive; the
  folder and material name use the canonical id. Extraction refuses
  traversal paths, oversized members, and header/size lies, and caps the
  zip at 64 MB (2K-PNG packs top out near 48 MB). New modules:
  `src/pbr2rad/ambientcg.py` (client) and `src/pbr2rad/sources.py` (a
  small registry — key, module, formats, TTLs — used by the CLI and web).
- **Provenance.** Every fetch now writes `pbr2rad_source.json` next to
  the maps (source, asset id/URL, license, physical size when published,
  map list); `convert_set` reads it into `ConvertResult.source`, the
  `manifest.json` entry (`"source": {…}`, additive) and a `# source:` line
  in the `.rad` header. Poly Haven fetches write it too.
- **Web: one Reference Material toggle.** The Poly Haven / Upload Files
  tabs become a single three-way toggle — Poly Haven | ambientCG | Upload
  Files — where the two catalog buttons share the browse panel. New routes
  `GET /api/v1/sources`, `GET /api/v1/sources/{source}/search?q=`,
  `GET /api/v1/sources/{source}/{id}/info`, `POST
  /api/v1/sources/{source}/convert` (body `{asset_id, resolution, fmt,
  options}`); the old `/polyhaven/*` and `/convert/polyhaven` routes
  remain as thin aliases. Search is token-AND over id, name and tags (so
  "red brick" finds `Bricks104`); catalogs are cached per source (1 h
  Poly Haven, 6 h ambientCG) with stale-on-failure. ambientCG publishes
  label-only tiles when no picture is available (rotation and USED/IGNORED
  still work). The web UI asks ambientCG for JPG packs (4–10 MB at 1k vs
  ~2× for PNG). `ConvertResponse` gains `source` / `source_url`, shown as a
  link in the result panel.

- **Selected-material hero.** Clicking a catalog tile now shows a large
  sphere render of the material (Poly Haven `thumbs/…?width=512`,
  ambientCG 512 px light/dark variants picked by theme) beside its name,
  source, physical size when published, tags and a "View on …" link, and
  scrolls the detail into view. The large previews ride along in search
  results so the hero appears before `/info` returns.
- **Real per-map thumbnails for ambientCG.** ambientCG publishes no
  per-map images, so `/info` now generates them: the asset's 1K-JPG pack
  (the same one the web convert uses, so the later conversion is a cache
  hit) is downloaded once, each map is thumbnailed straight from the zip
  (nothing extracted, JPEG draft-mode decode) into
  `~/.cache/pbr2rad/ambientcg/<Id>/thumbs_1K-JPG/`, and the tiles are
  inlined as data URIs. Failures degrade to label-only tiles and are only
  cached briefly, so the next click retries. Because this means *browsing*
  downloads packs from ambientCG (4–10 MB each), thumbnail-triggered
  downloads are throttled process-wide (burst 30, then 3/min, ≤ 2 in
  flight) and skipped entirely once the cache exceeds 2 GB; when the
  catalog entry is known the per-asset API call is skipped too.
- **Download cache budget.** `~/.cache/pbr2rad` (Poly Haven maps, ambientCG
  packs + thumbnails, catalogs) is now capped by `PBR2RAD_CACHE_MAX_MB`
  (default 1024; `0` disables) with least-recently-modified eviction
  (catalogs and in-flight `.part` files exempt), enforced after every
  download, at boot and every 10 min (`fetch.enforce_cache_budget`).
- **Deployment: persistent rootfs.** `fly.toml` sets `persist_rootfs =
  "always"`: with Fly's default the rootfs is reset on every auto-stop, so
  the catalogs were rebuilt upstream on every machine wake and every
  cached pack re-downloaded. Now caches survive stop/start and deploys
  (bounded by the budget above).
- **Preview renders: neutral environment + auto-exposure.** The preview
  scene gains a dim lower-hemisphere `glow source` so metals and glossy
  materials reflect something other than a black void (a chrome-like
  sphere was ~43 % pure black before), and exposure is now computed per
  render from the sphere's own luminance (median → mid-tone, 95th
  percentile kept below clipping; clamped 0.5–32×) instead of a fixed
  +1.4 stops. The PNG is RGBA with a transparent surround (the sphere's
  silhouette is derived from the camera constants) so it sits on either
  theme; the ClimateStudio `.pvw` thumbnail is flattened onto a dark grey.
- **Footer** reorganised into two rows (contact · site / texture credits ·
  Patreon) with separators and breathing room.

### Changed

- **Selection detail layout.** The hero row now holds the large preview,
  facts, resolution picker and Fetch & Convert side by side (≈260 px), the
  catalog grid keeps its rows at content height (`grid-auto-rows:
  max-content`) with a two-row minimum, and the browse tab scrolls on short
  windows — the detail no longer collapses the catalog tiles to slivers.
- Same-pack downloads are serialized per (asset, pack) so a tile click
  (thumbnails) and a convert can't stream into the same `.part` file;
  disk/cache I/O errors during a convert return 503 + `Retry-After`.
- **Reference Material toggle order/default.** ambientCG is now the first
  button and the landing source; Poly Haven second; Upload Files third.
  `pbr2rad fetch` keeps `--source polyhaven` as its default.
- **Catalog prefetch (deploy).** With `PBR2RAD_PREFETCH_CATALOGS=1` (set in
  `fly.toml`) the app warms the ambientCG + Poly Haven catalogs in a
  background thread at startup, so the first visitor after a deploy doesn't
  wait ~12 s on the cold ambientCG build. Off by default (tests/dev).
- Download cache is now per source: `~/.cache/pbr2rad/<source>/…`
  (`polyhaven/` is unchanged; `ambientcg/` is new). The web catalog cache
  file is `catalog.v3.json` under each source dir and holds the
  normalized entry list (v2 added the normalized shape, v3 the large/dark
  preview fields); older catalog files are simply no longer read.

### Fixed

- **Non-square textures rendered with the albedo misaligned against the
  normal/roughness maps** (`cal.py`, `rad.py`, `convert.py`). Radiance maps a
  picture so its *short* side spans `[0,1]` and its long side `[0, long/short]`
  (`getpict()` in `data.c`), while our projection `.cal` wraps `u`/`v` in the
  unit square and the `.dat` files declare `0 1 N` on both axes. For a 2:1
  texture (ambientCG's `Bricks104` is 1024×512) the `colorpict` therefore
  sampled only the left half of the albedo, stretched 2×, while the bump /
  roughness data spanned the full tile. Every Poly Haven texture is square,
  so it never showed there. The `.cal` now emits aspect-scaled
  `pic_u = u * max(1, w/h)` / `pic_v = v * max(1, h/w)` and the colorpict
  line uses `pic_u pic_v`; texdata/brightdata keep `u v`. Golden hashes
  regenerated for the `.cal`/`.rad` text (no `.hdr`/`.dat` change).

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
