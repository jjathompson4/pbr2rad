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
_CAM_FOV_DEG = 42.0
_SPHERE_RADIUS = 1.0

# Neutral environment the sphere reflects: a dim glow covering the lower
# hemisphere of directions (the upper one is the sky ``light`` source). As a
# distant ``source`` it can't shadow the key/fill/sky lights (an enclosing
# glow sphere would), it is invisible to the direct calculation (glow,
# maxrad 0) but seen by specular and ambient rays, so metals and glossy
# materials get a lit body instead of reflecting a black void. The camera
# looks down, so it also forms the background — which is masked out below.
ENV_RADIANCE = (0.06, 0.06, 0.066)

# ---------------------------------------------------------------------------
# Exposure. Targets are display-linear (ra_bmp applies the 2.2 gamma after).
# ---------------------------------------------------------------------------
EXPOSURE_FALLBACK = 2 ** 1.4   # the fixed "+1.4 stops" used before auto-exposure
EXPOSURE_T_MID = 0.27          # median sphere luminance → ~0.55 after gamma
EXPOSURE_T_HI = 0.94           # 95th percentile → ~0.97; the top 5% may clip
EXPOSURE_MIN = 0.5
EXPOSURE_MAX = 32.0
_MASK_SHRINK = 0.92            # measure inside the rim, away from AA edge pixels
_EDGE_FEATHER_PX = 1.5         # alpha ramp width around the silhouette


@functools.lru_cache(maxsize=1)
def radiance_available() -> bool:
    """Return True if Radiance tools are on PATH.

    Cached: the answer can't change within one process (the deployment bakes
    Radiance into the image), and this sits on the health-check hot path.
    """
    return shutil.which("rpict") is not None and shutil.which("oconv") is not None


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
    """The preview scene: unit sphere, three-point lights, sky + env hemispheres."""
    er, eg, eb = ENV_RADIANCE
    return (
        f"{name} sphere ball\n0\n0\n4 0 0 0 {_SPHERE_RADIUS:g}\n\n"
        # Key light (warm, upper-front-left) - main shading source
        "void light key_l\n0\n0\n3 5.0 4.5 4.0\n\n"
        "key_l source key\n0\n0\n4 -0.6 -0.6 0.8 8\n\n"
        # Fill light (cool, lower-front-right) - softens shadows
        "void light fill_l\n0\n0\n3 1.0 1.2 1.5\n\n"
        "fill_l source fill\n0\n0\n4 0.7 -0.5 0.3 30\n\n"
        # Sky dome - hemispherical environment ambient (replaces HDRI)
        "void light sky_dome\n0\n0\n3 0.4 0.5 0.7\n\n"
        "sky_dome source sky\n0\n0\n4 0 0 1 180\n\n"
        # Lower hemisphere: dim neutral glow for reflections / ambient only
        f"void glow env_g\n0\n0\n4 {er:g} {eg:g} {eb:g} 0\n\n"
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

        # Auto-exposure from the sphere's own pixels (the surround is the
        # env glow / void and must not drive the exposure).
        exposure = EXPOSURE_FALLBACK
        lum = _read_luminance(hdr, size, env)
        if lum is not None:
            exposure = auto_exposure(lum[sphere_mask(size)])
        log.debug("preview exposure for %s: %.3g", name, exposure)

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
