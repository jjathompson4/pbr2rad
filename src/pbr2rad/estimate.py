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

import math
from pathlib import Path

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
    gray = sharpened.tobytes()

    def px(x: int, y: int) -> float:
        """Get pixel value as 0..1 float, clamped to image bounds."""
        x = max(0, min(width - 1, x))
        y = max(0, min(height - 1, y))
        return gray[y * width + x] / 255.0

    # Compute Sobel gradients and find max magnitude for auto-scaling
    gradients = []
    for y in range(height):
        for x in range(width):
            gx = (
                -1 * px(x - 1, y - 1) + 1 * px(x + 1, y - 1)
                + -2 * px(x - 1, y)     + 2 * px(x + 1, y)
                + -1 * px(x - 1, y + 1) + 1 * px(x + 1, y + 1)
            )
            gy = (
                -1 * px(x - 1, y - 1) + -2 * px(x, y - 1) + -1 * px(x + 1, y - 1)
                + 1 * px(x - 1, y + 1) + 2 * px(x, y + 1) + 1 * px(x + 1, y + 1)
            )
            gradients.append((gx, gy))

    # Auto-scale: normalize gradients so the strongest edges produce
    # a visible deflection.  This handles low-contrast images like
    # dark tiles with subtle grout lines.
    max_mag = max(
        math.sqrt(gx * gx + gy * gy) for gx, gy in gradients
    )
    if max_mag < 1e-6:
        max_mag = 1.0
    # Scale so max gradient → ~0.7 deflection (strong but not extreme),
    # then apply user strength on top.
    auto_scale = 0.7 / max_mag

    normal_data = bytearray(width * height * 3)
    for i, (gx, gy) in enumerate(gradients):
        # Normal vector (OpenGL convention: Y up)
        s = auto_scale * strength
        nx = -gx * s
        ny = -gy * s
        nz = 1.0

        # Normalize
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length > 0:
            nx /= length
            ny /= length
            nz /= length

        # Remap [-1,+1] → [0,255]
        idx = i * 3
        normal_data[idx] = max(0, min(255, int((nx * 0.5 + 0.5) * 255)))
        normal_data[idx + 1] = max(0, min(255, int((ny * 0.5 + 0.5) * 255)))
        normal_data[idx + 2] = max(0, min(255, int((nz * 0.5 + 0.5) * 255)))

    out = Image.frombytes("RGB", (width, height), bytes(normal_data))
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
    ex_data = edge_x.tobytes()
    ey_data = edge_y.tobytes()

    edge_mag = []
    for i in range(width * height):
        dx = (ex_data[i] - 128) / 128.0
        dy = (ey_data[i] - 128) / 128.0
        edge_mag.append(math.sqrt(dx * dx + dy * dy))

    # Pass 2: Local variance via box blur
    mean_img = img.filter(ImageFilter.BoxBlur(radius))
    mean_data = [b / 255.0 for b in mean_img.tobytes()]
    pixels = [b / 255.0 for b in img.tobytes()]

    sq_img = Image.frombytes(
        "L", (width, height),
        bytes(max(0, min(255, int(p * p * 255))) for p in pixels),
    )
    mean_sq_img = sq_img.filter(ImageFilter.BoxBlur(radius))
    mean_sq_data = [b / 255.0 for b in mean_sq_img.tobytes()]

    variance = []
    for i in range(len(pixels)):
        v = max(0.0, mean_sq_data[i] - mean_data[i] * mean_data[i])
        variance.append(math.sqrt(v))  # sqrt for perceptual scaling

    # Normalize both signals
    max_edge = max(edge_mag) if edge_mag else 1.0
    max_var = max(variance) if variance else 1.0
    if max_edge < 1e-6:
        max_edge = 1.0
    if max_var < 1e-6:
        max_var = 1.0

    # Combine: 60% edge strength + 40% variance
    rough_bytes = bytearray(width * height)
    for i in range(width * height):
        e = edge_mag[i] / max_edge
        v = variance[i] / max_var
        combined = 0.6 * e + 0.4 * v
        # Remap with floor and ceiling
        rough = 0.15 + combined * 0.75
        rough_bytes[i] = max(0, min(255, int(rough * 255)))

    out = Image.frombytes("L", (width, height), bytes(rough_bytes))
    # Moderate smoothing to clean up noise while preserving edges
    out = out.filter(ImageFilter.GaussianBlur(radius=3))
    output_path = Path(output_path)
    out.save(output_path)
    return output_path
