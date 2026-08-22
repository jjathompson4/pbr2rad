"""ClimateStudio material-preview (``.pvw``) writer.

ClimateStudio's material browser looks for an eponymous ``.pvw`` next to a
``.rad`` file and uses it as the library thumbnail. Every material folder we
emit ships one so previews light up as soon as CS ships the SRC that reads
them.

Internals of the container format, the clearance to write it, and the
byte-for-byte verification against CS's own output are recorded in the
internal handoff note (``thompsonjeff.com/tools/handoff-pvw-integration.md``).
Do not restate them in user-facing docs.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

# CS version string stamped into every file we write.
PVW_VERSION_STRING = "2.4.9631.16263"
# Edge length of the embedded preview image (square).
PVW_IMAGE_SIZE = 256

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Container writer — verified byte-identical to ClimateStudio's own output.
# Keep verbatim; it is stdlib-only on purpose (no protobuf dependency).
# ---------------------------------------------------------------------------

def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _field(num: int, payload: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def build_pvw(name: str, png_bytes: bytes, version: str = PVW_VERSION_STRING) -> bytes:
    if png_bytes[:8] != _PNG_MAGIC:
        raise ValueError("png_bytes is not a PNG file")
    return (_field(1, name.encode("utf-8"))
            + _field(2, version.encode("utf-8"))
            + _field(3, png_bytes))


def write_pvw(
    path: str | Path,
    name: str,
    png_bytes: bytes,
    version: str = PVW_VERSION_STRING,
) -> None:
    with open(path, "wb") as f:
        f.write(build_pvw(name, png_bytes, version))


# ---------------------------------------------------------------------------
# Preview image normalisation
# ---------------------------------------------------------------------------

def make_preview_png(
    source: str | Path,
    size: int = PVW_IMAGE_SIZE,
    *,
    background: tuple[int, int, int] | None = None,
) -> bytes:
    """Return ``source`` as a square RGB PNG of ``size``x``size`` pixels.

    Non-square inputs are centre-cropped to their largest square before the
    resize, so the preview never shows a stretched texture. Sources with an
    alpha channel (the web preview render has a transparent background) are
    composited over ``background`` when given; otherwise alpha is dropped.
    """
    from PIL import Image

    with Image.open(source) as raw:
        img = raw
        if background is not None and (
            "A" in img.getbands() or img.mode == "P"
        ):
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (*background, 255))
            img = Image.alpha_composite(bg, img)
        img = img.convert("RGB")
        w, h = img.size
        if w != h:
            edge = min(w, h)
            left = (w - edge) // 2
            top = (h - edge) // 2
            img = img.crop((left, top, left + edge, top + edge))
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
