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
    gray = img.tobytes()

    def px(x: int, y: int) -> float:
        """Get pixel value as 0..1 float, clamped to image bounds."""
        x = max(0, min(width - 1, x))
        y = max(0, min(height - 1, y))
        return gray[y * width + x] / 255.0

    # Sobel kernels applied per-pixel
    normal_data = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            # Sobel X: horizontal gradient
            gx = (
                -1 * px(x - 1, y - 1) + 1 * px(x + 1, y - 1)
                + -2 * px(x - 1, y)     + 2 * px(x + 1, y)
                + -1 * px(x - 1, y + 1) + 1 * px(x + 1, y + 1)
            )
            # Sobel Y: vertical gradient
            gy = (
                -1 * px(x - 1, y - 1) + -2 * px(x, y - 1) + -1 * px(x + 1, y - 1)
                + 1 * px(x - 1, y + 1) + 2 * px(x, y + 1) + 1 * px(x + 1, y + 1)
            )

            # Normal vector (OpenGL convention: Y up)
            nx = -gx * strength
            ny = -gy * strength
            nz = 1.0

            # Normalize
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length > 0:
                nx /= length
                ny /= length
                nz /= length

            # Remap [-1,+1] → [0,255]
            idx = (y * width + x) * 3
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

    # Compute local variance using box-blur approach:
    # variance = E[x^2] - E[x]^2
    # where E[] is the local mean computed via box blur.

    # Create float arrays
    pixels = [b / 255.0 for b in img.tobytes()]
    pixels_sq = [p * p for p in pixels]

    # Box blur for local mean and local mean-of-squares
    # Use Pillow's built-in BoxBlur for speed
    radius = window // 2

    mean_img = img.filter(ImageFilter.BoxBlur(radius))
    mean_data = [b / 255.0 for b in mean_img.tobytes()]

    sq_img = Image.frombytes("L", (width, height), bytes(max(0, min(255, int(p * 255))) for p in pixels_sq))
    mean_sq_img = sq_img.filter(ImageFilter.BoxBlur(radius))
    mean_sq_data = [b / 255.0 for b in mean_sq_img.tobytes()]

    # Variance = E[x^2] - E[x]^2
    variance = []
    for i in range(len(pixels)):
        v = max(0.0, mean_sq_data[i] - mean_data[i] * mean_data[i])
        variance.append(v)

    # Normalize to 0..1
    max_var = max(variance) if variance else 1.0
    if max_var < 1e-10:
        max_var = 1.0

    rough_bytes = bytearray(width * height)
    for i, v in enumerate(variance):
        # Map variance to roughness: sqrt gives a more perceptual scaling
        normalized = math.sqrt(v / max_var)
        # Remap: minimum roughness 0.2 (nothing is perfectly smooth),
        # maximum 0.9 (leave room for truly rough materials)
        rough = 0.2 + normalized * 0.7
        rough_bytes[i] = max(0, min(255, int(rough * 255)))

    out = Image.frombytes("L", (width, height), bytes(rough_bytes))
    # Light smoothing to reduce noise
    out = out.filter(ImageFilter.GaussianBlur(radius=2))
    output_path = Path(output_path)
    out.save(output_path)
    return output_path
