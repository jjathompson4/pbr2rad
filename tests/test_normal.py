"""Tests for normal map support (texdata) and roughness (brightdata)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from pbr2rad.normal import (
    convert_normal_to_dat,
    convert_roughness_to_dat,
    detect_convention,
    generate_normal_cal,
    generate_roughness_cal,
    write_dat_2d,
)
from pbr2rad.rad import MaterialParams, generate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_normal_png(path: Path, width: int = 4, height: int = 4) -> None:
    """Create a synthetic normal map (flat surface = (128, 128, 255) blue)."""
    img = Image.new("RGB", (width, height), (128, 128, 255))
    # Put a non-flat normal in one corner to make it interesting
    img.putpixel((0, 0), (255, 128, 128))  # tilted +X
    img.putpixel((1, 0), (0, 128, 128))    # tilted -X
    img.save(path)


# ---------------------------------------------------------------------------
# Tests: .dat file writing
# ---------------------------------------------------------------------------

class TestWriteDat2D:
    def test_basic_write(self, tmp_path):
        dat_path = tmp_path / "test.dat"
        data = [0.0, 0.5, 1.0, 0.25]
        write_dat_2d(dat_path, data, 2, 2)

        text = dat_path.read_text()
        lines = text.strip().split("\n")
        assert lines[0] == "2"        # 2 dimensions
        assert "2" in lines[1]        # width
        assert "2" in lines[2]        # height
        # Data starts at line 3
        assert len(lines) == 5  # header(3) + 2 rows of data

    def test_dimension_mismatch_raises(self, tmp_path):
        with pytest.raises(ValueError, match="does not match"):
            write_dat_2d(tmp_path / "bad.dat", [1.0, 2.0], 3, 3)

    def test_values_preserved(self, tmp_path):
        dat_path = tmp_path / "vals.dat"
        data = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        write_dat_2d(dat_path, data, 3, 2)

        text = dat_path.read_text()
        lines = text.strip().split("\n")
        # Row 0: 0.100, 0.200, 0.300
        row0 = [float(v) for v in lines[3].split()]
        assert len(row0) == 3
        assert abs(row0[0] - 0.1) < 1e-3
        assert abs(row0[2] - 0.3) < 1e-3

    def test_compact_format(self, tmp_path):
        """Values are written with 3 decimal places, not full float precision."""
        dat_path = tmp_path / "compact.dat"
        data = [0.501961, 0.247059]  # typical 8-bit normal map values
        write_dat_2d(dat_path, data, 2, 1)
        text = dat_path.read_text()
        lines = text.strip().split("\n")
        row = lines[3]
        # Each value should be like "0.502" not "0.501961"
        vals = row.split("\t")
        for v in vals:
            assert len(v) <= 5, f"value {v!r} too long — should be compact"


# ---------------------------------------------------------------------------
# Tests: normal map conversion
# ---------------------------------------------------------------------------

class TestConvertNormalToDat:
    def test_produces_three_dat_files(self, tmp_path):
        png = tmp_path / "test_nor_gl.png"
        _make_normal_png(png)

        r, g, b, w, h = convert_normal_to_dat(png, tmp_path, "test")
        assert (tmp_path / r).exists()
        assert (tmp_path / g).exists()
        assert (tmp_path / b).exists()
        assert w == 4
        assert h == 4

    def test_dat_filenames(self, tmp_path):
        png = tmp_path / "mat_nor_gl.png"
        _make_normal_png(png)

        r, g, b, _, _ = convert_normal_to_dat(png, tmp_path, "mat")
        assert r == "mat_nor_r.dat"
        assert g == "mat_nor_g.dat"
        assert b == "mat_nor_b.dat"

    def test_channel_values_in_range(self, tmp_path):
        png = tmp_path / "test_nor.png"
        _make_normal_png(png)

        r_name, _, _, _, _ = convert_normal_to_dat(png, tmp_path, "test")
        text = (tmp_path / r_name).read_text()
        # Skip header (3 lines), parse data
        lines = text.strip().split("\n")[3:]
        for line in lines:
            for val in line.split():
                f = float(val)
                assert 0.0 <= f <= 1.0, f"value {f} out of range"


# ---------------------------------------------------------------------------
# Tests: .cal generation
# ---------------------------------------------------------------------------

class TestGenerateNormalCal:
    def test_contains_perturbation_functions(self):
        cal = generate_normal_cal("wood")
        assert "dx_func" in cal
        assert "dy_func" in cal
        assert "dz_func" in cal

    def test_gl_convention_no_flip(self):
        cal = generate_normal_cal("wood", is_dx=False)
        # GL convention: no flip on Y
        assert "1 * (2*g" in cal or "-1" not in cal.split("dy_func")[1].split(";")[0].replace("- 1", "")

    def test_dx_convention_flips_y(self):
        cal = generate_normal_cal("wood", is_dx=True)
        assert "-1" in cal  # flip_y = -1

    def test_functions_take_rgb_args(self):
        cal = generate_normal_cal("wood")
        assert "dx_func(r,g,b)" in cal
        assert "dy_func(r,g,b)" in cal
        assert "dz_func(r,g,b)" in cal

    def test_references_A1(self):
        cal = generate_normal_cal("wood")
        assert "A1" in cal


# ---------------------------------------------------------------------------
# Tests: convention detection
# ---------------------------------------------------------------------------

class TestDetectConvention:
    def test_gl_preferred(self):
        maps = {"normal_gl": Path("a.png"), "normal_dx": Path("b.png")}
        assert detect_convention(maps) == "gl"

    def test_dx_when_only_dx(self):
        maps = {"normal_dx": Path("b.png")}
        assert detect_convention(maps) == "dx"

    def test_default_gl(self):
        maps = {"normal": Path("c.png")}
        assert detect_convention(maps) == "gl"

    def test_empty_maps(self):
        assert detect_convention({}) == "gl"


# ---------------------------------------------------------------------------
# Tests: texdata modifier chain in rad.py
# ---------------------------------------------------------------------------

class TestTexdataChain:
    def test_normal_inserts_texdata(self):
        params = MaterialParams(
            name="wood",
            hdr_file="wood.hdr",
            cal_file="wood.cal",
            normal_dat_r="wood_nor_r.dat",
            normal_dat_g="wood_nor_g.dat",
            normal_dat_b="wood_nor_b.dat",
            normal_cal_file="wood_normal.cal",
        )
        rad = generate(params)
        assert "texdata wood_tex" in rad
        assert "9 dx_func dy_func dz_func" in rad
        assert "wood_nor_r.dat" in rad
        assert "wood_nor_g.dat" in rad
        assert "wood_nor_b.dat" in rad
        assert "wood_normal.cal u v" in rad
        # real args: bump scale
        assert "1 1" in rad
        # texdata modifier feeds into plastic
        assert "wood_tex plastic wood" in rad

    def test_no_normal_no_texdata(self):
        params = MaterialParams(
            name="wood",
            hdr_file="wood.hdr",
            cal_file="wood.cal",
        )
        rad = generate(params)
        assert "texdata" not in rad
        assert "wood_pat plastic wood" in rad

    def test_normal_metal_chain(self):
        params = MaterialParams(
            name="chrome",
            hdr_file="chrome.hdr",
            cal_file="chrome.cal",
            metalness=0.9,
            normal_dat_r="chrome_nor_r.dat",
            normal_dat_g="chrome_nor_g.dat",
            normal_dat_b="chrome_nor_b.dat",
            normal_cal_file="chrome_normal.cal",
        )
        rad = generate(params)
        assert "chrome_tex metal chrome" in rad


# ---------------------------------------------------------------------------
# Tests: end-to-end with convert pipeline
# ---------------------------------------------------------------------------

class TestConvertWithNormal:
    def test_convert_produces_normal_files(self, tmp_path):
        """Full pipeline: synthetic PBR set with normal map."""
        from pbr2rad.convert import ConvertOptions, convert_set
        from pbr2rad.discover import PBRSet

        # Create synthetic PBR set
        mat_dir = tmp_path / "wood"
        mat_dir.mkdir()
        albedo = mat_dir / "wood_diff_2k.png"
        Image.new("RGB", (16, 16), (180, 140, 100)).save(albedo)
        normal = mat_dir / "wood_nor_gl_2k.png"
        _make_normal_png(normal, 16, 16)

        pbr = PBRSet(
            name="wood",
            root=mat_dir,
            maps={"albedo": albedo, "normal_gl": normal},
        )

        out = tmp_path / "out"
        opts = ConvertOptions(normal=True, bump_scale=1.0)
        result = convert_set(pbr, out, opts)

        # Check normal-specific output files
        mat_out = out / "wood"
        assert (mat_out / "wood_nor_r.dat").exists()
        assert (mat_out / "wood_nor_g.dat").exists()
        assert (mat_out / "wood_nor_b.dat").exists()
        assert (mat_out / "wood_normal.cal").exists()

        # Check .rad references texdata
        rad_text = result.rad_file.read_text()
        assert "texdata" in rad_text

    def test_convert_no_normal_flag(self, tmp_path):
        """--no-normal skips normal map processing."""
        from pbr2rad.convert import ConvertOptions, convert_set
        from pbr2rad.discover import PBRSet

        mat_dir = tmp_path / "wood"
        mat_dir.mkdir()
        albedo = mat_dir / "wood_diff_2k.png"
        Image.new("RGB", (16, 16), (180, 140, 100)).save(albedo)
        normal = mat_dir / "wood_nor_gl_2k.png"
        _make_normal_png(normal, 16, 16)

        pbr = PBRSet(
            name="wood",
            root=mat_dir,
            maps={"albedo": albedo, "normal_gl": normal},
        )

        out = tmp_path / "out"
        opts = ConvertOptions(normal=False)
        result = convert_set(pbr, out, opts)

        rad_text = result.rad_file.read_text()
        assert "texdata" not in rad_text
        assert not (out / "wood" / "wood_nor_r.dat").exists()


# ---------------------------------------------------------------------------
# Tests: spatially varying roughness (brightdata)
# ---------------------------------------------------------------------------

class TestConvertRoughnessToDat:
    def test_produces_dat_file(self, tmp_path):
        rough_png = tmp_path / "rough.png"
        Image.new("L", (8, 8), 128).save(rough_png)

        dat_name, w, h = convert_roughness_to_dat(rough_png, tmp_path, "test")
        assert (tmp_path / dat_name).exists()
        assert dat_name == "test_rough.dat"
        assert w == 8
        assert h == 8

    def test_values_in_range(self, tmp_path):
        rough_png = tmp_path / "rough.png"
        Image.new("L", (4, 4), 200).save(rough_png)

        dat_name, _, _ = convert_roughness_to_dat(rough_png, tmp_path, "test")
        text = (tmp_path / dat_name).read_text()
        lines = text.strip().split("\n")[3:]  # skip header
        for line in lines:
            for val in line.split():
                f = float(val)
                assert 0.0 <= f <= 1.0


class TestGenerateRoughnessCal:
    def test_contains_rough_func(self):
        cal = generate_roughness_cal("stone")
        assert "rough_func" in cal
        assert "A1" in cal

    def test_inverts_roughness(self):
        cal = generate_roughness_cal("stone")
        # rough_func should subtract roughness from 1
        assert "1 - A1" in cal or "1-A1" in cal


class TestBrightdataChain:
    def test_brightdata_in_chain(self):
        from pbr2rad.rad import MaterialParams, generate

        params = MaterialParams(
            name="stone",
            hdr_file="stone.hdr",
            cal_file="stone.cal",
            rough_dat="stone_rough.dat",
            rough_cal_file="stone_rough.cal",
        )
        rad = generate(params)
        assert "brightdata stone_rough" in rad
        assert "rough_func" in rad
        assert "stone_rough.dat" in rad
        assert "stone_rough.cal" in rad
        assert "stone_rough plastic stone" in rad

    def test_no_rough_map_no_brightdata(self):
        from pbr2rad.rad import MaterialParams, generate

        params = MaterialParams(
            name="stone",
            hdr_file="stone.hdr",
            cal_file="stone.cal",
        )
        rad = generate(params)
        assert "brightdata" not in rad

    def test_full_chain_normal_plus_roughness(self):
        from pbr2rad.rad import MaterialParams, generate

        params = MaterialParams(
            name="stone",
            hdr_file="stone.hdr",
            cal_file="stone.cal",
            normal_dat_r="stone_nor_r.dat",
            normal_dat_g="stone_nor_g.dat",
            normal_dat_b="stone_nor_b.dat",
            normal_cal_file="stone_normal.cal",
            rough_dat="stone_rough.dat",
            rough_cal_file="stone_rough.cal",
        )
        rad = generate(params)
        # Full chain: colorpict → texdata → brightdata → plastic
        assert "colorpict stone_pat" in rad
        assert "texdata stone_tex" in rad
        assert "brightdata stone_rough" in rad
        assert "stone_rough plastic stone" in rad

    def test_convert_with_varying_roughness(self, tmp_path):
        from pbr2rad.convert import ConvertOptions, convert_set
        from pbr2rad.discover import PBRSet

        mat_dir = tmp_path / "stone"
        mat_dir.mkdir()
        albedo = mat_dir / "stone_diff_2k.png"
        Image.new("RGB", (16, 16), (150, 140, 130)).save(albedo)
        rough = mat_dir / "stone_rough_2k.png"
        Image.new("L", (16, 16), 180).save(rough)

        pbr = PBRSet(
            name="stone",
            root=mat_dir,
            maps={"albedo": albedo, "roughness": rough},
        )

        out = tmp_path / "out"
        opts = ConvertOptions(varying_roughness=True, normal=False)
        result = convert_set(pbr, out, opts)

        assert (out / "stone" / "stone_rough.dat").exists()
        assert (out / "stone" / "stone_rough.cal").exists()
        rad_text = result.rad_file.read_text()
        assert "brightdata" in rad_text
