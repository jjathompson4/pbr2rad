"""Optional Radiance render preview.

If ``rpict`` is on PATH, renders a low-res preview sphere with the
converted material.  Falls back gracefully if Radiance is not installed.

The render is auto-exposed from the sphere's own luminance distribution
(see ``auto_exposure``) and delivered as an RGBA PNG with a transparent
surround, so it sits cleanly on either UI theme.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("pbr2rad.web")

# ---------------------------------------------------------------------------
# Camera + scene constants. Straight-on at the equator, like the source
# sites' reference renders (ambientCG / Poly Haven): the camera sits on -Y
# aimed exactly at the origin, where the unit preview sphere sits, poles on Z.
# ``sphere_disk_radius`` derives the sphere's image-space silhouette from
# these, so keep them together. (Until 2026-08 this was a 3/4 view from
# (2.6, -2.9, 2.1); the distance is kept so the sphere fills the frame the
# same.) With this view the key light is upper-left-front and the fill
# right-front — the same highlight placement as the reference renders.
# ---------------------------------------------------------------------------
_CAM_VP = (0.0, -4.4249, 0.0)
_CAM_VD = (0.0, 1.0, 0.0)
_CAM_VU = (0.0, 0.0, 1.0)
_CAM_FOV_DEG = 27.5            # sphere fills ~95 % of the frame, like the source renders (~96 %)
_SPHERE_RADIUS = 1.0

# Light rig v7 (2026-08-23, image-based): the preview sphere sits inside a
# real photo studio — Poly Haven's CC0 "Studio Small 09" HDRI (see
# web/assets/README.md), used as a ``colorpict`` pattern on two ``glow``
# hemispheres. Everything in the scene is that one luminous environment, so
# chrome reflects actual softboxes / walls / floor, glossy materials pick up
# the same soft highlights the texture sites' renders show, and diffuse
# shading (from the ambient pass, ``-ab 1``) is as flat as theirs (top/bottom
# ≈ 1.1–1.2). No ``light`` primitives anywhere: Radiance zeroes specular rays
# that hit a ``light``, which is why the earlier disc rigs drew near-mirror
# metals with black discs.
#
# ENV_ROTATION_DEG turns the environment so its main softbox is upper-front-
# left (like the references' key); ENV_WHITE_BALANCE scales the three channels
# so the irradiance on a camera-facing surface is exactly neutral (the HDRI is
# slightly cool) — a grey card still renders grey. ``rig_radiance`` /
# ``rig_irradiance`` sample the same image, so the fixed exposure and the
# neutrality tests stay tied to what the renderer sees.
# Two rigs (2026-08-23, Jeff): plastics/dielectrics are previewed under the
# LIGHT rig (v5) — two broad `light` discs + a dim neutral glow surround —
# whose direct Gaussian highlights are crisp, noise-free and track the
# roughness slider; metals are previewed under the HDRI rig (v7) because a
# near-mirror needs a real environment to reflect (and `light` sources read
# as black discs in mirrors). ``rig_for(primitive)`` picks; each rig has its
# own irradiance model, exposure gain and calibration against the ambientCG
# reference spheres (see CHANGELOG).
RIG_LIGHTS = "lights"
RIG_HDRI = "hdri"

# Per-source exposure: the rig gains are calibrated against ambientCG's
# reference spheres (≈ 1.07 × albedo display-linear for neutral materials).
# Poly Haven renders its reference spheres much dimmer (oak_veneer_01
# ×0.48, plastered_wall_04 ×0.44 of their albedos, neutral across channels),
# so a Poly Haven material is previewed at this fraction of the ambientCG
# exposure to compare at a glance. Uploads / unknown sources use 1.0.
SOURCE_EXPOSURE_SCALE: dict[str, float] = {"polyhaven": 0.41}


def source_exposure_scale(source: str | None) -> float:
    return SOURCE_EXPOSURE_SCALE.get((source or "").lower(), 1.0)


def rig_for(primitive: str | None) -> str:
    """Preview rig for a converted material: metals → HDRI, everything else → lights."""
    return RIG_HDRI if (primitive or "").lower() == "metal" else RIG_LIGHTS


# Light rig (v5b, 2026-08-23): ONE ``light`` key disc — the texture sites'
# renders show a single large soft highlight upper-left — plus a broad ``glow``
# fill disc (fills the shadows through the ambient pass but, being glow, adds
# no second specular blob; with two ``light`` discs every glossy wood grew a
# second highlight on the right that the references don't have), and sky/env
# ``glow`` hemispheres. All grey, so a grey card renders grey
# (``rig_irradiance`` is unit-tested for that). Key direction chosen so the
# highlight lands where the references' does (upper-left, ≈ 0.4 R from centre).
#
# v5c — edge glow: the reference renders show a bright rim along the left and
# right limb on sheen materials (ambientCG Wood028 3–4× the sphere mean over
# the outer ~12 % of the radius, WoodFloor052 a thin line at the very edge,
# Poly Haven oak 2.5× both sides) and none on matte ones. That is the bright
# surroundings seen through the glossy lobe at grazing angles. Radiance's
# ``plastic`` only applies its Fresnel term to roughness-0 specular, ``glow``
# never enters the direct calculation and with -st 0.15 / 5 % specular no
# specular rays are traced — so the glow surround cannot make a rim; dim
# ``light`` discs BEHIND the sphere can: their highlight comes from the direct
# calculation (Gaussian lobe, peak ∝ ρs·L·Ω/(4π α² cos θi) → strong on glossy,
# nothing on matte, brighter at grazing incidence). A source β° from straight
# behind (+Y; the camera sits at −Y) is mirrored only by limb points at
# r/R ≈ 0.93 (β 54°) … 0.99 (β 29°), never by the face. Radiance evaluates a
# distant ``source`` at its centre only (no partitioning, no lobe widening on a
# curved surface), so each disc gives a dash whose length is set by the
# material's lobe; a short arc of small discs per side makes the band the
# references show. The discs face away from the camera-facing normal, so the
# fixed exposure (and the calibrated gain) is untouched.
KEY_RADIANCE = (1.6, 1.6, 1.6)
KEY_DIR = (-0.64, -0.30, 0.72)
KEY_ANGLE_DEG = 50.0
FILL_RADIANCE = (0.7, 0.7, 0.7)         # glow disc (no direct highlight)
FILL_DIR = (0.7, -0.5, 0.3)
FILL_ANGLE_DEG = 70.0
SKY_RADIANCE = (0.10, 0.10, 0.10)       # +z hemisphere (glow)
ENV_RADIANCE = (0.10, 0.10, 0.10)       # -z hemisphere (glow) — equal to the sky: no horizon line in reflections
LIGHTS_ALBEDO_GAIN = 1.21               # calibrated: neutral diffuse materials match the ambientCG references
RIM_RADIANCE = (4.14, 4.14, 4.14)       # edge-glow discs (light): grey, E = L·Ω ≈ 0.025 each, ≈ 0.84 per side
RIM_ANGLE_DEG = 5.0                     # small: sampled at the centre anyway; blocks little glow sky
RIM_BETA_DEG = 44.0                     # angle from straight behind (+Y) → rim peak at r/R ≈ 0.965
RIM_ELEVATIONS_DEG = tuple(float(e) for e in range(-40, 61, 3))   # 34 discs per side, 3° apart: continuous down to roughness
                                        # ≈ 0.15 (2° was continuous to 0.12 but cost ~1 s more per render)


def rim_directions() -> list[tuple[float, float, float]]:
    """Unit directions of the edge-glow discs: left side first, then right,
    each at ``RIM_BETA_DEG`` from +Y (behind the sphere) and spread along the
    limb by ``RIM_ELEVATIONS_DEG``."""
    beta = math.radians(RIM_BETA_DEG)
    out: list[tuple[float, float, float]] = []
    for side in (-1.0, 1.0):
        for e_deg in RIM_ELEVATIONS_DEG:
            e = math.radians(e_deg)
            out.append((side * math.sin(beta) * math.cos(e), math.cos(beta), math.sin(beta) * math.sin(e)))
    return out

# HDRI rig (v7) settings
SUPERSAMPLE = 2                 # rpict at 2x, pfilt -r 0.6 reduce: averages glossy-sampling speckle
SPECULAR_THRESHOLD = 0.02       # rpict -st: sample specular lobes down to 2 % (plastics are 5 %)
SPECULAR_SAMPLES = 8            # rpict -ss: lobe samples per ray
# Lights rig: sample the specular lobe only for GLOSSY materials. Folding
# (rpict's default -st 0.15) turns sub-threshold specular into an isotropic
# veil L = ρs·E_amb/π that swamps dark glossy woods (Wood028, albedo luma
# 0.013: the veil was 3.5× the wood's own diffuse — grey fog). Sampling fixes
# that — but on very rough, strongly normal-mapped materials (Fabric030,
# roughness 0.73) the wide sampled lobe re-enters the sphere and the render
# explodes (49 s at 192 px). At high roughness the wide lobe reflects the
# surround nearly isotropically anyway, so folding is a good approximation
# exactly where sampling is pathological: sample below this roughness, fold
# above it.
SPECULAR_SAMPLE_MAX_ROUGHNESS = 0.6


def samples_specular(rig: str, roughness: float | None) -> bool:
    """Whether a render samples the specular lobe (vs folding it into the
    ambient): the HDRI rig always does; the light rig only for glossy
    materials (see ``SPECULAR_SAMPLE_MAX_ROUGHNESS``; unknown roughness folds)."""
    if rig == RIG_HDRI:
        return True
    return roughness is not None and roughness <= SPECULAR_SAMPLE_MAX_ROUGHNESS
HDRI_ALBEDO_GAIN = 1.15         # calibrated (studio HDRI, sampled specular)
ENV_HDR_NAME = "studio_small_09_512.hdr"
ENV_HDR_DIR = Path(__file__).resolve().parent / "assets"
ENV_ROTATION_DEG = 330.0
ENV_WHITE_BALANCE = (1.0176, 1.0107, 0.9729)   # channel gains (see test_preview)

# ---------------------------------------------------------------------------
# Exposure. Default is FIXED, derived from the rig, so brightness encodes
# reflectance the way the source renders do (white tiles bright, dark fabric
# dark): a camera-facing diffuse surface with albedo a lands at
# EXPOSURE_ALBEDO_GAIN × a in display-linear before ra_bmp's 2.2 gamma.
# PBR2RAD_PREVIEW_EXPOSURE=auto re-enables the per-render percentile
# exposure (useful for diagnosing very dark materials; not comparable
# across materials). Targets are display-linear.
# ---------------------------------------------------------------------------
EXPOSURE_ALBEDO_GAIN = 1.15    # default gain (HDRI rig); the light rig uses LIGHTS_ALBEDO_GAIN — see fixed_exposure(rig)
EXPOSURE_FALLBACK = 2 ** 1.4   # the fixed "+1.4 stops" used before round 3
EXPOSURE_T_MID = 0.32          # auto mode: median sphere luminance → ~0.6 after gamma
EXPOSURE_T_HI = 0.94           # auto mode: 95th percentile → ~0.97; the top 5% may clip
EXPOSURE_MIN = 0.5
EXPOSURE_MAX = 32.0
_MASK_SHRINK = 0.92            # measure inside the rim, away from AA edge pixels
_EDGE_FEATHER_PX = 1.5         # alpha ramp width around the silhouette

# Display-only "look": the source sites' sphere renders carry a mild
# saturation boost over the albedo itself (~1.35–1.5× on wood/brick, none on
# neutrals). A modest boost on the preview PNG keeps our render comparable
# without touching the .rad/.hdr or the readout. Neutrals are unaffected.
# Luminance-aware (2026-08-22): the references behave like a filmic view
# transform — MORE saturated than a flat boost in shadows/midtones (bricks
# 0.66 vs our 0.58, wood 0.66 vs 0.44 at luma 0.3–0.5) and LESS in the
# highlights (tiles 0.03 vs 0.06 at luma > 0.85, where a uniform boost left a
# warm cast on near-whites). So the factor ramps from PREVIEW_SATURATION at
# luma ≤ RAMP[0] to PREVIEW_SATURATION_HI at luma ≥ RAMP[1]; evaluated against
# the 7 references a 1.40→0.75 ramp halved the per-luminance-bin error of the
# old uniform 1.25 (0.089 → 0.070); the shipped values below are milder by
# Jeff's preference. Luma (Rec.601) is preserved, so neutrals and brightness
# are untouched.
# Jeff (2026-08-22/23): a uniform 1.25 read too strong (it warmed near-
# whites), 1.10 starved the woods, 1.35 made polished woods orange. Swept
# against nine references (four woods): the woods disagree among themselves
# (their polished ones carry a Fresnel sheen a Radiance plastic lacks), and
# 1.25 → 0.80 is the balanced point (mean-colour error 0.059 vs 0.086 at 1.0).
PREVIEW_SATURATION = 1.25          # shadows / midtones
PREVIEW_SATURATION_HI = 0.80       # at white (highlight protection)
PREVIEW_SATURATION_RAMP = (0.40, 0.95)


@functools.lru_cache(maxsize=1)
def radiance_available() -> bool:
    """Return True if Radiance tools are on PATH.

    Cached: the answer can't change within one process (the deployment bakes
    Radiance into the image), and this sits on the health-check hot path.
    """
    return shutil.which("rpict") is not None and shutil.which("oconv") is not None


def _unit(v) -> tuple[float, float, float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


_ENV_IMAGE = None


def env_image() -> np.ndarray:
    """The environment HDRI as (H, W, 3) linear radiance (cached, raw — the
    white balance is applied in ``rig_radiance`` and in the scene)."""
    global _ENV_IMAGE
    if _ENV_IMAGE is None:
        from .. import hdr as hdr_mod
        _ENV_IMAGE = hdr_mod.read_hdr(ENV_HDR_DIR / ENV_HDR_NAME)
    return _ENV_IMAGE


def rig_radiance(dirs):
    """Radiance (N,3) seen along unit directions ``dirs`` (N,3): the rotated,
    white-balanced environment — the same lookup the scene's .cal performs
    (equirectangular, +Z up, row 0 = zenith). Used by ``rig_irradiance``."""
    img = env_image()
    h, w, _ = img.shape
    d = np.asarray(dirs, dtype=float)
    phi = np.arctan2(d[:, 1], d[:, 0]) + math.radians(ENV_ROTATION_DEG)
    u = (phi / (2.0 * math.pi)) % 1.0
    v = 0.5 - np.arcsin(np.clip(d[:, 2], -1.0, 1.0)) / math.pi
    xi = np.clip((u * w).astype(int), 0, w - 1)
    yi = np.clip((v * h).astype(int), 0, h - 1)
    return img[yi, xi] * np.asarray(ENV_WHITE_BALANCE)


_RIG_GRID = None


def _rig_grid():
    """Direction grid over the whole sphere with solid-angle weights (cached)."""
    global _RIG_GRID
    if _RIG_GRID is None:
        th = np.radians(np.arange(0.75, 180.0, 1.5))
        ph = np.radians(np.arange(0.75, 360.0, 1.5))
        TH, PH = np.meshgrid(th, ph, indexing="ij")
        dirs = np.stack([np.sin(TH) * np.cos(PH), np.sin(TH) * np.sin(PH), np.cos(TH)], axis=-1).reshape(-1, 3)
        dw = (np.sin(TH) * math.radians(1.5) ** 2).reshape(-1)
        _RIG_GRID = (dirs, dw)
    return _RIG_GRID


def lights_irradiance(normal) -> tuple[float, float, float]:
    """Light rig: irradiance on a Lambertian surface — disc sources as
    L·Ω·cosθ, glow hemispheres as L·π·(1+cosθ)/2 (analytic)."""
    n = _unit(normal)

    def disc(radiance, direction, full_angle_deg):
        d = _unit(direction)
        omega = math.pi * math.sin(math.radians(full_angle_deg / 2.0)) ** 2
        cos = max(0.0, sum(a * b for a, b in zip(n, d)))
        return [c * omega * cos for c in radiance]

    def hemi(radiance, axis):
        a = _unit(axis)
        cos = sum(x * y for x, y in zip(n, a))
        return [c * math.pi * max(0.0, (1.0 + cos) / 2.0) for c in radiance]

    parts = [
        disc(KEY_RADIANCE, KEY_DIR, KEY_ANGLE_DEG),
        disc(FILL_RADIANCE, FILL_DIR, FILL_ANGLE_DEG),
        hemi(SKY_RADIANCE, (0.0, 0.0, 1.0)),
        hemi(ENV_RADIANCE, (0.0, 0.0, -1.0)),
        *[disc(RIM_RADIANCE, d, RIM_ANGLE_DEG) for d in rim_directions()],
    ]
    return tuple(sum(p[i] for p in parts) for i in range(3))


def hdri_irradiance(normal) -> tuple[float, float, float]:
    """HDRI rig: ∫ L(ω) max(0, n·ω) dω over the environment (numeric)."""
    n = np.asarray(_unit(normal))
    dirs, dw = _rig_grid()
    cos = np.clip(dirs @ n, 0.0, None)
    L = rig_radiance(dirs)
    e = (L * (cos * dw)[:, None]).sum(axis=0)
    return (float(e[0]), float(e[1]), float(e[2]))


def rig_irradiance(normal, rig: str = RIG_HDRI) -> tuple[float, float, float]:
    """Irradiance (R, G, B) a rig delivers to a Lambertian surface with the
    given normal. Used for the fixed exposure and the neutrality tests."""
    return lights_irradiance(normal) if rig == RIG_LIGHTS else hdri_irradiance(normal)


def rig_neutrality(normal, rig: str = RIG_HDRI) -> float:
    """max/min channel ratio of the rig's irradiance (1.0 = perfectly neutral)."""
    e = rig_irradiance(normal, rig)
    lo = min(e)
    return (max(e) / lo) if lo > 0 else float("inf")


def _camera_facing_normal() -> tuple[float, float, float]:
    return _unit(tuple(-c for c in _CAM_VD))


def fixed_exposure(rig: str = RIG_HDRI, source: str | None = None) -> float:
    """Exposure multiplier so that a camera-facing diffuse surface of albedo
    ``a`` renders at ``gain × a`` (display-linear) under ``rig``: radiance is
    a·E/π, so k = gain·π/E using the rig's photopic irradiance — times the
    source's reference-exposure factor (``SOURCE_EXPOSURE_SCALE``)."""
    e = rig_irradiance(_camera_facing_normal(), rig)
    e_vis = 0.265 * e[0] + 0.670 * e[1] + 0.065 * e[2]
    if e_vis <= 0:
        return EXPOSURE_FALLBACK
    gain = LIGHTS_ALBEDO_GAIN if rig == RIG_LIGHTS else HDRI_ALBEDO_GAIN
    return gain * math.pi / e_vis * source_exposure_scale(source)


def exposure_mode() -> str:
    """``"fixed"`` (default) or ``"auto"`` via $PBR2RAD_PREVIEW_EXPOSURE."""
    return "auto" if os.environ.get("PBR2RAD_PREVIEW_EXPOSURE", "").lower() == "auto" else "fixed"


def apply_look(
    img,
    saturation: float = PREVIEW_SATURATION,
    *,
    saturation_hi: float = PREVIEW_SATURATION_HI,
    ramp: tuple[float, float] = PREVIEW_SATURATION_RAMP,
):
    """Display-only look for the preview PNG (RGBA): luminance-aware
    saturation — ``saturation`` in shadows/midtones (luma ≤ ramp[0]),
    ``saturation_hi`` at white (luma ≥ ramp[1]), linear in between.

    Blends each pixel with its Rec.601 luma (what PIL's ImageEnhance.Color
    does, but with a per-pixel factor), so greys stay grey, luma is preserved
    and alpha is kept. ``saturation == saturation_hi == 1`` is a no-op.
    """
    from PIL import Image

    if abs(saturation - 1.0) < 1e-6 and abs(saturation_hi - 1.0) < 1e-6:
        return img
    rgba = img.convert("RGBA")
    arr = np.asarray(rgba).astype(np.float32)
    rgb = arr[..., :3]
    luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    lo, hi = ramp
    t = np.clip((luma / 255.0 - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    k = saturation + (saturation_hi - saturation) * t
    grey = luma[..., None]
    out = np.clip(grey + k[..., None] * (rgb - grey), 0, 255)
    result = np.concatenate([out, arr[..., 3:4]], axis=-1).astype(np.uint8)
    return Image.fromarray(result, "RGBA")


def sphere_disk_radius(size: int) -> float:
    """Image-space radius (px) of the unit sphere's silhouette in a size² render."""
    dist = math.sqrt(sum(c * c for c in _CAM_VP))
    angular = math.asin(_SPHERE_RADIUS / dist)
    half = size / 2.0
    focal = half / math.tan(math.radians(_CAM_FOV_DEG / 2.0))
    return focal * math.tan(angular)


def _radial_distance(size: int) -> np.ndarray:
    c = (size - 1) / 2.0
    yy, xx = np.ogrid[:size, :size]
    return np.sqrt((xx - c) ** 2 + (yy - c) ** 2)


def sphere_mask(size: int, *, shrink: float = _MASK_SHRINK) -> np.ndarray:
    """Boolean (size, size) mask of pixels safely inside the sphere silhouette."""
    return _radial_distance(size) <= sphere_disk_radius(size) * shrink


def sphere_alpha(size: int, *, feather: float = _EDGE_FEATHER_PX) -> np.ndarray:
    """uint8 (size, size) alpha: opaque inside the silhouette, feathered rim,
    transparent outside — this is what makes the preview theme-proof."""
    r = sphere_disk_radius(size)
    d = _radial_distance(size)
    a = np.clip((r + feather / 2.0 - d) / feather, 0.0, 1.0)
    return np.round(a * 255.0).astype(np.uint8)


def auto_exposure(lum) -> float:
    """Exposure multiplier for a set of sphere luminance samples.

    Brings the median to a readable mid-tone while keeping the 95th
    percentile below clipping; the brighter of the two constraints wins so
    specular highlights don't hold dark materials down. Clamped, and falls
    back to the legacy fixed exposure on empty/degenerate input.
    """
    arr = np.asarray(lum, dtype=np.float64).ravel()
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return EXPOSURE_FALLBACK
    median = float(np.median(arr))
    hi = float(np.percentile(arr, 95))
    candidates = []
    if median > 0:
        candidates.append(EXPOSURE_T_MID / median)
    if hi > 0:
        candidates.append(EXPOSURE_T_HI / hi)
    if not candidates:
        return EXPOSURE_FALLBACK
    ev = min(candidates)
    return float(min(EXPOSURE_MAX, max(EXPOSURE_MIN, ev)))


def _read_luminance(hdr: Path, size: int, env: dict) -> np.ndarray | None:
    """Per-pixel luminance of ``hdr`` via ``pvalue``; None if anything fails."""
    try:
        out = subprocess.run(
            ["pvalue", "-h", "-H", "-d", "-b", str(hdr)],
            capture_output=True, check=True, timeout=30, env=env,
        ).stdout
        arr = np.array(out.decode("ascii", "replace").split(), dtype=np.float64)
    except Exception:
        log.warning("pvalue failed for %s; using fixed exposure", hdr.name, exc_info=True)
        return None
    if arr.size != size * size:
        log.warning("pvalue returned %d samples for %dx%d", arr.size, size, size)
        return None
    return arr.reshape(size, size)


RIG_CAL_NAME = "preview.rig.cal"


def _rig_cal_text() -> str:
    """Function file for the environment lookup: equirectangular picture
    coordinates from the ray direction (A1 = rotation in radians; the picture
    is 2:1 so ``env_u`` spans 0..2), plus the white-balance channel functions
    (A2..A4 = gains)."""
    return (
        "{ pbr2rad preview rig v7: equirectangular environment lookup }\n"
        "phi = atan2(Dy, Dx) + A1;\n"
        "env_u = 2 * mod(phi / (2*PI), 1);\n"
        "env_v = 0.5 + asin(Dz) / PI;\n"
        "wb_r(r, g, b) = A2 * r;\n"
        "wb_g(r, g, b) = A3 * g;\n"
        "wb_b(r, g, b) = A4 * b;\n"
    )


def _scene_text_lights(name: str) -> str:
    """Light rig: unit sphere, one ``light`` key disc (crisp, roughness-
    tracking highlight from the direct calculation), dim ``light`` edge-glow
    discs behind the sphere (rim on sheen materials only), a broad ``glow``
    fill disc and sky + env ``glow`` hemispheres (seen by reflections, sampled
    by the ambient pass)."""
    def rgb(c):
        return f"{c[0]:g} {c[1]:g} {c[2]:g}"

    def xyz(v):
        return f"{v[0]:.4f} {v[1]:.4f} {v[2]:.4f}"

    rims = "".join(
        f"rim_l source rim{i + 1}\n0\n0\n4 {xyz(d)} {RIM_ANGLE_DEG:g}\n\n"
        for i, d in enumerate(rim_directions())
    )
    return (
        f"{name} sphere ball\n0\n0\n4 0 0 0 {_SPHERE_RADIUS:g}\n\n"
        f"void light key_l\n0\n0\n3 {rgb(KEY_RADIANCE)}\n\n"
        f"key_l source key\n0\n0\n4 {xyz(KEY_DIR)} {KEY_ANGLE_DEG:g}\n\n"
        f"void light rim_l\n0\n0\n3 {rgb(RIM_RADIANCE)}\n\n"
        f"{rims}"
        f"void glow fill_g\n0\n0\n4 {rgb(FILL_RADIANCE)} 0\n\n"
        f"fill_g source fill\n0\n0\n4 {xyz(FILL_DIR)} {FILL_ANGLE_DEG:g}\n\n"
        f"void glow sky_g\n0\n0\n4 {rgb(SKY_RADIANCE)} 0\n\n"
        "sky_g source sky\n0\n0\n4 0 0 1 180\n\n"
        f"void glow env_g\n0\n0\n4 {rgb(ENV_RADIANCE)} 0\n\n"
        "env_g source env\n0\n0\n4 0 0 -1 180\n"
    )


def _scene_text(name: str, rig: str = RIG_HDRI) -> str:
    """The preview scene for ``rig``: the light rig, or the unit sphere inside
    the studio HDRI (the picture as a colorpict pattern on a glow, on two
    distant hemispherical sources)."""
    if rig == RIG_LIGHTS:
        return _scene_text_lights(name)
    wb = ENV_WHITE_BALANCE
    return (
        f"{name} sphere ball\n0\n0\n4 0 0 0 {_SPHERE_RADIUS:g}\n\n"
        f"void colorpict envpic\n7 wb_r wb_g wb_b {ENV_HDR_NAME} {RIG_CAL_NAME} env_u env_v\n0\n"
        f"4 {math.radians(ENV_ROTATION_DEG):.6f} {wb[0]:.4f} {wb[1]:.4f} {wb[2]:.4f}\n\n"
        "envpic glow env_g\n0\n0\n4 1 1 1 0\n\n"
        "env_g source env_up\n0\n0\n4 0 0 1 180\n\n"
        "env_g source env_dn\n0\n0\n4 0 0 -1 180\n"
    )


def render_preview(
    mat_dir: Path,
    rad_file: Path,
    output_png: Path,
    *,
    size: int = 384,
    rig: str = RIG_LIGHTS,
    source: str | None = None,
    roughness: float | None = None,
) -> bool:
    """Render a preview sphere with the given material.

    ``roughness`` (the material's mean perceptual roughness) decides whether
    the lights rig samples the specular lobe or folds it — see
    ``SPECULAR_SAMPLE_MAX_ROUGHNESS``. Returns True on success, False on failure.
    """
    if not radiance_available():
        return False

    work = output_png.parent
    name = rad_file.stem

    # Scene: a unit sphere with the material under test, lit by key + fill
    # discs, a sky dome and a dim lower-hemisphere environment. The web app
    # normally passes the ``preview_<name>.rad`` reference-wrap chain (the
    # texture wrapped like the source sites' spheres); with the exported
    # box/triplanar chain the sphere's continuously varying normal instead
    # reveals the three soft projection "seams" along the world-axis great
    # circles. (Named ``preview.scene.rad`` so it can't collide with a
    # ``preview_<name>`` variant of a material called ``scene``.)
    scene_rad = work / "preview.scene.rad"
    scene_rad.write_text(_scene_text(name, rig), encoding="ascii")
    (work / RIG_CAL_NAME).write_text(_rig_cal_text(), encoding="ascii")
    # 2× supersampling (pfilt -r 0.6 reduce) wherever the specular lobe is
    # sampled — sampling speckles; folded renders are deterministic and stay 1×.
    sample_spec = samples_specular(rig, roughness)
    ss = SUPERSAMPLE if sample_spec else 1
    ambient = (
        ["-ab", "1", "-aa", "0.12", "-ad", "1024", "-as", "512",
         "-st", f"{SPECULAR_THRESHOLD:g}", "-ss", f"{SPECULAR_SAMPLES:g}"]
        if rig == RIG_HDRI else
        ["-ab", "2", "-aa", "0.1", "-ad", "1024", "-as", "512",
         # -dt/-dc stay at rpict's defaults: forcing -dt 0 made every ambient
         # self-hit test all the rim sources (Fabric030: 11 s vs 1.5 s).
         *(["-st", f"{SPECULAR_THRESHOLD:g}", "-ss", f"{SPECULAR_SAMPLES:g}"]
           if sample_spec else [])]
    )

    env = {
        **os.environ,
        # material dir (hdr/cal/dat) + work dir (scene + rig .cal) + the
        # environment HDRI + Radiance lib
        "RAYPATH": f"{mat_dir}:{work}:{ENV_HDR_DIR}:.:{os.environ.get('RAYPATH', '/usr/local/radiance/lib')}",
    }

    octree = work / "preview.oct"
    hdr = work / "preview.hdr"

    try:
        # Compile
        with open(octree, "wb") as f:
            subprocess.run(
                ["oconv", str(rad_file), str(scene_rad)],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=30, env=env,
            )

        # Render
        with open(hdr, "wb") as f:
            subprocess.run(
                [
                    "rpict",
                    # Straight-on view aimed at the origin; distance and FOV
                    # put the unit sphere at ~95 % of the frame width.
                    "-vp", *(f"{c:g}" for c in _CAM_VP),
                    "-vd", *(f"{c:g}" for c in _CAM_VD),
                    "-vu", *(f"{c:g}" for c in _CAM_VU),
                    "-vh", f"{_CAM_FOV_DEG:g}", "-vv", f"{_CAM_FOV_DEG:g}",
                    # Rendered at 2x and reduced by pfilt (Gaussian): averages
                    # the glossy-sampling speckle of the HDRI softboxes.
                    "-x", str(size * ss), "-y", str(size * ss),
                    # Light rig: -ab 2 over a dim glow surround (direct
                    # highlights from the light discs). HDRI rig: all light is
                    # the glow environment seen through the ambient pass, the
                    # specular lobe is sampled against it (-st/-ss; Radiance's
                    # default -st 0.15 would fold plastics' 5 % into diffuse),
                    # and it renders at 2x for a Gaussian pfilt reduce.
                    *ambient,
                    # Adaptive pixel sampling: sample every 2nd pixel and
                    # refine where neighbours disagree (-pt 0.05 default).
                    # Visually identical to -ps 1 here (textures force
                    # refinement) but the 68-source direct loop then runs on
                    # far fewer points: Fabric030 11.3 s → 1.7 s.
                    "-ps", "2",
                    str(octree),
                ],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=60, env=env,
            )

        # Exposure: fixed (derived from the rig, comparable across materials)
        # unless auto mode is requested; auto measures the sphere's own pixels.
        if exposure_mode() == "auto":
            exposure = EXPOSURE_FALLBACK
            lum = _read_luminance(hdr, size * ss, env)
            if lum is not None:
                exposure = auto_exposure(lum[sphere_mask(size * ss)])
        else:
            exposure = fixed_exposure(rig, source)
        log.debug("preview exposure for %s: %.3g (%s, rig %s)", name, exposure, exposure_mode(), rig)

        # Convert to PNG via pfilt + ra_bmp + Pillow
        filtered = work / "preview_filt.hdr"
        bmp = work / "preview.bmp"
        with open(filtered, "wb") as f:
            subprocess.run(
                ["pfilt", "-1", "-e", f"{exposure:.4g}",
                 *(["-x", f"/{ss}", "-y", f"/{ss}", "-r", "0.6"] if ss > 1 else []),
                 str(hdr)],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=30, env=env,
            )
        subprocess.run(
            ["ra_bmp", str(filtered), str(bmp)],
            stderr=subprocess.PIPE, check=True, timeout=30, env=env,
        )

        from PIL import Image
        with Image.open(bmp) as img:
            rgba = img.convert("RGBA")
            if rgba.size == (size, size):
                rgba.putalpha(Image.fromarray(sphere_alpha(size), mode="L"))
            rgba = apply_look(rgba)
            rgba.save(output_png)
        return True

    except subprocess.TimeoutExpired as exc:
        log.warning("preview render timed out (%s) for %s", exc.cmd[0], rad_file.name)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace").strip()
        log.warning(
            "preview render failed (%s, exit %d) for %s: %s",
            exc.cmd[0], exc.returncode, rad_file.name, stderr[-500:],
        )
        return False
    except Exception:
        log.exception("preview render failed for %s", rad_file.name)
        return False
