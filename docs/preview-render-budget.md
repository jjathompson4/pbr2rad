# Preview render budget and high bump values

Decision log, 2026-11-09. Code: `src/pbr2rad/web/preview.py` (constants next
to `AMBIENT_BOUNCES` / `PREVIEW_FULL_TIMEOUT_S`), surfaced by
`api._convert_and_preview` as `preview_status` / `preview_error`.

## The budget

| Step | Limit | What happens past it |
|---|---|---|
| Full-quality render (`rpict`) | 40 s | Logged; a draft render starts |
| Draft render (`rpict`) | 20 s | `preview_status: "timeout"`, no preview |
| `oconv`, `pfilt`, `ra_bmp` | 30 s each | `preview_status: "timeout"` |

The production host is one shared vCPU, about 3× slower than a laptop. A
typical full render takes ~12 s there (Bricks105 1k: 4 s locally), so 40 s
is generous for normal materials. The 40 + 20 s split keeps the worst case
near the old single 60 s cap. Before this change, hitting that cap returned
`preview_url: null` with no reason.

The draft render (`DRAFT_AMBIENT`) uses one bounce, `-ad 256 -as 0 -aa 0.2`,
folded specular (rpict's default `-st`), and no 2× supersampling. It is 6–8×
faster than the full render and looks nearly the same, just a little
grainier. The UI labels it "Draft render".

When there is no picture (`timeout` / `error`), the material files are still
written and the response still carries everything else. On a re-render the
Output panel keeps the previous picture, dimmed, under a "Preview out of
date" badge. `render_preview` deletes the old `preview.png` first, so the
zip never ships a thumbnail of the previous settings.

## Why high bump values were slow

Bump × multiplies the `texdata` normal perturbation. At × 3 an edge texel (a
mortar line, a plank gap) tilts the shading normal by roughly 50°, and at × 5
by up to ~90° (grazing or behind the surface). Tilts like that point ambient and specular rays back
into the sphere. On a smooth convex sphere under distant sources, those
self-hits are the only thing a second ambient bounce can reach. So `-ab 2`
did no work at normal bump values, then blew up once rays started
re-entering:

| 768 px, ambientCG 1k | bump × 1 | bump × 3 |
|---|---|---|
| Bricks105 (sampled specular), `-ab 2` | 4.1 s | 67 s |
| Bricks105, `-ab 1` | 4.0 s | 4.3 s |
| Planks040 (folded), `-ab 2` | 11 s | 118 s |
| Planks040, `-ab 1` | 11 s | 18 s |

`-ab 1` vs `-ab 2` renders were byte-identical at × 1 and × 2 on Bricks105,
Bricks104, Concrete049, Planks040 and Fabric030. At × 3 the mean differed by
0.1 % (1.5–2 levels out of 255 per pixel). So the light rig now runs `-ab 1`,
like the HDRI rig, and the calibration gain is unchanged. Raising `-lr` made
no difference, and Radiance printed no warnings.

At × 5 a few rough, strongly normal-mapped materials (Bricks105, Fabric030)
can still run past 40 s on a loaded machine. That is what the draft fallback
is for. The Bump slider still goes to 5. Capping it lower (bump > ~3 tilts
normals past what a real surface does) is a product call that hasn't been
made.

## Client side

The server converts one job at a time for everyone (`_CONVERT_LOCK`, 503 +
`Retry-After: 15` when busy). The page therefore runs at most one convert or
re-render at a time:

- Convert buttons and the override sliders are locked while one is in flight.
- Responses are applied only if they are still the latest request
  (generation counter).
- Option changes made meanwhile are queued into one follow-up re-render.
- A 503 from another visitor's job is retried up to 3 times, honouring
  `Retry-After` but waiting at most 30 s each time.
