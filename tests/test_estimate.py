"""Tests for normal and roughness map estimation from albedo."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from pbr2rad.estimate import estimate_normal, estimate_roughness


def _make_albedo(path: Path, size: int = 32, color=(180, 140, 100)) -> None:
    Image.new("RGB", (size, size), color).save(path)


def _make_checkerboard(path: Path, size: int = 32, squares: int = 4) -> None:
    """High-contrast checkerboard — should produce strong normals/roughness."""
    img = Image.new("L", (size, size))
    sq = size // squares
    for y in range(size):
        for x in range(size):
            if ((x // sq) + (y // sq)) % 2 == 0:
                img.putpixel((x, y), 240)
            else:
                img.putpixel((x, y), 20)
    img.save(path)


# ---------------------------------------------------------------------------
# Normal map estimation
# ---------------------------------------------------------------------------

class TestEstimateNormal:
    def test_output_is_rgb_png(self, tmp_path):
        albedo = tmp_path / "albedo.png"
        _make_albedo(albedo)
        out = tmp_path / "normal.png"
        result = estimate_normal(albedo, out)
        assert result == out
        assert out.exists()
        img = Image.open(out)
        assert img.mode == "RGB"
        assert img.size == (32, 32)

    def test_uniform_albedo_mostly_blue(self, tmp_path):
        """Uniform color → flat surface → normals point outward (0.5, 0.5, 1.0)."""
        albedo = tmp_path / "flat.png"
        _make_albedo(albedo, color=(128, 128, 128))
        out = tmp_path / "normal.png"
        estimate_normal(albedo, out)
        img = Image.open(out)
        # Sample center pixel — should be near (128, 128, 255)
        r, g, b = img.getpixel((16, 16))
        assert abs(r - 128) < 10, f"R={r}, expected ~128"
        assert abs(g - 128) < 10, f"G={g}, expected ~128"
        assert b > 200, f"B={b}, expected >200 (outward-facing)"

    def test_checkerboard_has_edge_normals(self, tmp_path):
        """Checkerboard edges should produce non-flat normals."""
        albedo = tmp_path / "checker.png"
        _make_checkerboard(albedo, size=64)
        out = tmp_path / "normal.png"
        estimate_normal(albedo, out, strength=2.0)
        img = Image.open(out)
        data = img.tobytes()
        # Check that not all pixels are identical (edges should differ)
        unique = set(data[::3])  # sample R channel
        assert len(unique) > 3, "Expected variation in normal map R channel"

    def test_strength_controls_intensity(self, tmp_path):
        """Higher strength = more deviation from flat."""
        albedo = tmp_path / "checker.png"
        _make_checkerboard(albedo)

        out_low = tmp_path / "normal_low.png"
        out_high = tmp_path / "normal_high.png"
        estimate_normal(albedo, out_low, strength=0.5)
        estimate_normal(albedo, out_high, strength=3.0)

        low_data = Image.open(out_low).tobytes()
        high_data = Image.open(out_high).tobytes()

        # High strength should have more deviation from center (128)
        def mean_deviation(data):
            return sum(abs(b - 128) for b in data[::3]) / (len(data) // 3)

        assert mean_deviation(high_data) > mean_deviation(low_data)


# ---------------------------------------------------------------------------
# Roughness map estimation
# ---------------------------------------------------------------------------

class TestEstimateRoughness:
    def test_output_is_grayscale_png(self, tmp_path):
        albedo = tmp_path / "albedo.png"
        _make_albedo(albedo)
        out = tmp_path / "rough.png"
        result = estimate_roughness(albedo, out)
        assert result == out
        assert out.exists()
        img = Image.open(out)
        assert img.mode == "L"
        assert img.size == (32, 32)

    def test_uniform_albedo_low_roughness(self, tmp_path):
        """Uniform color → no texture variance → low roughness."""
        albedo = tmp_path / "flat.png"
        _make_albedo(albedo, color=(128, 128, 128))
        out = tmp_path / "rough.png"
        estimate_roughness(albedo, out)
        img = Image.open(out)
        data = img.tobytes()
        avg = sum(data) / len(data)
        # Should be near the minimum roughness floor (0.2 * 255 ≈ 51)
        assert avg < 100, f"avg={avg}, expected low roughness for uniform albedo"

    def test_checkerboard_higher_roughness(self, tmp_path):
        """Checkerboard has high contrast → higher roughness."""
        albedo = tmp_path / "checker.png"
        _make_checkerboard(albedo, size=64)
        out = tmp_path / "rough.png"
        estimate_roughness(albedo, out)
        img = Image.open(out)
        data = img.tobytes()
        avg = sum(data) / len(data)
        # Should be higher than uniform
        assert avg > 80, f"avg={avg}, expected higher roughness for checkerboard"

    def test_values_in_valid_range(self, tmp_path):
        albedo = tmp_path / "albedo.png"
        _make_albedo(albedo)
        out = tmp_path / "rough.png"
        estimate_roughness(albedo, out)
        data = Image.open(out).tobytes()
        assert all(0 <= b <= 255 for b in data)


# ---------------------------------------------------------------------------
# Integration: estimation flows into convert pipeline
# ---------------------------------------------------------------------------

class TestEstimationIntegration:
    def test_single_albedo_produces_full_chain(self, tmp_path):
        """A single albedo with estimate_maps=True generates texdata + brightdata."""
        from pbr2rad.convert import ConvertOptions, convert_set
        from pbr2rad.discover import PBRSet

        mat_dir = tmp_path / "brick"
        mat_dir.mkdir()
        albedo = mat_dir / "brick_diff.png"
        _make_checkerboard(albedo, size=32)
        # Save as RGB since convert expects RGB albedo
        Image.open(albedo).convert("RGB").save(albedo)

        pbr = PBRSet(name="brick", root=mat_dir, maps={"albedo": albedo})

        out = tmp_path / "out"
        opts = ConvertOptions(
            estimate_maps=True,
            normal=True,
            varying_roughness=True,
        )
        result = convert_set(pbr, out, opts)

        rad_text = result.rad_file.read_text()
        # Full chain: colorpict → texdata → brightdata → plastic
        assert "colorpict" in rad_text
        assert "texdata" in rad_text
        assert "brightdata" in rad_text

    def test_no_estimate_flag_skips(self, tmp_path):
        """estimate_maps=False with no maps → flat material."""
        from pbr2rad.convert import ConvertOptions, convert_set
        from pbr2rad.discover import PBRSet

        mat_dir = tmp_path / "brick"
        mat_dir.mkdir()
        albedo = mat_dir / "brick_diff.png"
        Image.new("RGB", (16, 16), (180, 140, 100)).save(albedo)

        pbr = PBRSet(name="brick", root=mat_dir, maps={"albedo": albedo})

        out = tmp_path / "out"
        opts = ConvertOptions(estimate_maps=False)
        result = convert_set(pbr, out, opts)

        rad_text = result.rad_file.read_text()
        assert "texdata" not in rad_text
        assert "brightdata" not in rad_text
