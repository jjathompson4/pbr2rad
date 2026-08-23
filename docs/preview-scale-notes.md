# Preview scale vs. the source-site spheres — notes for a future session

*Status (2026-08-22, evening): DECIDED for the preview card — the Radiance preview
**always** wraps the texture the way the source site's sphere does (no toggle, no
dropdown), with a note under the previews pointing to Maps & Projection for the export
projection (box/triplanar by default). That is the `preview-reference-wrap` work minus
the toggle; shipped from `main` (see CHANGELOG "Preview sphere matches the source
render"). Still OPEN: the real-world unit question (section 2 below and candidate
direction 2) — ClimateStudio keeps the Rhino document units, so this needs a
model-units input plus the sources' physical sizes. Calibration script:
`scripts/preview_calibration.py`.*

## The question (Jeff)

> "Every single rad preview scales the bitmaps differently than the source file preview
> from ambientCG and Poly Haven … I'd like to find a more streamlined way to do this and
> make the previews match in terms of scale more automatically."

There are two different "scales" hiding in that sentence:

1. **The preview card** — our Radiance sphere render next to the source site's own sphere
   render in the Output panel. They never look alike in pattern scale/layout.
2. **The exported material in the model** — Radiance has no units; our `u_scale`/`v_scale`
   are "repeats per model unit" and default to **1**, so a 2 m tile texture repeats every
   metre in a metric model and every *foot* in an imperial one. (The docs carry a lookup
   table for this — `docs/using-pbr2rad-with-climatestudio.md`, "Texture scale is tied to
   model units" — which is a sign it should be automatic.)

## Measured facts (2026-08-22)

**How the source sites render their spheres** (measured 2026-08-22 by unwrapping the
reference renders to longitude/latitude and counting grout / mortar lines, cross-checked
by eye on contact sheets of albedo vs render):
- Equirectangular UV wrap on a sphere, poles at top/bottom, **camera straight-on at the
  equator**, soft studio key with the highlight upper-left.
- **ambientCG: a fixed 3 repeats around (U), 1.5 pole-to-pole (V)** — square texels at
  the equator. Tiles141 (6 × 6 tiles per repeat) shows grout every ~20° of longitude and
  latitude (18 around / 9 pole-to-pole); Tiles140 (7 × 7) shows ~9 columns across the
  front and ~10 rows; Bricks058 ~25 courses per 180°. (A first estimate of 2 × 1 from
  eyeballing was 1.5× too coarse — Jeff spotted it as "6 tiles vs 4".)
- **Not physical-size based**: Paving Stones 151 (540 cm), Bricks058 (105 cm) and the
  200 cm tiles all show the same repeat density.
- **Poly Haven: same density, same orientation.** A first look at Brick Floor 003
  (albedo has horizontal courses, its sphere shows them vertical) suggested the
  picture was transposed there, and the preview briefly used
  `cal.spherical(..., swap_uv=True)` for Poly Haven. A survey on 2026-08-23 of the
  directional Poly Haven assets (planks, veneers, corrugated iron, brick walls,
  laminate, tiles, metal: structure-tensor orientation of the 1k albedo vs the centre
  of the 512 px thumbnail) found the orientation **preserved on 118 of 121, none
  rotated, 3 unclear** — `brick_floor_003` (below the anisotropy cut, but rotated on
  visual inspection) is the lone outlier: its thumbnail simply doesn't match its
  current albedo. So both
  sources use the untransposed wrap (u 3 / v 1.5); `swap_uv` stays available in
  `cal.spherical` but no source uses it. Survey: `scripts/polyhaven_orientation_survey.py`.
- Reference thumbnails used for calibration (512 px, dark background):
  `https://acg-media.struffelproductions.com/file/ambientCG-Web/media/thumbnail/512-JPG-242424/<Id>.jpg`
  for Ids WoodFloor052, Concrete034, Tiles141, Grass005, Bricks104, Fabric030, Metal049A.

**Our production preview** (`src/pbr2rad/web/preview.py` as deployed, PR #7): renders the
*exported* chain (box/triplanar by default) at `u_scale = 1` → one repeat per world unit
on a radius-1 sphere ≈ 2 repeats across the face, plus the three triplanar blend seams;
3/4 camera at `(2.6, −2.9, 2.1)`, FOV 27.5°; rig v5 (key 2.4@40°, fill 1.4@50°, sky glow
0.10, env glow 0.06), fixed exposure gain 1.30, saturation 1.25. Value-calibrated (means
match the references), but the pattern scale/layout never matches theirs.

**Physical-size metadata available:**
- Poly Haven: the API returns `dimensions` (mm) for **all 851** textures
  (`https://api.polyhaven.com/assets?t=textures`, e.g. `brick_floor_003: [2000, 2000]`).
  Our normaliser in `src/pbr2rad/fetch.py` sets `dims_cm = None` — **not parsed yet**.
- ambientCG: `dims_cm` for **510 of 2009** assets (already in the catalog and shown on
  the hero card, e.g. Tiles141 200 × 200 cm); `None` for the rest.

## What was built (first on branch `preview-reference-wrap`, 3940d39; shipped minus the toggle)

- `convert.py`: the inlined `.cal`/`_normal.cal`/`_rough.cal`/`.rad` writes extracted
  into `_write_material_chain(out_dir, stem, ProjectionSpec, _ChainInputs)`; new
  `ConvertOptions.write_preview_variant` writes a second chain
  `preview_<name>.{rad,cal,_normal.cal,_rough.cal}` (same `.hdr`/`.dat`, primitive
  `preview_<name>`) with `REFERENCE_WRAP` = spherical, u_scale 2, v_scale 1;
  `ConvertResult.preview_rad_file`. Exported files byte-identical (golden untouched).
- `web/preview.py`: camera straight-on `(0, −4.4249, 0)`; ENV glow 0.06 → 0.10 (= sky)
  so the equatorial view isn't top-heavy and near-mirror metals show no horizon line;
  scene file renamed `preview.scene.rad`.
- `web/models.py` / `web/api.py`: `preview_mapping` ∈ {reference, exported} on
  convert/upload/rerender (sibling of `options`, stored top-level in `job.json`, echoed
  on the response); `_convert_and_preview` renders `preview_rad_file or rad_file`.
- UI: "Preview sphere: Reference wrap | Exported projection" toggle at the top of Maps &
  Projection; editing a projection field auto-switches to Exported; caption changes.
- Calibration (sphere mean RGB vs ambientCG reference; up/lo = upper/lower-half
  luminance): Concrete034 190 vs 190 (up/lo 1.32 vs 1.13), Tiles141 220/212/203 vs
  215/211/206 (1.28 vs 1.08), Bricks104 180/131/111 vs 180/132/108 (1.38 vs 1.26),
  Grass005 100/130/52 vs 96/122/44, WoodFloor052 167/130/97 vs 201/149/94, Fabric030 99
  vs 117, Metal049A 141 vs 107 (known near-mirror limitation); mean |Δlog L| over
  concrete/tiles/bricks **0.009**.
- Verdict (Jeff): works and matches by construction, but the toggle was "a little
  clunky" — so the shipped version drops the toggle and the `preview_mapping` plumbing:
  the web app always writes and renders the wrap chain, and a note under the previews
  explains that the export projection is chosen separately (Maps & Projection, box by
  default for the ~90 % ClimateStudio case).

## Candidate directions (to weigh next time)

1. **Scale the sphere, not the material** — leading candidate for "automatic". Keep
   rendering the *exported* chain, but size the preview sphere so **one texture repeat
   spans its diameter**: for box/planar, `r = 1 / (2 · u_scale)` in model units, camera
   distance ∝ r so the framing is unchanged (`sphere_disk_radius` etc. already derive
   from the constants). The preview then tracks the user's U/V scale — and any
   physical-size setting (direction 2) — with no second `.cal`, no toggle, no
   `preview_mapping`; the pattern scale matches the references (~1 repeat across the
   face) while the seams stay honest to the chosen projection. Open points: cylindrical /
   spherical / uv modes (their repeat rules differ from box — choose r from the larger of
   the u/v repeat, or from v only), non-square textures (use the larger repeat), clamps for
   extreme u_scale, and whether a radius change should also move the lights (they're
   distant sources — no).
2. **Physical-size-aware export scale** — Model units (m · cm · mm · ft · in) + Texture
   size (prefilled from the source: Poly Haven all, ambientCG 510; editable; blank =
   "repeats per unit" as today) → `u_scale = v_scale = 1 / size_in_model_units`, shown in
   the existing fields and still overridable; Summary line "1 repeat = 2.00 m (0.50
   repeats/m)"; uploads type the size; CLI `--units ft --tile-size 2.0` or auto from the
   fetcher sidecar (`pbr2rad_source.json` — add `dims_cm` for Poly Haven); manifest/`.rad`
   header records the physical size. Pairs naturally with (1): the preview becomes "a
   ball as big as one tile". Effort ≈ half a day.
3. **Reference wrap always, no toggle** — **this is what shipped** (2026-08-22); the
   projection settings never show in the preview sphere; the note explains it.
4. **Straight-on camera + ENV 0.10 only** — small, independently good, can ship alone
   (tests on the branch: `test_camera_is_straight_on_and_key_is_upper_left_front`,
   updated `test_sphere_disk_radius_matches_camera_geometry`, `TestRigNeutrality`).
5. ~~Keep the parked toggle~~ — superseded by (3).

## How to resume

- The wrap preview is on `main`; the local branch `preview-reference-wrap` (3940d39)
  is the pre-simplification version (with the toggle) and can be deleted.
- Calibration: `PYTHONPATH=src RAYPATH=.:/usr/local/radiance/lib .venv/bin/python
  scripts/preview_calibration.py --tag try1 [--mode exported|wrap] [--refs DIR] [--out DIR]`
  — converts the 7 reference materials from the (cached) ambientCG 1K-JPG zips, renders
  the preview sphere with the *current* `preview.py`, prints mean RGB / up-lo vs the
  references and writes a comparison sheet. Env overrides `RIG_ENV`, `RIG_SKY`,
  `RIG_KEYZ`, `RIG_KEYANG` for quick rig experiments.
- Acceptance targets (from the round-5 calibration): mean |Δlog L| < 0.15 over
  concrete/tiles/bricks, up/lo 1.1–1.4, every light grey with `rig_neutrality ≤ 1.01`,
  `fixed_exposure()` within 1–6; plus, for scale, "about one repeat across the sphere's
  face" for square tileable textures.
- Related: `docs/next-steps.md` items 4 and 6; the About dialog lists no limitation for
  this (it's a preview nicety, not a material defect).

## Saturation / brightness / VLR audit (2026-08-22, after the wrap fix)

- Saturation vs the references is **luminance-dependent** (filmic-like view transform on
  their side): at luma 0.3–0.5 they are more saturated than our old flat ×1.25 (bricks
  0.66 vs 0.58, wood 0.66 vs 0.44, grass 0.67 vs 0.61); above luma 0.85 less (tiles 0.03 vs
  0.06, bricks 0.04 vs 0.09). `apply_look` now ramps ×1.40 (luma ≤ 0.40) → ×0.75 (luma ≥
  0.95), luma preserved. Candidates evaluated offline on the 7 references: uniform 1.0
  bin-err 0.106 / uniform 1.25 0.089 / 1.35→0.80 0.074 / **1.40→0.75 0.070** / 1.45→0.70
  0.066 (chosen the middle one). Wood remains under-saturated vs its reference (0.19
  bin-err) whatever the curve — their wood is simply richer everywhere.
- Brightness: neutral diffuse materials match within ±2 % by construction; wood −13 % and
  fabric −16 % (ref has a glossy sheen under a bright HDRI; our plastic spec 0.05 + dim
  env can't produce it — and the exported material behaves the same in a real scene, so
  the preview is honest); chrome +30 % (known limitation). A flatter rig (ENV 0.15 / SKY
  0.12) moves up/lo from 1.3 to 1.25 but brightens grass (+21 %) and chrome (+82 %) —
  not adopted; ENV stays 0.10.
- VLR math (`convert.material_reflectance`): diffuse = C·(1−spec), specular = spec for
  plastic / C·spec for metal (Radiance `normal.c`: rdiff = 1 − rspec, metal specular is
  the material colour), photopic weights 0.265/0.670/0.065 (Radiance's), albedo mean
  taken in linear space after sRGB decoding, `diffuse_scale` applied and clipped at 1.
  "VLR total" = diffuse + specular hemispherical reflectance. Correct as written.

## Rig v6 "studio" (2026-08-22) — metals

Near-mirror metals showed the key/fill as **black discs**: Radiance zeroes a specular
ray that hits a `light` source (the direct calculation is supposed to add that highlight
via the Gaussian lobe, which for α ≈ 0.001 never lands on a 40° disc). A roughness floor
alone doesn't fix it (white core inside a black ring: the sampled specular ray still
hits the light). Fix: **all sources are `glow`** — seen identically by specular rays and
the ambient pass — diffuse shading from `-ab 1 -ad 2048`. Rig: key 1.8 @ 60° soft-edged
(core 55 %, ramp to the surround level — a ramp to 0 draws a black ring because a
`source` occludes what is behind it), fill 0.30 @ 90° soft, sky 0.03, floor gradient
→ 0.09 at the nadir (continuous at the horizon, no horizon line), gain 1.22. Trade-off
kept: a fill bright enough to flatten diffuse shading clips to white in a mirror
(reference shows it mid-grey); dimming it steepens up/lo (fill 0.22/floor 0.07 →
1.37). Sheets: scratch `metal/`, `metal2..5/`, `r4/sheet_final.png`.

## Rig v7 "studio HDRI" (2026-08-23) — what shipped for metals

v6's glow discs still read as white blobs on grey; Jeff: "the rig needs a true studio
environment context". Tried four Poly Haven CC0 studio HDRIs as image-based lighting
(`colorpict` on two glow hemispheres, equirect lookup in `preview.rig.cal`, rotation
chosen to maximise irradiance on the reference key direction): **studio_small_09**
(rot 330°) gives dark-bodied chrome with a big soft key top-left like the ambientCG
reference and the flattest diffuse shading (up/lo 1.11–1.19); studio_small_08 too blue,
brown_photostudio_02 reads as a room, photo_studio_01 too bright. Per-channel white
balance (1.0176 / 1.0107 / 0.9729) makes the camera-facing irradiance neutral; off-axis
normals keep a little coloured bounce (≤ 1.15), as a real studio does. Exposure gain
re-trimmed to 1.12. `-ab 1 -ad 1024 -as 512` at 384 px ≈ 1.9 s with the same noise as
2048. The HDRI ships as package data (`web/assets/`, `pyproject` package-data).
Sheets: scratch `hdri/sheet_ibl.png`, `r4/sheet_v7b.png`.

Follow-up (2026-08-23): under the HDRI, plastics lost their sheen and the roughness
slider stopped doing anything — Radiance's default `-st 0.15` skips specular sampling
for spec < 15 % (the 5 % is folded into diffuse, so means didn't move and the
calibration hid it). Fixed with `-st 0.02 -ss 8` plus 2× supersampling reduced by
`pfilt -1 -x /2 -y /2 -r 0.6` (the glossy lobe sampled against small bright softboxes
sparkles otherwise; clamping the HDRI peaks barely helped, 2× halves the speckle to below
the texture's own level). Gain 1.15. Roughness 0.05 → sharp softbox reflections, 0.3 →
glossy sheen like the reference parquet, 0.6 → broad dull. `pfilt` without `-1` would
auto-expose and then apply `-e` — always pass `-1`.

## Two rigs (2026-08-23, final)

Jeff: "could we just have two separate rigs? one for metal one for others?" — yes:
`preview.rig_for(primitive)`: **plastics → light rig** (v5: key 2.4 @ 40°, fill 1.4 @ 50°
as `light`, sky/env glow 0.10, `-ab 2 -ad 512`, gain 1.30, ~2 s) — crisp Gaussian
highlights from the direct calculation, responsive to roughness, no sampling speckle;
**metals → HDRI rig** (v7, `-st 0.02 -ss 8`, 2× supersample, gain 1.15, ~4 s) — real
reflections. Each rig has its own irradiance model (`lights_irradiance` analytic,
`hdri_irradiance` numeric) and `fixed_exposure(rig)`. The seam: the backdrop flips with the
primitive when metalness crosses 0.5 — acceptable, each sphere is judged against its own
reference. `scripts/preview_calibration.py --rig auto|lights|hdri`.

Light rig v5b (2026-08-23): Jeff — "the light sources don't match the locations of the
ref materials": the references show ONE big soft highlight upper-left; our two `light`
discs put a second blob on the right. Fill is now a `glow` disc (0.7 @ 70°: shadow fill via
the ambient pass, no direct highlight), key a single `light` 1.6 @ 50° at (−0.64, −0.30,
0.72) so the highlight lands ≈ 0.4 R upper-left like theirs; sky/env 0.10; gain 1.21;
`-ab 2 -ad 1024`. Dark woods still render brighter than their references (our glow
surround lifts dark materials; their studio walls are dark) — a lower sky/env would fix
that at the cost of flatter-than-reference neutrals; left as is.

### Light rig v5c — edge glow (2026-08-23)

Jeff: "there are edge highlights that are mostly noticeable and impactful on the wood
materials, or materials with a significant sheen component … Wood Floor 052, the left
edge, you can see a strong edge glow … Wood 028, a strong edge glow on both left and
right sides." Measured (thin limb band r/R 0.93–0.99, ±30° of the limb point, ÷ sphere
mean, linear): Wood028 ref L 2.9 / R 4.1; WoodFloor052 L: dark 0.3 at 0.86–0.97 then a
line at the last 1.5 %; Poly Haven oak 2.2 / 1.9 (+ top 2.3); Concrete034 1.24 / 0.48
(flat — no rim on matte). Ours (v5b): 0.9–1.0 everywhere. It is the bright surroundings
seen through the glossy lobe at grazing angles (GGX + Fresnel in their renderers).

Why the glow surround can't do it in Radiance: `plastic`'s Fresnel term applies only to
roughness-0 specular; distant `glow` `source`s are `SSKIP` for the direct calculation;
with `-st 0.15` and 5 % specular no specular rays are traced. A `light` source behind the
sphere can: the direct calculation computes the Gaussian lobe, peak radiance ≈
ρs·L·Ω/(4π α² cos θi) (α = roughness², squared again) — ∝ 1/α² so strong on glossy,
nothing on matte, and it grows at grazing incidence.

Geometry (traced through the real camera): a source β° from straight-behind (+Y; the
camera sits at −Y) is mirrored by limb points at r/R — β 62° → 0.90, 54° → 0.93, 48° →
0.95, 40° → 0.97, 29° → 0.99. So y ≥ 0.6 lights only r/R ≥ 0.93: a rim, never a face
blob. Radiance evaluates a distant `source` at its centre only (no partitioning for
distant sources, no lobe widening on a curved surface): one disc gives a dash whose arc
length is set by the material's lobe, and a sparse row of discs gives dots at low
roughness (3 discs 30° apart → three dots; 11 discs 10° apart → dots at roughness 0.2;
26 discs 4° apart → dots at 0.15). Final: **34 discs per side, 3° apart** (elevations
−40°…+60°), 5° each, radiance 4.14 (E ≈ 0.025 each, ≈ 0.84 per side), β 44°,
`rim_directions()`. Continuous down to roughness ≈ 0.15. The discs face away from the
camera-facing normal → `fixed_exposure` and the gain 1.21 unchanged. (A β 34° detour —
tried when the fog below was misattributed to the rims' diffuse ring — was reverted on
Jeff's instruction; 2° spacing was relaxed to 3° for speed once `-ps 2` landed.)

### The real fog: folded specular (2026-08-23)

Jeff: "dark woods all appear grey and foggy … reducing the specular slider slightly
helps." Cause — rpict's default `-st 0.15` **folds** any specular below the threshold
(our plastics: 5 %) into the ambient reflectance: an isotropic veil L = ρs·E_amb/π
(E_amb ≈ 0.711, exposure 3.886 → ≈ 0.044 display-linear) on every material. Wood028's
mean linear albedo luma is 0.013 → its diffuse face is ≈ 0.016: **the veil was 3.5× the
wood itself** (predicted 0.060 with veil; actual render 0.056; ambientCG's reference
face 0.033). Light materials (+10 %) never showed it.

Fix: the light rig samples the lobe (`-st 0.02 -ss 8`, 2× supersample + pfilt −r 0.6)
**only for mean roughness ≤ 0.6** (`preview.samples_specular`): the 5 % then reflects
the surround directionally and Wood028's face drops to 0.047. Above 0.6 the render
keeps folding — a wide lobe reflects near-isotropically anyway, and sampling it through
a strong normal map re-enters the sphere and explodes (Fabric030, roughness 0.73 +
weave normals: 49 s at 192 px). Perf: `-dt/-dc` back at rpict defaults (`-dt 0` made
every ambient self-hit test all 68 rim sources — Fabric030 11 s → 1.7 s) and `-ps 2`
(visually identical; Fabric030 11.3 s → 1.7 s at -st 0.15). Timings: glossy ≈ 4.1 s,
rough ≈ 1.5–1.7 s per sphere.

Known seam: crossing roughness 0.6 on the slider switches sampled → folded, which
steps a *dark* material's face up ~20 % (Wood028: 0.048 @ 0.58 → 0.060 @ 0.62); light
materials don't show it. Raising the threshold would risk Fabric-style blowups on any
bumpy material pushed past it, so the seam stays.

Calibration after both changes (scratch `r5/rimcal.py`): Wood028 face 0.056 → **0.047**
(ref 0.037), limb band **2.4/2.45** (ref 2.9/4.1); WoodFloor052 1.7/1.7; Concrete034
1.20/1.26 (ref left 1.24); mean |Δlog L| over concrete/tiles/bricks **0.111** — the
gain 1.21 needed no retune.

## Wood colour vs references; per-source exposure (2026-08-23)

Measured ref-render ÷ own-albedo (linear, per channel): ambientCG Concrete034 1.07/1.06/1.02,
Bricks104 1.12/0.91/0.75, **WoodFloor051 1.86/1.60/1.08, WoodFloor052 1.82/1.56/1.03,
Wood095 1.08/1.38/1.57** — opposite shifts on two woods, so no lighting/tone curve explains it
(an ACES-fit view transform was tried: brightens and desaturates, doesn't match). Poly Haven
renders track their albedos neutrally (oak_veneer_01 0.50/0.48/0.46, plastered_wall_04
0.47/0.43/0.41 — just a dimmer exposure). Conclusion: ambientCG's wood preview images don't
match their current colour maps; our render shows the map (what CS renders). Decision
(Jeff): keep faithful, note it in About. Poly Haven's dim exposure → per-source factor
0.41 (`SOURCE_EXPOSURE_SCALE`; oak_veneer_01 +0.03, plastered_wall_04 +0.07 log vs their renders after trim), passed from the job's sidecar source.

