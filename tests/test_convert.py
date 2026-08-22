import json
from pathlib import Path

from PIL import Image

from pbr2rad.convert import ConvertOptions, convert_set, write_manifest
from pbr2rad.discover import discover


def _make_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (4, 4)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_non_square_albedo_scales_picture_lookup(tmp_path: Path) -> None:
    """A 2:1 albedo must stretch the colorpict lookup (pic_u = u * 2) while
    the .dat modifiers keep the unit square — otherwise only the left half
    of the picture is sampled and it no longer lines up with the normals."""
    src = tmp_path / "in" / "Bricks104"
    _make_png(src / "Bricks104_1K-JPG_Color.png", (150, 80, 60), size=(64, 32))
    _make_png(src / "Bricks104_1K-JPG_NormalGL.png", (128, 128, 255), size=(64, 32))
    _make_png(src / "Bricks104_1K-JPG_Roughness.png", (128, 128, 128), size=(64, 32))

    result = convert_set(discover(src), tmp_path / "out", ConvertOptions(projection="box"))
    cal_text = result.cal_file.read_text()
    assert "pic_u = u * 2;" in cal_text
    assert "pic_v = v * 1;" in cal_text
    rad_text = result.rad_file.read_text()
    assert "Bricks104.hdr Bricks104.cal pic_u pic_v" in rad_text
    assert "Bricks104_normal.cal u v" in rad_text      # texdata keeps u v
    assert (result.width, result.height) == (64, 32)

    # Portrait: v stretches instead.
    src2 = tmp_path / "in" / "tall"
    _make_png(src2 / "tall_diff.png", (90, 90, 90), size=(16, 48))
    result2 = convert_set(discover(src2), tmp_path / "out2", ConvertOptions(projection="uv"))
    cal2 = result2.cal_file.read_text()
    assert "pic_u = u * 1;" in cal2
    assert "pic_v = v * 3;" in cal2


def test_convert_set_end_to_end(tmp_path: Path) -> None:
    src = tmp_path / "in" / "wood_floor"
    _make_png(src / "wood_floor_diff_2k.png", (180, 140, 90))
    _make_png(src / "wood_floor_rough_2k.png", (128, 128, 128))
    _make_png(src / "wood_floor_nor_gl_2k.png", (128, 128, 255))

    pbr = discover(src)
    out = tmp_path / "out"
    out.mkdir()
    # varying_roughness is opt-in (it modulates the diffuse, not the roughness);
    # this test exercises the full legacy chain explicitly.
    result = convert_set(pbr, out, ConvertOptions(projection="uv", varying_roughness=True))

    assert result.rad_file.exists()
    assert result.cal_file.exists()
    assert result.hdr_file.exists()
    assert result.primitive == "plastic"
    # roughness map was mid-gray (128) → ~0.5
    assert abs(result.roughness - 0.5) < 0.01

    rad_text = result.rad_file.read_text()
    # Normal map and roughness map are present, so full chain is used
    assert "texdata wood_floor_tex" in rad_text
    assert "9 dx_func dy_func dz_func" in rad_text
    assert "brightdata wood_floor_rough" in rad_text
    assert "wood_floor_rough plastic wood_floor" in rad_text
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


# ---------------------------------------------------------------------------
# Material characteristics, overrides, and the (corrected) albedo scaling
# ---------------------------------------------------------------------------

def test_plastic_hdr_is_not_prescaled_and_readout_matches_radiance(tmp_path: Path) -> None:
    """Radiance makes plastic diffuse = C × (1 − spec) itself, so the .hdr must
    carry C unscaled (the old 1−spec pre-scale darkened plastics by 5 %)."""
    from pbr2rad.hdr import visible
    src = tmp_path / "in" / "grey"
    _make_png(src / "grey_diff.png", (128, 128, 128), size=(8, 8))   # sRGB 128 → 0.2158 linear
    result = convert_set(discover(src), tmp_path / "out", ConvertOptions(projection="uv"))
    assert result.primitive == "plastic"
    assert abs(result.specularity - 0.05) < 1e-9
    assert abs(result.avg_rgb[0] - 0.2158) < 0.002
    refl = result.reflectance
    assert abs(refl["diffuse_rgb"][0] - 0.2158 * 0.95) < 0.003
    assert refl["specular_rgb"] == [0.05, 0.05, 0.05]
    assert abs(refl["diffuse_vis"] - visible(refl["diffuse_rgb"])) < 1e-3
    assert abs(refl["total_vis"] - (refl["diffuse_vis"] + 0.05)) < 1e-3
    assert result.avg_srgb_hex == "#808080"
    # The .hdr encodes ~0.2158, not 0.2158 × 0.95: the header's first pixel
    # exponent/mantissa are easier to check through the manifest + a re-read.
    manifest = json.loads(write_manifest([result], tmp_path / "out").read_text())
    entry = manifest["materials"][0]
    assert entry["specularity"] == 0.05
    assert entry["reflectance"]["total_vis"] == refl["total_vis"]
    assert entry["avg_srgb_hex"] == "#808080"
    rad = result.rad_file.read_text()
    assert "5 1 1 1 0.05 " in rad


def test_specularity_and_diffuse_overrides(tmp_path: Path) -> None:
    src = tmp_path / "in" / "grey"
    _make_png(src / "grey_diff.png", (128, 128, 128), size=(8, 8))
    opts = ConvertOptions(projection="uv", specularity_override=0.2, diffuse_scale=1.5,
                          roughness_override=0.5)
    result = convert_set(discover(src), tmp_path / "out", opts)
    assert result.specularity == 0.2
    assert result.diffuse_scale == 1.5
    assert "5 1 1 1 0.2 0.25" in result.rad_file.read_text()     # spec, alpha = 0.5²
    refl = result.reflectance
    assert abs(refl["diffuse_rgb"][0] - 0.2158 * 1.5 * 0.8) < 0.004
    assert refl["specular_rgb"] == [0.2, 0.2, 0.2]
    assert result.roughness_radiance == 0.25


def test_metal_reflectance_split(tmp_path: Path) -> None:
    src = tmp_path / "in" / "steel"
    _make_png(src / "steel_diff.png", (200, 200, 200), size=(8, 8))
    _make_png(src / "steel_metal.png", (255, 255, 255), size=(8, 8))
    result = convert_set(discover(src), tmp_path / "out", ConvertOptions(projection="uv"))
    assert result.primitive == "metal" and result.specularity == 1.0
    refl = result.reflectance
    assert refl["diffuse_rgb"] == [0.0, 0.0, 0.0]
    assert abs(refl["specular_rgb"][0] - result.avg_rgb[0]) < 1e-3
    assert abs(refl["total_vis"] - refl["specular_vis"]) < 1e-9


def test_material_reflectance_clamps_boost(tmp_path: Path) -> None:
    from pbr2rad.convert import material_reflectance
    r = material_reflectance((0.9, 0.9, 0.9), primitive="plastic", specularity=0.05, diffuse_scale=2.0)
    assert r["diffuse_rgb"] == [0.95, 0.95, 0.95]      # C clipped to 1.0 before × (1 − spec)
    assert r["total_vis"] == 1.0


def test_varying_roughness_is_opt_in(tmp_path: Path) -> None:
    """Default chain has no brightdata: the roughness map only feeds the scalar
    roughness (a pattern would scale the diffuse reflectance, not roughness)."""
    src = tmp_path / "in" / "m"
    _make_png(src / "m_diff.png", (128, 128, 128), size=(8, 8))
    _make_png(src / "m_rough.png", (200, 200, 200), size=(8, 8))
    _make_png(src / "m_nor_gl.png", (128, 128, 255), size=(8, 8))
    result = convert_set(discover(src), tmp_path / "out", ConvertOptions(projection="uv"))
    rad = result.rad_file.read_text()
    assert "brightdata" not in rad and "texdata" in rad
    assert "roughness" in result.channels_used           # still used for the scalar
    assert abs(result.roughness - 200 / 255) < 0.01
