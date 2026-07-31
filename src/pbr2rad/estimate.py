"""Estimate normal and roughness maps from an albedo image.

When a user provides only an albedo/diffuse texture, these functions
generate reasonable approximations of normal and roughness maps using
simple image processing:

* **Normal map**: Sobel edge detection on the grayscale albedo to find
  height gradients, then construct an OpenGL-convention normal map.
* **Roughness map**: Local variance in a sliding window — textured areas
  get higher roughness, smooth areas get lower roughness.

Uses only Pillow — no additional dependencies.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def estimate_normal(
    albedo_path: Path,
    output_path: Path,
    *,
    strength: float = 1.0,
) -> Path:
    """Generate a normal map from an albedo image via Sobel edge detection.

    The result is an RGB PNG in OpenGL convention:
    R = X (right), G = Y (up), B = Z (outward).

    Parameters
    ----------
    albedo_path:
        Path to the input albedo/diffuse image.
    output_path:
        Where to save the generated normal map PNG.
    strength:
        Bump strength multiplier.  Higher = more pronounced bumps.

    Returns the output path.
    """
    img = Image.open(albedo_path).convert("L")
    width, height = img.size

    # Enhance local contrast before Sobel to make subtle features visible.
    # Use CLAHE-like approach: apply a local equalization via unsharp mask.
    sharpened = img.filter(ImageFilter.UnsharpMask(radius=10, percent=200, threshold=0))
    gray = (
        np.frombuffer(sharpened.tobytes(), dtype=np.uint8)
        .reshape(height, width)
        .astype(np.float64)
        / 255.0
    )

    # Sobel gradients with clamped (edge-replicated) borders. The summation
    # order below mirrors the original per-pixel expressions term for term so
    # the float results are bit-identical to the old Python loop.
    p = np.pad(gray, 1, mode="edge")
    tl = p[0:-2, 0:-2]; tm = p[0:-2, 1:-1]; tr = p[0:-2, 2:]
    ml = p[1:-1, 0:-2];                     mr = p[1:-1, 2:]
    bl = p[2:,   0:-2]; bm = p[2:,   1:-1]; br = p[2:,   2:]

    gx = -1 * tl + 1 * tr
    gx = gx + -2 * ml
    gx = gx + 2 * mr
    gx = gx + -1 * bl
    gx = gx + 1 * br

    gy = -1 * tl + -2 * tm
    gy = gy + -1 * tr
    gy = gy + 1 * bl
    gy = gy + 2 * bm
    gy = gy + 1 * br

    # Auto-scale: normalize gradients so the strongest edges produce
    # a visible deflection.  This handles low-contrast images like
    # dark tiles with subtle grout lines.
    max_mag = float(np.sqrt(gx * gx + gy * gy).max())
    if max_mag < 1e-6:
        max_mag = 1.0
    # Scale so max gradient → ~0.7 deflection (strong but not extreme),
    # then apply user strength on top.
    auto_scale = 0.7 / max_mag

    # Normal vector (OpenGL convention: Y up), normalized, remapped to bytes.
    s = auto_scale * strength
    nx = -gx * s
    ny = -gy * s
    length = np.sqrt(nx * nx + ny * ny + 1.0)  # nz = 1.0
    nx = nx / length
    ny = ny / length
    nz = 1.0 / length

    def _to_byte(v: np.ndarray) -> np.ndarray:
        # int() truncation + clamp, exactly as the original per-pixel code.
        return np.clip(((v * 0.5 + 0.5) * 255).astype(np.int64), 0, 255)

    rgb = np.stack([_to_byte(nx), _to_byte(ny), _to_byte(nz)], axis=-1)
    out = Image.frombytes("RGB", (width, height), rgb.astype(np.uint8).tobytes())
    output_path = Path(output_path)
    out.save(output_path)
    return output_path


def estimate_roughness(
    albedo_path: Path,
    output_path: Path,
    *,
    window: int = 9,
) -> Path:
    """Generate a roughness map from an albedo via local variance.

    High-variance regions (texture detail, edges) → rough.
    Low-variance regions (flat, smooth) → glossy.

    Parameters
    ----------
    albedo_path:
        Path to the input albedo/diffuse image.
    output_path:
        Where to save the generated roughness map PNG (grayscale).
    window:
        Sliding window size for variance computation.

    Returns the output path.
    """
    img = Image.open(albedo_path).convert("L")
    width, height = img.size

    # Two-pass approach for better roughness estimation:
    # 1. Edge strength (Sobel magnitude) — edges/grout/seams → rough
    # 2. Local texture variance — fine detail → rough, flat → smooth
    # Combine both signals for a more useful roughness map.

    radius = window // 2

    # Pass 1: Sobel edge magnitude
    edge_x = img.filter(ImageFilter.Kernel(
        (3, 3), [-1, 0, 1, -2, 0, 2, -1, 0, 1], scale=1, offset=128,
    ))
    edge_y = img.filter(ImageFilter.Kernel(
        (3, 3), [-1, -2, -1, 0, 0, 0, 1, 2, 1], scale=1, offset=128,
    ))
    def _to_f(im: Image.Image) -> np.ndarray:
        return np.frombuffer(im.tobytes(), dtype=np.uint8).astype(np.float64)

    dx = (_to_f(edge_x) - 128) / 128.0
    dy = (_to_f(edge_y) - 128) / 128.0
    edge_mag = np.sqrt(dx * dx + dy * dy)

    # Pass 2: Local variance via box blur
    mean_img = img.filter(ImageFilter.BoxBlur(radius))
    mean_data = _to_f(mean_img) / 255.0
    pixels = _to_f(img) / 255.0

    sq_bytes = np.clip((pixels * pixels * 255).astype(np.int64), 0, 255)
    sq_img = Image.frombytes(
        "L", (width, height), sq_bytes.astype(np.uint8).tobytes(),
    )
    mean_sq_img = sq_img.filter(ImageFilter.BoxBlur(radius))
    mean_sq_data = _to_f(mean_sq_img) / 255.0

    # sqrt for perceptual scaling
    variance = np.sqrt(np.maximum(0.0, mean_sq_data - mean_data * mean_data))

    # Normalize both signals
    max_edge = float(edge_mag.max()) if edge_mag.size else 1.0
    max_var = float(variance.max()) if variance.size else 1.0
    if max_edge < 1e-6:
        max_edge = 1.0
    if max_var < 1e-6:
        max_var = 1.0

    # Combine: 60% edge strength + 40% variance
    combined = 0.6 * (edge_mag / max_edge) + 0.4 * (variance / max_var)
    # Remap with floor and ceiling
    rough = 0.15 + combined * 0.75
    rough_bytes = np.clip((rough * 255).astype(np.int64), 0, 255)

    out = Image.frombytes("L", (width, height), rough_bytes.astype(np.uint8).tobytes())
    # Moderate smoothing to clean up noise while preserving edges
    out = out.filter(ImageFilter.GaussianBlur(radius=3))
    output_path = Path(output_path)
    out.save(output_path)
    return output_path
