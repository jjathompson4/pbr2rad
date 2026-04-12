# GPU Radiance Engine — Plan (v0.1 "gpurad")

## Context

The design doc `Radiance Bitmap Texture Automation Layer` described a
future project: a from-scratch GPU Radiance implementation on OptiX 9,
with full scene-format compatibility, bitmap texture support, and
potential use as a StereoFlux alternative backend or standalone product.

Motivation:

- **Accelerad is frozen** at OptiX 3.x (2019); OptiX 7+ was a complete
  API rewrite. The public Accelerad codebase is not a viable modern
  foundation, but it is a valuable **reference implementation** — it
  proves GPU Radiance matches CPU accuracy (Jones & Reinhart 2015) and
  its source layout documents which Radiance internals need GPU
  equivalents.
- **ClimateStudio** runs a proprietary evolution of Accelerad; a
  modern open GPU Radiance fills the obvious gap in the public stack.
- Hardware has moved far ahead: OptiX 9 on Ada/Hopper/Blackwell RT
  Cores is ~50× the throughput of the 2019 baseline.

## Scope decisions (already made)

| Decision                         | Choice                                             |
|----------------------------------|----------------------------------------------------|
| Accuracy target                  | **CPU Radiance parity** (rtrace within ~5%)        |
| v0.1 deliverables                | **Both `grpict` and `grtrace`**                    |
| Radiance source reuse            | **Link Radiance as C library** (fork the mirror)   |
| Repo & platform                  | **New repo, Linux-first**                          |

Parity is the non-negotiable here. It forces implementing Radiance's
ambient calculation / irradiance cache (Jones thesis, MIT 2017) rather
than a modern path tracer — harder, but the only path to AEC
compliance credibility.

## Reference anchors

- **Radiance source** — `github.com/LBNL-ETA/Radiance` (official CVS
  mirror, auto-synced daily, do not push to master). Fork this.
- **Accelerad source** — `github.com/nljones/Accelerad` (reference
  only; OptiX 3.x API, not directly portable but shows which Radiance
  internals were bridged to GPU and how).
- **Radiance 6.0 reference manual** —
  `radsite.lbl.gov/radiance/refer/refman.pdf` (scene format, material
  definitions, modifier semantics — the spec we must match).
- **OptiX 9 SDK** — developer.nvidia.com/designworks/optix/download.
  Start from the `optixPathTracer` sample; it's the closest published
  analog to what we need.
- **Jones & Reinhart 2015** — "Experimental validation of ray tracing
  as a means of image-based visual analysis" (validation methodology).
- **Jones PhD 2017** — "Validated Interactive Daylighting Analysis for
  Architectural Design" (the GPU irradiance-cache algorithm).

## Architecture overview

```
gpurad/                         # new repo
├── CMakeLists.txt
├── external/
│   └── radiance/               # fork of LBNL-ETA/Radiance (submodule)
├── src/
│   ├── common/                 # C++ host code: context, SBT, AS build
│   ├── rt_gpu/                 # OptiX host code: pipeline, kernels
│   ├── shaders/                # .cu files compiled to OptiX modules
│   │   ├── raygen_pict.cu      # grpict ray generation
│   │   ├── raygen_trace.cu     # grtrace ray generation
│   │   ├── closesthit_*.cu     # one per material type
│   │   ├── miss.cu             # sky/background
│   │   └── anyhit_shadow.cu    # shadow testing
│   ├── scene/                  # bridge layer: radiance structs → GPU
│   └── bin/
│       ├── grpict.cpp          # CLI: parses rpict args, drives pipeline
│       └── grtrace.cpp         # CLI: parses rtrace args, sensor output
├── test/
│   ├── unit/                   # googletest
│   └── validation/             # CPU-vs-GPU comparison scenes
└── docs/
```

**Integration pattern** (mirrors Accelerad's approach, updated to
modern OptiX):

1. **Scene parsing and octree construction**: reuse Radiance's own
   code verbatim. `src/ot/` builds the octree, `src/rt/otypes.c` has
   the object type table, `src/common/` has geometry primitives and
   sky models. Link these as a static library.
2. **Material parameter parsing**: same — Radiance's modifier-chain
   parser is the spec.
3. **Geometry upload**: walk the Radiance octree, extract triangles
   (tessellate any non-triangle primitives), build an OptiX
   Geometry Acceleration Structure (GAS). For mesh primitives, upload
   UVs so our own `colorpict` / Lu-Lv chain works.
4. **Material upload**: pack each resolved material into a GPU struct
   (albedo HDR handle, roughness, specularity, modifier chain IDs).
   One OptiX closest-hit program per material type.
5. **Output**: write Radiance HDR via the existing Radiance `.pic`
   writer in `src/px/` — guarantees binary compatibility with `rpict`
   output for validation.

## Phased roadmap

### Phase 0 — Environment and skeleton (prerequisite)
- Create `gpurad` repo, add Radiance fork as submodule.
- CMake that builds Radiance's static libs + an empty OptiX host exe.
- Dev environment: CUDA 12.x, OptiX 9 SDK, NVIDIA driver 535+, an
  RTX-class GPU (Ada or newer for full RT Core benefit).
- **Deliverable:** `grpict --version` prints.

### Phase 1 — Hello OptiX
- Minimal pipeline: raygen emits rays from a pinhole camera, miss
  program returns a gradient, closest-hit returns constant color.
- Render a single hard-coded triangle → PPM file.
- **Deliverable:** image of a triangle on a gradient background.

### Phase 2 — Scene loading
- Consume a real `.oct` file (Radiance's preprocessed octree).
- Walk the octree, extract polygons, build OptiX GAS.
- Hard-coded white diffuse material for everything.
- Reuse Radiance's view parser (`src/rt/srcsamp.c`, `viewfile()`)
  so `-vf` / `-vp` / `-vd` arguments behave identically.
- **Deliverable:** `grpict -vf view.vf scene.oct out.hdr` renders the
  Radiance test scenes (e.g. `cornell.oct`) with correct geometry
  and camera.

### Phase 3 — MVP material set → `grpict` parity
Port these material types as closest-hit programs (covers >90% of AEC
scenes):

| Material  | Notes                                           |
|-----------|-------------------------------------------------|
| `plastic` | Lambertian + Phong specular lobe                |
| `metal`   | Phong with colored specular = albedo            |
| `glass`   | Refractive with Radiance's attenuation semantics|
| `trans`   | Translucent diffuse (diffusers, fabric)         |
| `light`   | Emissive (area lights)                          |
| `mirror`  | Perfect reflector                               |
| `glow`    | Emissive without contribution to indirect       |

Plus modifiers required to drive them:
`colorpict`, `brightdata`, `texfunc`/`texdata`, `mixfunc`.
Direct illumination via source sampling (`src/rt/source.c` logic on
CPU side, shadow rays on GPU).

**Deliverable:** `grpict` produces visually indistinguishable output
from CPU `rpict` on the standard Radiance test suite (direct-only,
ambient value 0).

### Phase 4 — `grtrace` + numerical validation harness
- Sensor-grid ray generation (`raygen_trace.cu`): one ray per sensor
  point, write irradiance/illuminance to output.
- Binary-compatible output format with `rtrace -I+`.
- Validation suite: `test/validation/` contains N reference scenes
  with known CPU `rtrace` outputs. CI diffs GPU vs CPU per-point; fail
  if max relative error >5% or mean >1%.
- **Deliverable:** `grtrace` matches CPU `rtrace` within tolerance on
  the validation suite for direct illumination.

### Phase 5 — Ambient calculation / irradiance cache (the hard one)
This is the algorithmic core of Radiance and of Jones's thesis.

- CPU Radiance uses an adaptive spatial cache of indirect-irradiance
  samples, interpolated with Ward's gradient-based scheme.
- GPU port (per Jones) parallelises sample computation across warps,
  stores the cache in a GPU-friendly kd-tree or hash grid.
- **Algorithm sub-phases:**
  a. Hemispheric sampling at a single cache point (stratified)
  b. Gradient estimation (positional + rotational)
  c. Cache insertion + spatial query data structure
  d. Cache lookup + interpolation during shading
  e. Multi-bounce: recursive cache during sample computation
- Risk: this is months of work. Budget accordingly.
- **Deliverable:** `grpict -ab 2` matches CPU `rpict -ab 2` within
  tolerance on the validation suite. This is the "we built Radiance"
  moment.

### Phase 6 — Daylight coefficients + annual simulation
- Implement `rcontrib`-equivalent: matrix of (sensor × sky-patch)
  contribution coefficients, computed in one GPU pass.
- Combine with Perez sky vectors for 8760-hour annual metrics (sDA,
  ASE, UDI, DGP).
- **Deliverable:** produce LM-83 sDA compliance output from one scene
  in seconds, not hours.

### Phase 7 — Cloud deployment (follows StereoFlux cloud pattern)
- Docker image with CUDA 12 + OptiX 9 runtime + `gpurad` binaries.
- Deploy to RunPod Serverless or Modal (L40S / H100 / Blackwell).
- Orchestration API on Railway/Fly.io; scenes + results on
  Cloudflare R2.
- **Deliverable:** upload a `.rad`/`.oct` file, get an HDR back, via
  HTTPS.

## Critical risks & mitigations

| Risk                                                | Mitigation                                                                                               |
|-----------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| GPU irradiance cache is the algorithmic bottleneck  | Phase 5 is gated by Phases 1-4 working. Fall back to brute-force Monte Carlo as a baseline if IC stalls. |
| Radiance material BRDFs have subtle corner cases    | Validation harness in Phase 4 catches per-material divergence early.                                     |
| Radiance source is messy K&R-era C                  | Don't rewrite. Link as-is. Keep our code (C++17/20) separate from theirs.                                |
| OptiX 9 SBT and pipeline complexity                 | Start from `optixPathTracer` sample. Don't innovate on infrastructure.                                   |
| Driver/CUDA/OptiX version drift                     | Pin exact versions in Docker image for Phase 7; document host requirements.                              |
| Licensing                                           | Radiance uses a permissive custom licence (basically BSD-like). Confirm before publishing gpurad.        |

## Validation strategy (applies Phases 3+)

Every PR runs:

1. **Unit tests** — googletest on C++ host code (SBT packing, GAS
   builds, material param marshalling).
2. **Visual regression** — `grpict` output compared pixel-wise to
   stored references from CPU `rpict` on ~10 curated scenes.
3. **Numerical regression** — `grtrace` sensor-grid output compared
   per-point to CPU `rtrace` references, with per-metric tolerance
   bands (5% max, 1% mean).
4. **Cornell box reference** — standard benchmark; known analytical
   form factors; easy sanity check.

## First-week milestone (what "started" looks like)

- Repo created, Radiance submodule in, CMake builds both Radiance's
  static libs and a stub `grpict` binary.
- OptiX 9 sample `optixPathTracer` runs on target hardware.
- Single triangle rendered via our own pipeline → PPM file.

This is Phase 0 + half of Phase 1. No algorithmic work yet, but the
rig is real.

## Open questions

- **Hardware access:** do we have an RTX 40/50-series or L40S dev
  box, or do we need to provision cloud GPUs day 1?
- **Radiance licence confirmation:** need to read the exact terms
  before forking publicly.
- **Interoperability with pbr2rad:** should `grpict` ingest pbr2rad
  output folders directly (zero-config), or stay a pure Radiance
  consumer that relies on pbr2rad-generated `.rad` files?
- **Scene format:** accept `.rad` (text) and `.oct` (binary), or
  require `.oct` (preprocessed with `oconv`)? `.oct` is simpler to
  parse — single binary blob vs multi-file include chains.
- **Public release strategy:** open-source from day 1, or private
  until Phase 4 numerical parity is demonstrated?
- **Naming:** `gpurad` is a placeholder. Real name?

## Relationship to existing work in this repo

`pbr2rad` (already shipped in this repo) produces the material input
format `gpurad` consumes. They are complementary:

- `pbr2rad` → `/rad_materials/wood_floor_03/{wood_floor_03.rad, .cal, .hdr}`
- `gpurad` ingests that, together with scene geometry, and renders.

When `gpurad` moves to its own repo, these two tools form a matched
pair in the documentation and release story.
