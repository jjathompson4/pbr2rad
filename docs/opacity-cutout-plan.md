# Opacity maps → Radiance cutouts (perforated metal, fences, nets)

*Status: NOT IMPLEMENTED — design note for a future session (written 2026-08-22
after Jeff noticed ambientCG's perforated metals convert badly).*

## Symptom

ambientCG "Sheet Metal 001/002" (perforated plates), every "Fence" / "Net" /
"MetalWalkway" set, and other cut-out materials convert to a **solid** plate:
dark spots where the holes should be, often the wrong primitive, and a wrong
reflectance readout.

## Diagnosis

The opacity map is dropped at every stage of the pipeline:

| Stage | What happens today |
|-------|--------------------|
| `src/pbr2rad/ambientcg.py` `ACG_TO_INTERNAL` | no `"opacity"` entry → the `*_Opacity.*` file is never extracted from the zip |
| `src/pbr2rad/discover.py` `_PATTERNS` | no opacity / alpha channel → uploads and Poly Haven sets ignore it too |
| `src/pbr2rad/convert.py`, `rad.py` | no Radiance representation, no mixture support |

Consequences, all traceable to this one gap:

1. The holes render with whatever the Color map holds there (ambientCG fills
   them dark) → dark spots on a metal.
2. Hole pixels pollute the per-map means: mean metalness drops (≥ ~50 % open
   area flips the primitive to `plastic`), mean colour darkens, roughness
   shifts.
3. The reported reflectance describes the wrong material.

How common: 211 of the 2008 ambientCG materials in the catalog carry an
`Opacity` map (`maps` list in `~/.cache/pbr2rad/ambientcg/catalog.v3.json`):
SheetMetal001/002, Fence003/007A/007B/008A/008B, Net002A/004A, MetalWalkway014,
PaintedMetal004, many RoofingTiles* (tile edges), Fabric018/019 (fringes),
Wicker010A, Bamboo001C, Pizza001–004 (!), …

## Radiance mechanism — verified

Radiance has no alpha parameter, but a **mixture whose background is `void`
is a cutout**: where the mixing coefficient selects `void`, the surface is
not there and the ray continues. Verified 2026-08-22 with Radiance 6.0.2
(`rtrace` and `rcontrib`) using this scene — a plate with an r = 0.5 hole in
front of a wall; the ray through the hole sees the wall, the ray on the plate
sees the plate, and `rcontrib` (the daylight-matrix path) passes through too:

```
{ holes.cal: 1 = plate (foreground), 0 = void inside the hole }
holef = if(Px*Px + Py*Py - 0.25, 1, 0);
```
```
void glow red      0 0 4 1 0 0 0
void mixfunc perforated
4 red void holef holes.cal
0
0
perforated polygon plate   0 0 12  -2 2 1  2 2 1  2 -2 1  -2 -2 1
void glow green    0 0 4 0 1 0 0
green polygon wall         0 0 12  -9 9 3  9 9 3  9 -9 3  -9 -9 3
```
```
$ printf "0 0 0 0 0 1\n0.8 0 0 0 0 1\n" | rtrace -h -ab 0 -ov scene.oct
0 1 0      # through the hole → wall
1 0 0      # on the plate
```
(Light/glow sources emit from their front face only — wind the polygons so the
normal faces the ray origin, or the test reads black.)

Intermediate coefficients give partial transparency, so anti-aliased hole
edges come out soft. `mixpict` / `mixdata` are the same mechanism driven by a
picture / data file — `mixpict` on a greyscale `.hdr` fits our existing
colorpict pipeline best.

## Proposed implementation (≈ half a day)

Target chain — the user still assigns `X`:

```
void colorpict X_pat   7 red green blue X.hdr X.cal pic_u pic_v   (as now)
X_pat texdata  X_tex   …                                         (as now)
X_tex metal    X_base  0 0 5 1 1 1 1 0.2                         (as now, renamed)
void  mixpict  X       7 X_base void grey X_opacity.hdr X.cal pic_u pic_v
```
with `grey(r,g,b) = r` in the `.cal` (white = solid, black = hole). Using the
same `X.cal` + `pic_u/pic_v` keeps the cutout registered with the colour map
under every projection.

1. **Ingest** — `ACG_TO_INTERNAL["opacity"] = "opacity"`; `discover`
   tokens `opacity`, `alpha`, `mask`, `transparency` (covers uploads and the
   rare Poly Haven case); `ConvertOptions.opacity: bool = True`.
2. **Convert** — write the opacity map as a greyscale `.hdr` (same
   `convert_ldr_to_hdr` path, linear, clip 1); compute colour / metalness /
   roughness means **weighted by opacity** so the primitive describes the
   solid material, not the holes; emit the `mixpict` wrapper (name the
   primitive `X_base`); manifest / `ConvertResult` gain `open_area` (1 − mean
   opacity) and report reflectance for the solid material with the open
   fraction alongside. No `(1 − open)` scaling of the material itself — the
   cutout already removes that area.
3. **Web UI** — Maps & Projection: "Use opacity map (cutout)" checkbox,
   default on when the set has one; Summary: "Open area 38 %"; preview: holes
   should read as see-through like ambientCG's own reference — the preview
   PNG's alpha currently comes from a geometric sphere mask
   (`web/preview.py` `sphere_alpha`), so add a cheap coverage pass (e.g.
   re-render with the material swapped for a white `glow` on black) and use
   it as the alpha.
4. **Tests** — discover channel, weighted means, `.rad` text, golden hashes
   (intentional), rerender path unchanged (opacity is an option, flows through
   the `options` patch).

Test materials: `SheetMetal001` (≈ 40 % open, metal), `Fence007A` (thin
bars, mostly open), `Net002A` (extreme open area — good for the primitive
flip and the weighted means).

## Risks / unknowns

- **ClimateStudio** — plain Radiance handles `mixpict`+`void`; CS has its own
  parser/renderer layer and we already know textured chains import but do not
  preview in its dialog (`docs/using-pbr2rad-with-climatestudio.md`). Test
  a converted SheetMetal001 in CS before shipping this default-on. If CS
  rejects the mixture, the only fallback is the checkbox (solid, as today).
- Cutouts are zero-thickness — fine for sheet, fences, nets; wrong for thick
  grating (a geometry problem anyway).
- `rcontrib` attributes a pass-through ray to both the plate's modifier and
  what is behind it — irrelevant for sky/glazing-binned daylight matrices.
- The About dialog lists "perforated / cut-out materials" under Known
  limitations — remove that line when this lands.
