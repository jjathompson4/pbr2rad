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
# Camera + scene constants. The 3/4 view is aimed exactly at the origin, where
# the unit preview sphere sits — ``sphere_disk_radius`` derives the sphere's
# image-space silhouette from these, so keep them together.
# ---------------------------------------------------------------------------
_CAM_VP = (2.6, -2.9, 2.1)
_CAM_VD = (-0.584, 0.652, -0.472)
_CAM_VU = (0.0, 0.0, 1.0)
_CAM_FOV_DEG = 27.5            # sphere fills ~95 % of the frame, like the source renders (~96 %)
_SPHERE_RADIUS = 1.0

# Light rig — deliberately NEUTRAL (R = G = B for every light) so that a grey
# card renders grey and a material's own colour is what the user sees. The
# previous rig (warm key, cool fill, blue sky) integrated to an irradiance of
# roughly (0.81, 1.00, 1.37) on any surface — every preview had a blue cast
# and warm materials read as desaturated grey. Shapes are kept: a key from
# upper-front-left (now a larger, softer disc so highlights aren't a hot
# pin-point), a broad fill from the right, a sky hemisphere above, and a dim
# glow hemisphere below for reflections/ambient (a distant ``source`` so it
# can't shadow the lights; it also forms the background, masked out later).
# ``rig_irradiance`` computes the rig's colour balance and is unit-tested.
# v4 (round 5): two broad soft discs (``light`` — they drive the direct
# calculation) in a dim neutral surround made of two ``glow`` hemispheres
# (sky above, env below). Glow is the right primitive for the surround: it is
# what specular rays see (so chrome reflects a sky and an env instead of
# black — ``light`` sources are invisible to reflected rays) and the ambient
# pass picks it up for diffuse fill, exactly how gensky skies work. The
# source renders are lit by large soft lights, so diffuse shading is flat
# (top/bottom ≈ 1.2) and chrome reads as a dark body with soft reflections.
# Calibrated against ambientCG reference spheres (see CHANGELOG).
KEY_RADIANCE = (2.4, 2.4, 2.4)
KEY_DIR = (-0.6, -0.6, 0.8)
KEY_ANGLE_DEG = 40.0
FILL_RADIANCE = (1.4, 1.4, 1.4)
FILL_DIR = (0.7, -0.5, 0.3)
FILL_ANGLE_DEG = 50.0
SKY_RADIANCE = (0.10, 0.10, 0.10)       # +z hemisphere (glow)
ENV_RADIANCE = (0.06, 0.06, 0.06)       # -z hemisphere (glow)

# ---------------------------------------------------------------------------
# Exposure. Default is FIXED, derived from the rig, so brightness encodes
# reflectance the way the source renders do (white tiles bright, dark fabric
# dark): a camera-facing diffuse surface with albedo a lands at
# EXPOSURE_ALBEDO_GAIN × a in display-linear before ra_bmp's 2.2 gamma.
# PBR2RAD_PREVIEW_EXPOSURE=auto re-enables the per-render percentile
# exposure (useful for diagnosing very dark materials; not comparable
# across materials). Targets are display-linear.
# ---------------------------------------------------------------------------
EXPOSURE_ALBEDO_GAIN = 1.30    # calibrated: ambientCG references sit ≈ 1.25–1.35 × albedo (display-linear)
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
PREVIEW_SATURATION = 1.25


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


def rig_irradiance(normal) -> tuple[float, float, float]:
    """Approximate irradiance (R, G, B) the rig delivers to a Lambertian
    surface with the given normal — disc sources as L·Ω·cosθ, hemispheres as
    L·π·(1+cosθ)/2. Used to keep the rig white-balanced (see tests)."""
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
    ]
    return tuple(sum(p[i] for p in parts) for i in range(3))


def rig_neutrality(normal) -> float:
    """max/min channel ratio of the rig's irradiance (1.0 = perfectly neutral)."""
    e = rig_irradiance(normal)
    lo = min(e)
    return (max(e) / lo) if lo > 0 else float("inf")


def _camera_facing_normal() -> tuple[float, float, float]:
    return _unit(tuple(-c for c in _CAM_VD))


def fixed_exposure() -> float:
    """Exposure multiplier so that a camera-facing diffuse surface of albedo
    ``a`` renders at ``EXPOSURE_ALBEDO_GAIN × a`` (display-linear): radiance is
    a·E/π, so k = gain·π/E using the rig's photopic irradiance."""
    e = rig_irradiance(_camera_facing_normal())
    e_vis = 0.265 * e[0] + 0.670 * e[1] + 0.065 * e[2]
    if e_vis <= 0:
        return EXPOSURE_FALLBACK
    return EXPOSURE_ALBEDO_GAIN * math.pi / e_vis


def exposure_mode() -> str:
    """``"fixed"`` (default) or ``"auto"`` via $PBR2RAD_PREVIEW_EXPOSURE."""
    return "auto" if os.environ.get("PBR2RAD_PREVIEW_EXPOSURE", "").lower() == "auto" else "fixed"


def apply_look(img, saturation: float = PREVIEW_SATURATION):
    """Display-only look for the preview PNG (RGBA): mild saturation boost.

    Operates on colour only (PIL ImageEnhance.Color blends with the luma
    image), so greys stay grey and alpha is preserved.
    """
    from PIL import Image, ImageEnhance

    if abs(saturation - 1.0) < 1e-6:
        return img
    rgba = img.convert("RGBA")
    rgb = rgba.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(saturation)
    out = rgb.convert("RGBA")
    out.putalpha(rgba.getchannel("A"))
    return out


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


def _scene_text(name: str) -> str:
    """The preview scene: unit sphere, key + fill discs, sky + env hemispheres."""
    def rgb(c):
        return f"{c[0]:g} {c[1]:g} {c[2]:g}"

    def xyz(v):
        return f"{v[0]:g} {v[1]:g} {v[2]:g}"

    return (
        f"{name} sphere ball\n0\n0\n4 0 0 0 {_SPHERE_RADIUS:g}\n\n"
        # Key light (upper-front-left) - main shading source, broad soft disc
        f"void light key_l\n0\n0\n3 {rgb(KEY_RADIANCE)}\n\n"
        f"key_l source key\n0\n0\n4 {xyz(KEY_DIR)} {KEY_ANGLE_DEG:g}\n\n"
        # Fill light (front-right) - broad, softens shadows
        f"void light fill_l\n0\n0\n3 {rgb(FILL_RADIANCE)}\n\n"
        f"fill_l source fill\n0\n0\n4 {xyz(FILL_DIR)} {FILL_ANGLE_DEG:g}\n\n"
        # Sky hemisphere - dim neutral glow: seen by reflections, lights the
        # diffuse via the ambient pass (like a gensky sky), never shadows.
        f"void glow sky_g\n0\n0\n4 {rgb(SKY_RADIANCE)} 0\n\n"
        "sky_g source sky\n0\n0\n4 0 0 1 180\n\n"
        # Lower hemisphere: dim neutral glow for reflections / ambient only
        f"void glow env_g\n0\n0\n4 {rgb(ENV_RADIANCE)} 0\n\n"
        "env_g source env\n0\n0\n4 0 0 -1 180\n"
    )


def render_preview(
    mat_dir: Path,
    rad_file: Path,
    output_png: Path,
    *,
    size: int = 384,
) -> bool:
    """Render a preview sphere with the given material.

    Returns True on success, False on failure.
    """
    if not radiance_available():
        return False

    work = output_png.parent
    name = rad_file.stem

    # Scene: a unit sphere with the material under test, lit by three-point
    # studio lighting + sky dome + a dim lower-hemisphere environment. The
    # sphere's continuously varying normal reveals how the box/triplanar
    # .cal switches between projection axes (visible as three soft "seams"
    # along the world-axis great circles).
    scene_rad = work / "preview_scene.rad"
    scene_rad.write_text(_scene_text(name), encoding="ascii")

    env = {
        **os.environ,
        "RAYPATH": f"{mat_dir}:.:{os.environ.get('RAYPATH', '/usr/local/radiance/lib')}",
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
                    # 3/4 view aimed exactly at the origin; distance and FOV
                    # put the unit sphere at ~60% of the frame width.
                    "-vp", *(f"{c:g}" for c in _CAM_VP),
                    "-vd", *(f"{c:g}" for c in _CAM_VD),
                    "-vu", *(f"{c:g}" for c in _CAM_VU),
                    "-vh", f"{_CAM_FOV_DEG:g}", "-vv", f"{_CAM_FOV_DEG:g}",
                    "-x", str(size), "-y", str(size),
                    # Ambient settings tuned for a shared vCPU: good enough
                    # for a thumbnail, several times faster than the old
                    # -ab 3 / -ad 1024 / -as 512 settings which routinely
                    # approached the timeout on small hosts.
                    "-ab", "2",          # ambient bounces
                    "-aa", "0.1",        # ambient accuracy
                    "-ad", "512",        # ambient divisions
                    "-as", "256",        # ambient super-samples
                    "-ps", "1",          # no pixel sub-sampling
                    str(octree),
                ],
                stdout=f, stderr=subprocess.PIPE,
                check=True, timeout=60, env=env,
            )

        # Exposure: fixed (derived from the rig, comparable across materials)
        # unless auto mode is requested; auto measures the sphere's own pixels.
        if exposure_mode() == "auto":
            exposure = EXPOSURE_FALLBACK
            lum = _read_luminance(hdr, size, env)
            if lum is not None:
                exposure = auto_exposure(lum[sphere_mask(size)])
        else:
            exposure = fixed_exposure()
        log.debug("preview exposure for %s: %.3g (%s)", name, exposure, exposure_mode())

        # Convert to PNG via pfilt + ra_bmp + Pillow
        filtered = work / "preview_filt.hdr"
        bmp = work / "preview.bmp"
        with open(filtered, "wb") as f:
            subprocess.run(
                ["pfilt", "-1", "-e", f"{exposure:.4g}", str(hdr)],
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
