"""Tests for normal map support (texdata) and roughness (brightdata)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pbr2rad.cal import tangent_frame
from pbr2rad.normal import (
    convert_normal_to_dat,
    convert_roughness_to_dat,
    detect_convention,
    generate_normal_cal,
    generate_roughness_cal,
    orientation_remap,
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
        """Data is laid out column-major: one line per X column, H values per
        line. Source image (row-major PIL scanlines):
            (0.1, 0.2, 0.3)    <- y=0
            (0.4, 0.5, 0.6)    <- y=1
        File output (3 columns, each a 2-entry Y slice):
            col x=0: 0.1, 0.4
            col x=1: 0.2, 0.5
            col x=2: 0.3, 0.6
        """
        dat_path = tmp_path / "vals.dat"
        data = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        write_dat_2d(dat_path, data, 3, 2)

        lines = dat_path.read_text().strip().split("\n")
        assert len(lines) == 3 + 3  # header(3) + 3 x-columns
        col0 = [float(v) for v in lines[3].split()]
        col2 = [float(v) for v in lines[5].split()]
        assert col0 == pytest.approx([0.1, 0.4], abs=1e-3)
        assert col2 == pytest.approx([0.3, 0.6], abs=1e-3)

    def test_compact_format(self, tmp_path):
        """Values are written with 3 decimal places, not full float precision."""
        dat_path = tmp_path / "compact.dat"
        data = [0.501961, 0.247059]  # typical 8-bit normal map values
        write_dat_2d(dat_path, data, 2, 1)
        text = dat_path.read_text()
        lines = text.strip().split("\n")
        # Column-major: 2 columns x 1 row → one value per line, 2 data lines.
        for line in lines[3:]:
            for v in line.split("\t"):
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
        assert "nm_px(r,g) = (2*r - 1);" in cal
        assert "nm_py(r,g) = (2*g - 1);" in cal

    def test_dx_convention_flips_y(self):
        cal = generate_normal_cal("wood", is_dx=True)
        assert "nm_px(r,g) = (2*r - 1);" in cal
        assert "nm_py(r,g) = -(2*g - 1);" in cal

    def test_perturbation_is_built_in_the_tangent_frame(self):
        """x along T (u grows), y along B (v grows), z - 1 along the normal."""
        cal = generate_normal_cal("wood")
        assert "dx_func(r,g,b) = A1 * (nm_px(r,g)*nm_tx + nm_py(r,g)*nm_bx + (2*b - 2)*Nx);" in cal
        assert "dy_func(r,g,b) = A1 * (nm_px(r,g)*nm_ty + nm_py(r,g)*nm_by + (2*b - 2)*Ny);" in cal
        assert "dz_func(r,g,b) = A1 * (nm_px(r,g)*nm_tz + nm_py(r,g)*nm_bz + (2*b - 2)*Nz);" in cal
        assert tangent_frame("box") in cal          # the default frame

    def test_remap_turns_the_texel_vector(self):
        cal = generate_normal_cal("wood", remap=orientation_remap(0, flip_h=True))
        assert "nm_px(r,g) = -(2*r - 1);" in cal and "nm_py(r,g) = (2*g - 1);" in cal
        cal = generate_normal_cal("wood", is_dx=True, remap=orientation_remap(90))
        assert "nm_px(r,g) = (2*g - 1);" in cal and "nm_py(r,g) = (2*r - 1);" in cal

    def test_functions_take_rgb_args(self):
        cal = generate_normal_cal("wood")
        assert "dx_func(r,g,b)" in cal
        assert "dy_func(r,g,b)" in cal
        assert "dz_func(r,g,b)" in cal

    def test_references_A1(self):
        cal = generate_normal_cal("wood")
        assert "A1" in cal


class TestOrientationRemap:
    """The picture is flipped (h, then v) and then rotated CCW; a texel's
    (x right, y up) vector must turn the same way."""

    @pytest.mark.parametrize("rot, fh, fv, expected", [
        (0, False, False, (1, 0, 0, 1)),
        (0, True, False, (-1, 0, 0, 1)),
        (0, False, True, (1, 0, 0, -1)),
        (90, False, False, (0, -1, 1, 0)),       # right -> up
        (180, False, False, (-1, 0, 0, -1)),
        (270, False, False, (0, 1, -1, 0)),      # right -> down
        (270, False, True, (0, -1, -1, 0)),
        (-90, False, False, (0, 1, -1, 0)),
    ])
    def test_matrix(self, rot, fh, fv, expected):
        assert orientation_remap(rot, flip_h=fh, flip_v=fv) == pytest.approx(expected)
        assert all(v != 0 or str(v) == "0.0" for v in orientation_remap(rot, flip_h=fh, flip_v=fv))  # no -0.0


class TestTangentFrame:
    @pytest.mark.parametrize("mode", ["uv", "planar", "box", "cylindrical", "spherical"])
    def test_defines_the_frame(self, mode):
        text = tangent_frame(mode)
        for v in ("nm_tx", "nm_ty", "nm_tz", "nm_bx", "nm_by", "nm_bz"):
            assert f"\n{v} = " in text

    def test_negative_scale_flips_its_axis(self):
        assert "nm_rtx = 1;" in tangent_frame("planar", axis="xz")
        assert "nm_rtx = -1;" in tangent_frame("planar", axis="xz", u_scale=-2)
        assert "nm_rbz = -1;" in tangent_frame("planar", axis="xz", v_scale=-0.5)

    def test_uv_mode_uses_the_box_frame(self):
        uv, box = tangent_frame("uv"), tangent_frame("box")
        assert uv.split("\n", 1)[1] == box.split("\n", 1)[1]
        assert tangent_frame("uv", u_scale=-1) == uv        # mesh UVs ignore the scales

    def test_spherical_swap_exchanges_the_axes(self):
        plain, swapped = tangent_frame("spherical"), tangent_frame("spherical", swap_uv=True)
        assert "nm_rtx = -Py;" in plain and "nm_rbz = Px*Px + Py*Py;" in plain
        assert "nm_rbx = -Py;" in swapped and "nm_rtz = Px*Px + Py*Py;" in swapped

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            tangent_frame("cubic")


@pytest.mark.skipif(not __import__("shutil").which("rtrace"), reason="Radiance not installed")
class TestNormalsFollowTheSurface:
    """Trace a constant normal-map texel with Radiance and check the shading
    normal leans along the TEXTURE's u/v on every face, not along world axes
    (until 2026-11-10 only +Z faces were right: walls were 24-29 deg off)."""

    TEXEL = (179, 166, 238)     # decodes to about (0.404, 0.302, 0.867)

    def _material(self, tmp_path, **opts):
        from pbr2rad.convert import ConvertOptions, convert_set
        from pbr2rad.discover import discover

        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (16, 16), (128, 128, 128)).save(src / "t_albedo.png")
        dx = opts.pop("dx", False)
        Image.new("RGB", (16, 16), self.TEXEL).save(src / ("t_nor_dx.png" if dx else "t_nor_gl.png"))
        res = convert_set(discover(src), tmp_path / "out", ConvertOptions(
            normal=True, estimate_maps=False, write_pvw=False, bump_scale=1.0, **opts))
        vals = []
        for c in "rgb":
            vals.append(float((res.out_dir / f"{res.name}_nor_{c}.dat").read_text().split()[7]))
        texel = (2 * vals[0] - 1, (-1 if dx else 1) * (2 * vals[1] - 1), 2 * vals[2] - 1)
        return res, texel

    def _trace(self, res, geometry, rays):
        import os
        import subprocess

        env = {**os.environ, "RAYPATH": f".:{os.environ.get('RAYPATH', '/usr/local/radiance/lib')}"}
        (res.out_dir / "geo.rad").write_text(geometry)
        octree = subprocess.run(["oconv", res.rad_file.name, "geo.rad"], cwd=res.out_dir,
                                capture_output=True, env=env, check=True).stdout
        (res.out_dir / "t.oct").write_bytes(octree)
        out = subprocess.run(["rtrace", "-h", "-on", "t.oct"], cwd=res.out_dir, capture_output=True,
                             text=True, env=env, input="\n".join(rays), check=True).stdout
        return [np.array([float(x) for x in line.split()]) for line in out.strip().splitlines()]

    @staticmethod
    def _expect(texel, T, B, N, remap=(1, 0, 0, 1)):
        x, y, z = texel
        x, y = remap[0] * x + remap[1] * y, remap[2] * x + remap[3] * y
        n = x * np.array(T, float) + y * np.array(B, float) + z * np.array(N, float)
        return n / np.linalg.norm(n)

    # Box projection: X-facing -> u=Py v=Pz, Y-facing -> u=Px v=Pz, Z-facing -> u=Px v=Py.
    FACES = {
        (0, 0, 1): ((1, 0, 0), (0, 1, 0)), (0, 0, -1): ((1, 0, 0), (0, 1, 0)),
        (1, 0, 0): ((0, 1, 0), (0, 0, 1)), (-1, 0, 0): ((0, 1, 0), (0, 0, 1)),
        (0, 1, 0): ((1, 0, 0), (0, 0, 1)), (0, -1, 0): ((1, 0, 0), (0, 0, 1)),
    }

    def _box_scene(self, name):
        geo, rays = [], []
        for i, n in enumerate(self.FACES):
            c = np.array([3.0 * i + 0.37, 0.41, 0.29])
            n = np.array(n, float)
            a = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
            e1 = a - a.dot(n) * n
            e1 /= np.linalg.norm(e1)
            e2 = np.cross(n, e1)
            pts = [c + 0.1 * (sx * e1 + sy * e2) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            geo.append(f"{name} polygon f{i}\n0\n0\n12 " + " ".join(f"{v:.6f}" for p in pts for v in p) + "\n")
            rays.append(" ".join(f"{v:.6f}" for v in (*(c + 0.5 * n), *(-n))))
        return "\n".join(geo), rays

    @pytest.mark.parametrize("opts, remap", [
        ({}, (1, 0, 0, 1)),
        ({"dx": True}, (1, 0, 0, 1)),
        ({"flip_h": True}, (-1, 0, 0, 1)),
        ({"rotate_per_map": {"normal_gl": 90}}, (0, -1, 1, 0)),
    ])
    def test_box_projection_every_face(self, tmp_path, opts, remap):
        res, texel = self._material(tmp_path, projection="box", **opts)
        geo, rays = self._box_scene(res.name)
        for (n, (T, B)), got in zip(self.FACES.items(), self._trace(res, geo, rays)):
            want = self._expect(texel, T, B, n, remap)
            assert np.degrees(np.arccos(np.clip(want.dot(got / np.linalg.norm(got)), -1, 1))) < 0.5, n

    def test_spherical_projection(self, tmp_path):
        import math

        res, texel = self._material(tmp_path, projection="spherical", u_scale=3, v_scale=1.5)
        rays, frames = [], []
        for az, el in ((40, 20), (130, -35), (220, 60), (300, 5)):
            a, e = math.radians(az), math.radians(el)
            n = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
            east = np.array([-n[1], n[0], 0.0])
            north = np.array([-n[0] * n[2], -n[1] * n[2], n[0] ** 2 + n[1] ** 2])
            frames.append((east / np.linalg.norm(east), north / np.linalg.norm(north), n))
            rays.append(" ".join(f"{v:.6f}" for v in (*(2 * n), *(-n))))
        got = self._trace(res, f"{res.name} sphere ball\n0\n0\n4 0 0 0 1\n", rays)
        for (T, B, N), g in zip(frames, got):
            want = self._expect(texel, T, B, N)
            assert np.degrees(np.arccos(np.clip(want.dot(g / np.linalg.norm(g)), -1, 1))) < 0.5


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
