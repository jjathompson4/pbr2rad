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
    """
    out = bytearray()
    n = len(data)
    i = 0

    while i < n:
        # Look ahead for a run of identical values
        run_val = data[i]
        run_len = 1
        while i + run_len < n and data[i + run_len] == run_val and run_len < _MAX_SPAN:
            run_len += 1

        if run_len >= _MIN_RUN:
            # Emit a run
            out.append(run_len + 128)
            out.append(run_val)
            i += run_len
        else:
            # Collect literal (non-run) bytes until the next run ≥ _MIN_RUN
            lit_start = i
            lit_end = i

            while lit_end < n:
                # Check if a run starts at lit_end
                peek_val = data[lit_end]
                peek_len = 1
                while (
                    lit_end + peek_len < n
                    and data[lit_end + peek_len] == peek_val
                    and peek_len < _MIN_RUN
                ):
                    peek_len += 1

                if peek_len >= _MIN_RUN:
                    break  # run starts here — stop the literal span
                lit_end += 1

                if lit_end - lit_start >= _MAX_SPAN:
                    break  # max literal span reached

            span = lit_end - lit_start
            out.append(span)
            out.extend(data[lit_start:lit_end])
            i = lit_end

    return out


def _rle_encode_scanline(rgbe_row: bytearray, width: int) -> bytes:
    """Encode one scanline of interleaved RGBE data using adaptive RLE.

    ``rgbe_row`` is ``width * 4`` bytes of interleaved R,G,B,E values.
    Returns the full encoded scanline (header + 4 encoded channels).
    """
    # Scanline header: 0x02 0x02 <width big-endian 16-bit>
    header = bytes([0x02, 0x02, (width >> 8) & 0xFF, width & 0xFF])

    # De-interleave into 4 channel arrays
    ch_r = bytearray(width)
    ch_g = bytearray(width)
    ch_b = bytearray(width)
    ch_e = bytearray(width)

    for j in range(width):
        off = j * 4
        ch_r[j] = rgbe_row[off]
        ch_g[j] = rgbe_row[off + 1]
        ch_b[j] = rgbe_row[off + 2]
        ch_e[j] = rgbe_row[off + 3]

    # Encode each channel
    return (
        header
        + bytes(_rle_encode_channel(ch_r))
        + bytes(_rle_encode_channel(ch_g))
        + bytes(_rle_encode_channel(ch_b))
        + bytes(_rle_encode_channel(ch_e))
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

    ``pixels`` is a row-major sequence of linear RGB triples, top row first.

    When ``rle=True`` (default), scanlines are compressed with adaptive RLE
    which typically reduces file size by 3–4×.  Set ``rle=False`` for the
    original uncompressed output.
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

        if not rle or width < 8 or width > 0x7FFF:
            # Uncompressed path (original behavior, or width out of RLE range)
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
        else:
            # RLE path — encode scanline by scanline
            for row in range(height):
                row_start = row * width
                row_end = row_start + width
                # Build interleaved RGBE row
                rgbe_row = bytearray(width * 4)
                for j, (r, g, b) in enumerate(pixels[row_start:row_end]):
                    enc = _rgbe(r, g, b)
                    off = j * 4
                    rgbe_row[off] = enc[0]
                    rgbe_row[off + 1] = enc[1]
                    rgbe_row[off + 2] = enc[2]
                    rgbe_row[off + 3] = enc[3]
                f.write(_rle_encode_scanline(rgbe_row, width))


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
    img = Image.open(src).convert("RGB")
    width, height = img.size
    data = img.tobytes()  # row-major RGB bytes

    pixels: list[tuple[float, float, float]] = []
    lut = _SRGB_LUT_F
    if srgb:
        for i in range(0, len(data), 3):
            pixels.append(
                (lut[data[i]] * scale, lut[data[i + 1]] * scale, lut[data[i + 2]] * scale)
            )
    else:
        inv = scale / 255.0
        for i in range(0, len(data), 3):
            pixels.append((data[i] * inv, data[i + 1] * inv, data[i + 2] * inv))

    write_hdr(dst, pixels, width, height, rle=rle)
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

    if mode.startswith("I"):
        # Integer modes: "I;16"/"I;16L"/"I;16B" are 16-bit; plain "I" is
        # 32-bit (a 16-bit PNG often loads as "I"). getdata() decodes the
        # correct width/endianness for every variant — do NOT struct-unpack
        # raw bytes here: treating "I" as 16-bit shorts doubles the pixel
        # count with garbage values. Values are normalised as 16-bit.
        pixels = list(img.getdata())
        if not pixels:
            return 0.0
        return min(1.0, sum(pixels) / (65535.0 * len(pixels)))

    # 8-bit path (original behaviour)
    img = img.convert("L")
    data = img.tobytes()
    if not data:
        return 0.0
    return sum(data) / (255.0 * len(data))


__all__ = [
    "write_hdr",
    "convert_ldr_to_hdr",
    "average_rgb",
    "average_gray",
]
