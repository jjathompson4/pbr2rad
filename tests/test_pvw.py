"""Tests for the ClimateStudio material-preview (.pvw) writer."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from pbr2rad.convert import ConvertOptions, convert_set, write_manifest
from pbr2rad.discover import discover
from pbr2rad.pvw import (
    PVW_IMAGE_SIZE,
    PVW_VERSION_STRING,
    build_pvw,
    make_preview_png,
    write_pvw,
)

PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


# ---------------------------------------------------------------------------
# Independent protobuf reader — deliberately NOT sharing code with the writer,
# so a bug in the writer's varint/tag encoding cannot cancel itself out here.
# ---------------------------------------------------------------------------

def parse_pvw(data: bytes) -> dict[int, bytes]:
    """Parse length-delimited protobuf fields into {field_number: payload}."""
    fields: dict[int, bytes] = {}
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        assert tag & 0x07 == 2, f"field {tag >> 3} is not length-delimited"
        length, i = _read_varint(data, i)
        assert i + length <= len(data), "payload runs past end of file"
        fields[tag >> 3] = data[i:i + length]
        i += length
    return fields


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def _png_bytes(size: tuple[int, int] = (8, 8), color=(120, 90, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_png(path: Path, color: tuple[int, int, int], size=(16, 16)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


# ---------------------------------------------------------------------------
# Container writer
# ---------------------------------------------------------------------------

def test_build_pvw_field_layout() -> None:
    png = _png_bytes()
    fields = parse_pvw(build_pvw("wood_floor", png))

    assert sorted(fields) == [1, 2, 3]
    assert fields[1].decode("utf-8") == "wood_floor"
    assert fields[2].decode("utf-8") == PVW_VERSION_STRING
    assert fields[3] == png


def test_build_pvw_is_deterministic() -> None:
    png = _png_bytes()
    assert build_pvw("mat", png) == build_pvw("mat", png)


def test_build_pvw_rejects_non_png() -> None:
    with pytest.raises(ValueError):
        build_pvw("mat", b"BM not a png at all")


def test_build_pvw_handles_multibyte_lengths() -> None:
    """A payload over 127 bytes needs a multi-byte varint length prefix."""
    png = _png_bytes(size=(PVW_IMAGE_SIZE, PVW_IMAGE_SIZE))
    assert len(png) > 127
    assert parse_pvw(build_pvw("mat", png))[3] == png


def test_build_pvw_accepts_unicode_name() -> None:
    name = "béton_brut"
    fields = parse_pvw(build_pvw(name, _png_bytes()))
    assert fields[1].decode("utf-8") == name


def test_write_pvw_round_trips_through_disk(tmp_path: Path) -> None:
    png = _png_bytes()
    path = tmp_path / "mat.pvw"
    write_pvw(path, "mat", png)
    assert parse_pvw(path.read_bytes())[3] == png


# ---------------------------------------------------------------------------
# Preview image normalisation
# ---------------------------------------------------------------------------

def test_make_preview_png_normalises_size(tmp_path: Path) -> None:
    src = tmp_path / "albedo.png"
    _make_png(src, (200, 160, 120), size=(640, 480))

    data = make_preview_png(src)
    assert data[:8] == PNG_MAGIC
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (PVW_IMAGE_SIZE, PVW_IMAGE_SIZE)
    # A 256x256 PNG belongs in the tens of KB, not the megabytes.
    assert len(data) < 512 * 1024


def test_make_preview_png_centre_crops_rather_than_stretching(tmp_path: Path) -> None:
    # Left half red, right half blue, in a 2:1 frame. A centre crop keeps the
    # seam in the middle; a stretch would too, so check the far edges: after
    # cropping to the central square, both edge columns stay pure.
    img = Image.new("RGB", (400, 200), (255, 0, 0))
    for x in range(200, 400):
        for y in range(200):
            img.putpixel((x, y), (0, 0, 255))
    src = tmp_path / "wide.png"
    img.save(src)

    with Image.open(io.BytesIO(make_preview_png(src))) as out:
        assert out.size == (PVW_IMAGE_SIZE, PVW_IMAGE_SIZE)
        assert out.getpixel((2, 128))[0] > 200          # still red
        assert out.getpixel((PVW_IMAGE_SIZE - 3, 128))[2] > 200  # still blue


def test_make_preview_png_composites_alpha_over_background(tmp_path: Path) -> None:
    """The web preview render is RGBA with a transparent surround; the .pvw
    needs a flat colour there, not whatever RGB hides under alpha 0."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))          # transparent (black under)
    for x in range(16, 48):
        for y in range(16, 48):
            img.putpixel((x, y), (200, 50, 50, 255))          # opaque red centre
    src = tmp_path / "rgba.png"
    img.save(src)

    with Image.open(io.BytesIO(make_preview_png(src, background=(40, 40, 44)))) as out:
        assert out.mode == "RGB"
        assert out.getpixel((2, 2)) == (40, 40, 44)            # corner = background
        assert out.getpixel((PVW_IMAGE_SIZE // 2, PVW_IMAGE_SIZE // 2))[0] > 180
    # Without a background the alpha is simply dropped (legacy behaviour).
    with Image.open(io.BytesIO(make_preview_png(src))) as out:
        assert out.mode == "RGB"


def test_make_preview_png_handles_grayscale_source(tmp_path: Path) -> None:
    src = tmp_path / "gray.png"
    Image.new("L", (64, 64), 128).save(src)
    with Image.open(io.BytesIO(make_preview_png(src))) as img:
        assert img.mode == "RGB"


# ---------------------------------------------------------------------------
# Integration with the conversion pipeline
# ---------------------------------------------------------------------------

def _convert(tmp_path: Path, name: str = "wood_floor", **opt_kwargs):
    src = tmp_path / "in" / name
    _make_png(src / f"{name}_diff.png", (180, 140, 90))
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return convert_set(discover(src), out, ConvertOptions(**opt_kwargs))


def test_convert_set_writes_one_pvw_per_rad(tmp_path: Path) -> None:
    result = _convert(tmp_path)

    pvws = sorted(result.out_dir.glob("*.pvw"))
    assert len(pvws) == 1
    assert result.pvw_file == pvws[0]
    # Basenames must match exactly — CS looks for an eponymous file.
    assert pvws[0].stem == result.rad_file.stem


def test_convert_set_pvw_contents(tmp_path: Path) -> None:
    result = _convert(tmp_path)
    fields = parse_pvw(result.pvw_file.read_bytes())

    assert fields[1].decode("utf-8") == result.name
    assert fields[2].decode("utf-8") == PVW_VERSION_STRING
    assert fields[3][:8] == PNG_MAGIC
    with Image.open(io.BytesIO(fields[3])) as img:
        assert img.size == (PVW_IMAGE_SIZE, PVW_IMAGE_SIZE)


def test_convert_set_pvw_can_be_disabled(tmp_path: Path) -> None:
    result = _convert(tmp_path, write_pvw=False)
    assert result.pvw_file is None
    assert not list(result.out_dir.glob("*.pvw"))


def test_manifest_lists_the_pvw(tmp_path: Path) -> None:
    import json

    result = _convert(tmp_path)
    manifest = json.loads(write_manifest([result], tmp_path / "out").read_text())
    files = manifest["materials"][0]["files"]
    assert files["pvw"] == f"{result.name}/{result.name}.pvw"


def test_manifest_omits_pvw_when_not_written(tmp_path: Path) -> None:
    import json

    result = _convert(tmp_path, write_pvw=False)
    manifest = json.loads(write_manifest([result], tmp_path / "out").read_text())
    assert "pvw" not in manifest["materials"][0]["files"]
