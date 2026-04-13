"""Radiance HDR (RGBE) writer.

Writes an uncompressed Radiance ``.hdr`` / ``.pic`` image from linear-light
floating-point RGB data.  No RLE compression — simple per-pixel RGBE encoding,
which every Radiance tool reads.

Reference: Greg Ward, "Real Pixels" (Graphics Gems II, 1991).
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

from PIL import Image


def _srgb_to_linear(c: float) -> float:
    """Convert an sRGB-encoded channel value in [0,1] to linear light."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


# Precompute sRGB → linear LUT for 8-bit input (fast path).
_SRGB_LUT = bytes  # placeholder for type hints; actual LUT is float tuple below
_SRGB_LUT_F: tuple[float, ...] = tuple(_srgb_to_linear(i / 255.0) for i in range(256))


def _rgbe(r: float, g: float, b: float) -> tuple[int, int, int, int]:
    """Encode a linear RGB triple as a 4-byte RGBE pixel."""
    m = max(r, g, b)
    if m < 1e-32:
        return (0, 0, 0, 0)
    mantissa, exponent = math.frexp(m)
    # Scale factor so that mantissa * 256 fits in a byte.
    scale = mantissa * 256.0 / m
    return (
        max(0, min(255, int(r * scale))),
        max(0, min(255, int(g * scale))),
        max(0, min(255, int(b * scale))),
        max(0, min(255, exponent + 128)),
    )


def write_hdr(
    path: Path,
    pixels: list[tuple[float, float, float]],
    width: int,
    height: int,
    *,
    exposure: float = 1.0,
) -> None:
    """Write a Radiance HDR file.

    ``pixels`` is a row-major sequence of linear RGB triples, top row first.
    """
    path = Path(path)
    if len(pixels) != width * height:
        raise ValueError(
            f"pixel count {len(pixels)} does not match {width}x{height}"
        )

    header = (
        "#?RADIANCE\n"
        "# Written by pbr2rad\n"
        "FORMAT=32-bit_rle_rgbe\n"
        f"EXPOSURE={exposure:.6g}\n"
        "\n"
        f"-Y {height} +X {width}\n"
    ).encode("ascii")

    with open(path, "wb") as f:
        f.write(header)
        buf = bytearray(4 * width * height)
        i = 0
        for r, g, b in pixels:
            enc = _rgbe(r, g, b)
            buf[i] = enc[0]
            buf[i + 1] = enc[1]
            buf[i + 2] = enc[2]
            buf[i + 3] = enc[3]
            i += 4
        f.write(bytes(buf))


def convert_ldr_to_hdr(src: Path, dst: Path, *, srgb: bool = True) -> tuple[int, int]:
    """Convert an LDR image (PNG/JPG/TIFF) to Radiance HDR.

    ``srgb`` decodes sRGB-encoded input to linear light; set False for data
    maps (roughness, normal) if they are ever passed through this path.
    Returns ``(width, height)``.
    """
    img = Image.open(src).convert("RGB")
    width, height = img.size
    data = img.tobytes()  # row-major RGB bytes

    pixels: list[tuple[float, float, float]] = []
    lut = _SRGB_LUT_F
    if srgb:
        for i in range(0, len(data), 3):
            pixels.append((lut[data[i]], lut[data[i + 1]], lut[data[i + 2]]))
    else:
        inv = 1.0 / 255.0
        for i in range(0, len(data), 3):
            pixels.append((data[i] * inv, data[i + 1] * inv, data[i + 2] * inv))

    write_hdr(dst, pixels, width, height)
    return width, height


def average_rgb(src: Path, *, srgb: bool = True) -> tuple[float, float, float]:
    """Compute mean linear RGB of an image (for manifest / reflectance)."""
    img = Image.open(src).convert("RGB")
    data = img.tobytes()
    n = len(data) // 3
    if n == 0:
        return (0.0, 0.0, 0.0)
    r = g = b = 0.0
    lut = _SRGB_LUT_F
    if srgb:
        for i in range(0, len(data), 3):
            r += lut[data[i]]
            g += lut[data[i + 1]]
            b += lut[data[i + 2]]
    else:
        inv = 1.0 / 255.0
        for i in range(0, len(data), 3):
            r += data[i] * inv
            g += data[i + 1] * inv
            b += data[i + 2] * inv
    return (r / n, g / n, b / n)


def average_gray(src: Path) -> float:
    """Mean 0..1 luminance of a single-channel data map (e.g. roughness).

    Handles both 8-bit and 16-bit images correctly.
    """
    img = Image.open(src)
    mode = img.mode

    if mode in ("I;16", "I"):
        # 16-bit image — read raw 16-bit values to avoid Pillow's
        # lossy conversion to 8-bit "L" mode which saturates values.
        data = img.tobytes()
        if not data:
            return 0.0
        pixels = struct.unpack(f"<{len(data) // 2}H", data)
        return sum(pixels) / (65535.0 * len(pixels))

    # 8-bit path (original behaviour)
    img = img.convert("L")
    data = img.tobytes()
    if not data:
        return 0.0
    return sum(data) / (255.0 * len(data))


# Silence unused-import warnings in __all__.
__all__ = [
    "write_hdr",
    "convert_ldr_to_hdr",
    "average_rgb",
    "average_gray",
]

# struct is imported for potential future RLE writer; keep import stable.
_ = struct
