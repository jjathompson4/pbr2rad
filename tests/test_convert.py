import json
from pathlib import Path

from PIL import Image

from pbr2rad.convert import ConvertOptions, convert_set, write_manifest
from pbr2rad.discover import discover


def _make_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (4, 4)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_convert_set_end_to_end(tmp_path: Path) -> None:
    src = tmp_path / "in" / "wood_floor"
    _make_png(src / "wood_floor_diff_2k.png", (180, 140, 90))
    _make_png(src / "wood_floor_rough_2k.png", (128, 128, 128))
    _make_png(src / "wood_floor_nor_gl_2k.png", (128, 128, 255))

    pbr = discover(src)
    out = tmp_path / "out"
    out.mkdir()
    result = convert_set(pbr, out, ConvertOptions(projection="uv"))

    assert result.rad_file.exists()
    assert result.cal_file.exists()
    assert result.hdr_file.exists()
    assert result.primitive == "plastic"
    # roughness map was mid-gray (128) → ~0.5
    assert abs(result.roughness - 0.5) < 0.01

    rad_text = result.rad_file.read_text()
    # Normal map is present, so texdata chain is used
    assert "texdata wood_floor_tex" in rad_text
    assert "9 dx_func dy_func dz_func" in rad_text
    assert "wood_floor_tex plastic wood_floor" in rad_text
    assert "wood_floor.hdr" in rad_text
    assert "wood_floor.cal" in rad_text

    cal_text = result.cal_file.read_text()
    assert "Lu" in cal_text and "Lv" in cal_text


def test_convert_set_planar_projection(tmp_path: Path) -> None:
    src = tmp_path / "brick"
    _make_png(src / "brick_diff.png", (200, 80, 80))

    pbr = discover(src)
    out = tmp_path / "out"
    result = convert_set(
        pbr, out,
        ConvertOptions(projection="planar", planar_axis="xz", u_scale=2.0),
    )
    cal_text = result.cal_file.read_text()
    assert "Px * u_scale" in cal_text
    assert "Pz * v_scale" in cal_text


def test_convert_set_metal_switch(tmp_path: Path) -> None:
    src = tmp_path / "steel"
    _make_png(src / "steel_diff.png", (200, 200, 200))
    _make_png(src / "steel_metal.png", (255, 255, 255))

    pbr = discover(src)
    out = tmp_path / "out"
    result = convert_set(pbr, out, ConvertOptions(projection="uv"))
    assert result.primitive == "metal"
    assert "metal steel" in result.rad_file.read_text()


def test_manifest_lists_all_materials(tmp_path: Path) -> None:
    # Two sets.
    _make_png(tmp_path / "in" / "a" / "a_diff.png", (100, 100, 100))
    _make_png(tmp_path / "in" / "b" / "b_diff.png", (200, 50, 50))

    out = tmp_path / "out"
    out.mkdir()

    results = [
        convert_set(discover(tmp_path / "in" / "a"), out),
        convert_set(discover(tmp_path / "in" / "b"), out),
    ]
    manifest_path = write_manifest(results, out)
    data = json.loads(manifest_path.read_text())
    assert data["generator"] == "pbr2rad"
    names = sorted(m["name"] for m in data["materials"])
    assert names == ["a", "b"]
    for entry in data["materials"]:
        assert entry["files"]["rad"].endswith(".rad")
        assert entry["primitive"] in ("plastic", "metal")
