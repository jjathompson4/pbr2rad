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

The 40 + 20 s split keeps the worst case near the old single 60 s cap.
Before this change, hitting that cap returned `preview_url: null` with no
reason.

**The production host's speed depends on CPU burst balance** (corrected
2026-11-10; the first version of this note said "about 3× slower than a
laptop"). pbr2rad runs on one Fly `shared-cpu-1x` machine that scales to
zero when idle. A shared vCPU runs at full speed only while it has burst
balance, and is otherwise held to a 6.25 % baseline (5 ms per 80 ms), about
16× slower. A machine starts with 5 s of balance, which refills only while
it is idle ([Fly CPU performance docs](https://fly.io/docs/machines/cpu-performance/)).
Measured on prod right after the v=38 deploy, Bricks105 1k:

| Request | Time | Preview |
|---|---|---|
| Convert, bump × 1 | 17.6 s | ok |
| Re-render, bump × 3 (first, with balance left) | 8.9 s | ok |
| Re-render, bump × 3 (balance draining) | 26.4 s | ok |
| Re-render, bump × 1 (throttled) | 81.9 s | timeout: full and draft both ran out |
| Re-render, bump × 2 (throttled) | 74.9 s | timeout |

Locally the same full render takes ~4 s. So at full speed the budget is
generous, and once throttled neither render fits it. That is a hosting
limit, not a render setting, so on 2026-11-10 production moved to a
dedicated-CPU `performance-1x` (2 GB), which is never throttled (Jeff's
call; see `fly.toml`). It is billed per second while running, and
scale-to-zero stays on, so idle time still costs ~$0.

The draft render (`DRAFT_AMBIENT`) uses one bounce, `-ad 256 -as 0 -aa 0.2`,
folded specular (rpict's default `-st`), and no 2× supersampling. It is 6–8×
faster than the full render and looks nearly the same, just a little
grainier. The UI labels it "Draft render".

When there is no picture (`timeout` / `error`), the material files are still
written and the response still carries everything else. On a re-render the
Output panel keeps the previous picture, dimmed, under a "Preview out of
date" badge. `render_preview` deletes the old `preview.png` first, so the
zip never ships a thumbnail of the previous settings.

## Progressive previews (2026-11-10)

Each convert and re-render from the web app now asks for the **draft**
render (`preview_quality: "draft"`), which is about 7× cheaper than the full
one. The page shows that picture, then calls `POST /api/v1/jobs/{id}/refine`,
which re-renders only the preview at full quality (no re-conversion) and
swaps it in. Making every preview a draft was rejected: the draft folds the
specular lobe, so dark glossy materials read slightly greyer (Wood028 mean
RGB 75/66/61 full against 80/72/68 draft). The draft is fine as a first
look, not as the picture you judge.

- **A refine never blocks anyone.** While it holds the one-job lock its
  cancel event is published, and any convert or re-render that arrives (from
  anyone) sets it and takes the lock. `rpict` is killed within ~0.1 s
  (`preview._run` polls the child). The page also abandons its own refine
  the moment a slider moves or Convert is clicked.
- **Budget:** the draft gets the 20 s draft budget. The refine gets 60 s
  (`REFINE_TIMEOUT_S`, the old single cap) and no draft fallback. If it
  times out, fails or is cancelled, the draft stays as the job's preview
  (`keep_on_failure`) and the page labels it "Draft render" (plus "Full
  render timed out" / "Full render failed" when that is why).
- **The refine runs in the background** (since Bug 6, below): `POST
  /jobs/{id}/refine` answers `202` with `preview_status: "running"` and a
  `refine_id`; the page long-polls `GET /jobs/{id}/refine?refine_id=…&wait=10`,
  which answers as soon as the render settles or after 10 s. No request
  stays open for the render.
- **Atomic picture:** `preview.png` is written to a temp file and moved into
  place, so a killed render never leaves half a PNG.
- **Rate limits:** refines have their own 20/min bucket
  (`rate_limit_refine`), so a visitor still gets 20 changes a minute. Polls
  have another (`rate_limit_poll`, 120/min); a refine at the 60 s cap costs
  about 7.
- Clients that don't send `preview_quality` (older cached pages) get the
  full render as before.

Measured locally (Bricks105 1k): convert draft on screen at 2.3 s, full at
6.8 s; re-render draft 1.0–1.1 s, refine ~4.3 s; a slider move 1.3 s into a
refine got its new draft 1.1 s later. Production is roughly 4× slower per
render, so expect the draft in ~4 s and the full picture ~15 s later.

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
could still run past 40 s on a loaded machine. That is what the draft fallback
is for. Since the black core (below) high bump costs little more than × 1. The Bump slider still goes to 5. Capping it lower (bump > ~3 tilts
normals past what a real surface does) is a product call that hasn't been
made.

## Bug 6: refines cut off at 30 s, and the black core (2026-11-10)

Reported from a headless browser on v=41: Red Brick 03 (Poly Haven, 1k),
bump × 2, the refine came back as a non-JSON **502 after ~30 s** and the page
stayed on the draft.

**The 502 was not ours.** The prod log shows that refine finishing with a
200 after 32 s. The same sequence by `curl` through Cloudflare and Fly got
the full picture after 31.7 s. Nothing in our chain cuts at 30 s: uvicorn
runs with no worker timeout, Fly's proxy has none at that length, and
Cloudflare's origin timeout is 100 s (it answers 524, not 502). The cutoff
was on the tester's network, a proxy with a ~30 s limit. Visitors behind
such proxies (corporate networks) would hit it too, which is why the refine
now runs in the background (above) instead of in one long request.

**The refine was also far too slow at high bump.** Red Brick 03 1k, refine
time (full render, 768 px, sampled specular at roughness 0.53):

| | bump × 1 | × 2 | × 3 | × 5 |
|---|---|---|---|---|
| Local (Apple Silicon), before | 4.0 s | 9.8 s | 20 s | 34 s |
| Production (performance-1x), before | – | 31.7 s | > 60 s: timeout | > 60 s: timeout |
| Local, with the black core | 4.5 s | 4.2 s | 4.7 s | 4.9 s |

Production runs these about 3.2× slower than local, so with the core expect
~15 s at any bump.

Where the time went, bump × 5 locally: full render 34.6 s; the same with
specular folded instead of sampled 7.6 s; sampled specular with no ambient
pass 2.9 s. So the cost was specular rays × ambient calculations. At high
bump the shading normal tips far enough that sampled reflections leave
*below* the surface and hit the inside of the sphere. Nothing lights the
inside, so those rays always come back black, but each one still ran a full
ambient calculation (`-ad 1024 -as 512`) at its hit point.

The fix is a zero-radiance `glow` sphere at radius 0.999 inside the ball
(`preview.BLACK_CORE`). Such rays end on it with no shading work, and the
picture is the same, because the inside was black anyway. Full renders,
with core against without, ambientCG/Poly Haven 1k at bump × 1 and × 3:

| Material | Time × 1 | Time × 3 | Mean RGB change |
|---|---|---|---|
| Red Brick 03 | 4.6 → 4.5 s | 20.6 → 4.7 s | ≤ 0.07 % |
| Fabric030 | 2.2 → 2.2 s | 5.8 → 2.4 s | ≤ 0.11 % |
| Planks040 | 2.0 → 2.1 s | 3.6 → 2.2 s | ≤ 0.15 % |
| Bricks105, Wood028, Concrete049 | unchanged | unchanged | ≤ 0.11 % |
| Metal032 (HDRI rig) | unchanged | unchanged | byte-identical |

The per-pixel differences are the ambient sampling's own noise moving (any
change to the scene reshuffles its random samples). Side by side the pictures
are indistinguishable. The exported material is untouched: the core exists
only in `preview.scene.rad`.

**Black specks on the draft.** At high bump the draft shows black specks and
dashes along the upper edges of mortar joints near the top of the ball. They
are pixels whose perturbed normal faces away from the camera: Radiance flips
such a normal and the pixel shades black. Every draft setting tried
(`-ps 1`, `-ab 0`, `-aa 0`, `-as 64`, sampled specular) still shows them,
so they are not a sampling artifact. The full render has the same pixels at
768 px, but its 2× Gaussian reduce turns them into a soft dark edge. At
bump × 2 and × 5 the full render shows no specks. They are expected on the
draft and gone once the refine lands (a few seconds now). A real fix would
curb the perturbation at grazing incidence, which Radiance's own source
suggests (`raynormal()`: keep `DOT(rdir, pert) < Rdot`). That would change
the exported `.cal`, so it is left for a deliberate change.

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
