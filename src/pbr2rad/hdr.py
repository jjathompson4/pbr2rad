"""Radiance HDR (RGBE) writer with adaptive RLE compression.

Writes Radiance ``.hdr`` / ``.pic`` images from linear-light floating-point
RGB data.  Supports both uncompressed and RLE-compressed output (default: RLE).

RLE uses Greg Ward's adaptive scanline encoding: each scanline is split into
four separate channels (R, G, B, E) and each channel is independently
run-length encoded.  This typically reduces file size by 3–4×.

Reference: Greg Ward, "Real Pixels" (Graphics Gems II, 1991).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image


def _srgb_to_linear(c: float) -> float:
    """Convert an sRGB-encoded channel value in [0,1] to linear light."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


# Precompute sRGB → linear LUT for 8-bit input (fast path).
_SRGB_LUT_F: tuple[float, ...] = tuple(_srgb_to_linear(i / 255.0) for i in range(256))
_SRGB_LUT_NP = np.array(_SRGB_LUT_F, dtype=np.float64)


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


def _rgbe_encode_array(rgb: np.ndarray) -> np.ndarray:
    """Vectorized ``_rgbe``: (N, 3) float64 linear RGB → (N, 4) uint8 RGBE.

    Bit-for-bit identical to the scalar function: same frexp, same
    mantissa*256/m scale, same int() truncation and clamping.
    """
    m = np.maximum(np.maximum(rgb[:, 0], rgb[:, 1]), rgb[:, 2])
    valid = m >= 1e-32
    mantissa, exponent = np.frexp(m)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.where(valid, mantissa * 256.0 / m, 0.0)
    ints = np.clip((rgb * scale[:, None]).astype(np.int64), 0, 255)
    out = np.zeros((rgb.shape[0], 4), dtype=np.uint8)
    out[:, :3] = np.where(valid[:, None], ints, 0)
    out[:, 3] = np.where(valid, np.clip(exponent + 128, 0, 255), 0)
    return out


# ---------------------------------------------------------------------------
# Adaptive RLE encoding (new-style Radiance scanline compression)
# ---------------------------------------------------------------------------

_MIN_RUN = 4   # minimum run length to encode as a run
_MAX_SPAN = 127  # max literal or run span


def _rle_encode_channel(data: bytes) -> bytearray:
    """Encode one channel of one scanline using adaptive RLE.

    Returns a bytearray of the compressed channel data (without the
    scanline header — the caller writes that).

    Encoding rules:
    * Control byte < 128 → next *control* bytes are literal (non-run) data.
    * Control byte >= 128 → run of *(control − 128)* copies of the next byte.
    * Runs must be ≥ ``_MIN_RUN`` (4) bytes long.

    Implementation: maximal runs are located with numpy, then walked in
    Python (one iteration per run, not per byte). Output is byte-identical
    to the original per-byte scan: runs ≥ 4 emit run tokens chunked at 127;
    shorter repeats join the literal accumulator, which flushes at exactly
    127 bytes or when a run token (or end of data) arrives.
    """
    out = bytearray()
    n = len(data)
    if n == 0:
        return out

    arr = np.frombuffer(bytes(data), dtype=np.uint8)
    starts = np.concatenate(([0], np.flatnonzero(arr[1:] != arr[:-1]) + 1))
    lengths = np.diff(np.concatenate((starts, [n])))
    values = arr[starts]

    pending = bytearray()
    for v, run in zip(values.tolist(), lengths.tolist()):
        while run > 0:
            k = min(run, _MAX_SPAN)
            if k >= _MIN_RUN:
                if pending:
                    out.append(len(pending))
                    out.extend(pending)
                    pending.clear()
                out.append(k + 128)
                out.append(v)
            else:
                pending.extend([v] * k)
                while len(pending) >= _MAX_SPAN:
                    out.append(_MAX_SPAN)
                    out.extend(pending[:_MAX_SPAN])
                    del pending[:_MAX_SPAN]
            run -= k
    if pending:
        out.append(len(pending))
        out.extend(pending)
    return out


def _rle_encode_scanline(rgbe_row: bytearray | np.ndarray, width: int) -> bytes:
    """Encode one scanline of interleaved RGBE data using adaptive RLE.

    ``rgbe_row`` is ``width * 4`` bytes of interleaved R,G,B,E values
    (or an equivalent (width, 4) uint8 array).
    Returns the full encoded scanline (header + 4 encoded channels).
    """
    # Scanline header: 0x02 0x02 <width big-endian 16-bit>
    header = bytes([0x02, 0x02, (width >> 8) & 0xFF, width & 0xFF])

    if isinstance(rgbe_row, np.ndarray):
        row = rgbe_row.reshape(width, 4)
    else:
        row = np.frombuffer(bytes(rgbe_row), dtype=np.uint8).reshape(width, 4)

    # De-interleave via strided slicing (C memcpy, not a Python loop).
    return (
        header
        + bytes(_rle_encode_channel(np.ascontiguousarray(row[:, 0]).tobytes()))
        + bytes(_rle_encode_channel(np.ascontiguousarray(row[:, 1]).tobytes()))
        + bytes(_rle_encode_channel(np.ascontiguousarray(row[:, 2]).tobytes()))
        + bytes(_rle_encode_channel(np.ascontiguousarray(row[:, 3]).tobytes()))
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_hdr(
    path: Path,
    pixels: list[tuple[float, float, float]],
    width: int,
    height: int,
    *,
    exposure: float = 1.0,
    rle: bool = True,
) -> None:
    """Write a Radiance HDR file.

    ``pixels`` is a row-major sequence of linear RGB triples, top row first —
    either a list of 3-tuples or an ``(N, 3)`` float array.

    When ``rle=True`` (default), scanlines are compressed with adaptive RLE
    which typically reduces file size by 3–4×.  Set ``rle=False`` for the
    original uncompressed output.
    """
    path = Path(path)
    arr = np.asarray(pixels, dtype=np.float64).reshape(-1, 3)
    if arr.shape[0] != width * height:
        raise ValueError(
            f"pixel count {arr.shape[0]} does not match {width}x{height}"
        )

    header = (
        "#?RADIANCE\n"
        "# Written by pbr2rad\n"
        "FORMAT=32-bit_rle_rgbe\n"
        f"EXPOSURE={exposure:.6g}\n"
        "\n"
        f"-Y {height} +X {width}\n"
    ).encode("ascii")

    rgbe = _rgbe_encode_array(arr)

    with open(path, "wb") as f:
        f.write(header)

        if not rle or width < 8 or width > 0x7FFF:
            # Uncompressed path (original behavior, or width out of RLE range)
            f.write(rgbe.tobytes())
        else:
            # RLE path — encode scanline by scanline
            rows = rgbe.reshape(height, width, 4)
            for row in range(height):
                f.write(_rle_encode_scanline(rows[row], width))


def convert_ldr_to_hdr(
    src: Path,
    dst: Path,
    *,
    srgb: bool = True,
    rle: bool = True,
    scale: float = 1.0,
) -> tuple[int, int]:
    """Convert an LDR image (PNG/JPG/TIFF) to Radiance HDR.

    ``srgb`` decodes sRGB-encoded input to linear light; set False for data
    maps (roughness, normal) if they are ever passed through this path.
    ``rle`` enables adaptive RLE compression (default True).
    ``scale`` is a uniform multiplier applied after the gamma decode —
    used by the converter to reserve headroom for a constant specular
    term (see ``convert.convert_set``).
    Returns ``(width, height)``.
    """
    with Image.open(src) as raw:
        img = _demote_16bit(raw).convert("RGB")
    width, height = img.size
    data = np.frombuffer(img.tobytes(), dtype=np.uint8)  # row-major RGB bytes

    if srgb:
        pixels = _SRGB_LUT_NP[data] * scale
    else:
        pixels = data.astype(np.float64) * (scale / 255.0)

    write_hdr(dst, pixels.reshape(-1, 3), width, height, rle=rle)
    return width, height


def _demote_16bit(img: Image.Image) -> Image.Image:
    """Scale integer-mode (16/32-bit) images down to 8-bit grayscale.

    Pillow's ``convert("RGB")`` on an "I" image CLIPS values above 255 to
    white; a 16-bit PNG albedo would come out blown out. Divide down first.
    """
    if not img.mode.startswith("I"):
        return img
    arr = np.clip(np.asarray(img, dtype=np.int64) // 256, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "L")


def average_rgb(src: Path, *, srgb: bool = True) -> tuple[float, float, float]:
    """Compute mean linear RGB of an image (for manifest / reflectance)."""
    with Image.open(src) as raw:
        img = _demote_16bit(raw).convert("RGB")
    data = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(-1, 3)
    if data.shape[0] == 0:
        return (0.0, 0.0, 0.0)
    vals = _SRGB_LUT_NP[data] if srgb else data / 255.0
    sums = vals.sum(axis=0)
    n = data.shape[0]
    return (float(sums[0]) / n, float(sums[1]) / n, float(sums[2]) / n)


def average_gray(src: Path) -> float:
    """Mean 0..1 luminance of a single-channel data map (e.g. roughness).

    Handles both 8-bit and 16-bit images correctly.
    """
    with Image.open(src) as raw:
        img = raw.copy()
    mode = img.mode

    if mode.startswith("I"):
        # Integer modes: "I;16"/"I;16L"/"I;16B" are 16-bit; plain "I" is
        # 32-bit (a 16-bit PNG often loads as "I"). np.asarray decodes the
        # correct width/endianness for every variant — do NOT struct-unpack
        # raw bytes here: treating "I" as 16-bit shorts doubles the pixel
        # count with garbage values. Values are normalised as 16-bit.
        arr = np.asarray(img, dtype=np.float64)
        if arr.size == 0:
            return 0.0
        return min(1.0, float(arr.sum()) / (65535.0 * arr.size))

    # 8-bit path (original behaviour)
    img = img.convert("L")
    data = np.frombuffer(img.tobytes(), dtype=np.uint8)
    if data.size == 0:
        return 0.0
    return float(data.sum(dtype=np.int64)) / (255.0 * data.size)


__all__ = [
    "write_hdr",
    "convert_ldr_to_hdr",
    "average_rgb",
    "average_gray",
]
