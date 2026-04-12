import math
from pathlib import Path

from PIL import Image

from pbr2rad.hdr import _rgbe, convert_ldr_to_hdr, write_hdr


def _decode_rgbe(r: int, g: int, b: int, e: int) -> tuple[float, float, float]:
    if e == 0:
        return (0.0, 0.0, 0.0)
    f = math.ldexp(1.0, e - (128 + 8))
    return (r * f, g * f, b * f)


def test_rgbe_roundtrip_preserves_magnitude() -> None:
    for rgb in [(1.0, 0.5, 0.25), (0.1, 0.1, 0.1), (4.0, 2.0, 1.0)]:
        enc = _rgbe(*rgb)
        dec = _decode_rgbe(*enc)
        for a, b in zip(rgb, dec):
            assert abs(a - b) < 0.01, (rgb, dec)


def test_rgbe_zero_black() -> None:
    assert _rgbe(0.0, 0.0, 0.0) == (0, 0, 0, 0)


def test_write_hdr_header(tmp_path: Path) -> None:
    out = tmp_path / "test.hdr"
    write_hdr(out, [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], width=2, height=1)
    data = out.read_bytes()
    assert data.startswith(b"#?RADIANCE\n")
    assert b"FORMAT=32-bit_rle_rgbe" in data
    assert b"-Y 1 +X 2" in data


def test_convert_ldr_to_hdr_srgb_decode(tmp_path: Path) -> None:
    # Mid-gray sRGB (128) should decode to ~0.2159 linear.
    src = tmp_path / "gray.png"
    img = Image.new("RGB", (2, 2), (128, 128, 128))
    img.save(src)
    dst = tmp_path / "gray.hdr"
    w, h = convert_ldr_to_hdr(src, dst, srgb=True)
    assert (w, h) == (2, 2)

    data = dst.read_bytes()
    # Find start of pixel data: after blank line + resolution line.
    resolution_line = b"-Y 2 +X 2\n"
    idx = data.index(resolution_line) + len(resolution_line)
    pixels = data[idx:]
    assert len(pixels) == 4 * 4
    r, g, b, e = pixels[0], pixels[1], pixels[2], pixels[3]
    decoded = _decode_rgbe(r, g, b, e)
    for channel in decoded:
        assert abs(channel - 0.2159) < 0.01, decoded
