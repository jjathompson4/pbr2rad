"""Tests for the preview render helpers that don't need Radiance."""

from __future__ import annotations

import math

import numpy as np
import pytest

from pbr2rad.web import preview


def test_sphere_disk_radius_matches_camera_geometry():
    # |vp| = sqrt(2.6² + 2.9² + 2.1²) ≈ 4.425 → angular radius asin(1/4.425);
    # vh = 27.5° → focal = (size/2) / tan(13.75°). Sphere fills ~95 % of the
    # frame, like the source sites' reference renders (~96 %).
    size = 384
    dist = math.sqrt(2.6**2 + 2.9**2 + 2.1**2)
    expected = (size / 2) / math.tan(math.radians(13.75)) * math.tan(math.asin(1 / dist))
    assert preview.sphere_disk_radius(size) == pytest.approx(expected)
    assert 0.92 * size / 2 < expected < 0.98 * size / 2


def test_sphere_mask_is_centred_disk():
    size = 128
    m = preview.sphere_mask(size)
    assert m.shape == (size, size)
    c = (size - 1) / 2
    assert m[int(c), int(c)]                 # centre inside
    assert not m[0, 0] and not m[0, -1] and not m[-1, 0] and not m[-1, -1]
    # Area ≈ π r² for r = radius × shrink
    r = preview.sphere_disk_radius(size) * preview._MASK_SHRINK
    assert m.sum() == pytest.approx(math.pi * r * r, rel=0.05)
    # Symmetric
    assert np.array_equal(m, m[::-1, :]) and np.array_equal(m, m[:, ::-1])


def test_sphere_alpha_opaque_inside_transparent_outside():
    size = 96
    a = preview.sphere_alpha(size)
    assert a.dtype == np.uint8 and a.shape == (size, size)
    c = size // 2
    assert a[c, c] == 255
    assert a[0, 0] == 0
    r = preview.sphere_disk_radius(size)
    # Just inside the rim: opaque; a few px outside: transparent.
    assert a[c, int(c + r - 3)] == 255
    assert a[c, min(size - 1, int(c + r + 4))] == 0


class TestAutoExposure:
    def test_mid_grey_hits_median_target(self):
        lum = np.full(1000, 0.1)                       # median 0.1, p95 0.1
        ev = preview.auto_exposure(lum)
        # T_MID/median = 2.7 ; T_HI/p95 = 9.4 → the median rule wins.
        assert ev == pytest.approx(preview.EXPOSURE_T_MID / 0.1)

    def test_highlights_cap_exposure(self):
        lum = np.concatenate([np.full(900, 0.01), np.full(100, 1.0)])  # dark + 10% bright
        ev = preview.auto_exposure(lum)
        # median 0.01 → 27×, but p95 = 1.0 → T_HI / 1.0 = 0.94 → highlight rule wins.
        assert ev == pytest.approx(preview.EXPOSURE_T_HI / 1.0)

    def test_clamps(self):
        assert preview.auto_exposure(np.full(10, 1e-6)) == preview.EXPOSURE_MAX
        assert preview.auto_exposure(np.full(10, 50.0)) == preview.EXPOSURE_MIN

    def test_fallback_on_empty_or_degenerate(self):
        assert preview.auto_exposure(np.array([])) == preview.EXPOSURE_FALLBACK
        assert preview.auto_exposure(np.zeros(10)) == preview.EXPOSURE_FALLBACK
        assert preview.auto_exposure(np.array([np.nan, np.inf])) == preview.EXPOSURE_FALLBACK

    def test_real_world_shapes(self):
        # Shapes measured on real renders: a dim diffuse brick (median 0.03,
        # p95 0.10) gets lifted ~8×; a metal with bright speculars is held by
        # the p95 rule rather than blown out.
        rng = np.random.default_rng(0)
        brick = rng.uniform(0.01, 0.1, 5000)
        ev_brick = preview.auto_exposure(brick)
        assert 4 < ev_brick < 12
        metal = np.concatenate([rng.uniform(0.0, 0.02, 4000), rng.uniform(0.3, 1.3, 1000)])
        ev_metal = preview.auto_exposure(metal)
        assert ev_metal < 2.0


class TestRigNeutrality:
    """The rig must be white-balanced: a grey card renders grey."""

    @pytest.mark.parametrize("normal", [
        (0.587, -0.656, 0.475),   # facing the camera
        (0.0, 0.0, 1.0),          # up
        (-0.6, -0.6, 0.8),        # facing the key
        (0.7, -0.5, 0.3),         # facing the fill
        (0.0, 0.0, -1.0),         # down (env only)
    ])
    def test_irradiance_is_neutral(self, normal):
        e = preview.rig_irradiance(normal)
        assert min(e) > 0
        assert preview.rig_neutrality(normal) <= 1.01

    def test_every_light_is_grey(self):
        for c in (preview.KEY_RADIANCE, preview.FILL_RADIANCE,
                  preview.SKY_RADIANCE, preview.ENV_RADIANCE):
            assert len(set(c)) == 1, c

    def test_key_and_sky_shape_the_light(self):
        # Key-facing surfaces get more light than down-facing ones, but the
        # rig is soft: the ratio stays moderate (flat, reference-like shading).
        key = preview.rig_irradiance((-0.6, -0.6, 0.8))[1]
        down = preview.rig_irradiance((0, 0, -1))[1]
        assert key > down > 0
        assert key / down < 8

    def test_fixed_exposure_from_rig(self):
        e = preview.rig_irradiance(preview._camera_facing_normal())
        e_vis = 0.265 * e[0] + 0.670 * e[1] + 0.065 * e[2]
        assert preview.fixed_exposure() == pytest.approx(preview.EXPOSURE_ALBEDO_GAIN * math.pi / e_vis)
        assert 1.0 < preview.fixed_exposure() < 6.0

    def test_exposure_mode_env(self, monkeypatch):
        monkeypatch.delenv("PBR2RAD_PREVIEW_EXPOSURE", raising=False)
        assert preview.exposure_mode() == "fixed"
        monkeypatch.setenv("PBR2RAD_PREVIEW_EXPOSURE", "auto")
        assert preview.exposure_mode() == "auto"

    def test_apply_look_keeps_grey_grey_and_boosts_colour(self):
        from PIL import Image
        grey = Image.new("RGBA", (8, 8), (120, 120, 120, 255))
        out = preview.apply_look(grey, 1.5)
        assert out.getpixel((4, 4)) == (120, 120, 120, 255)
        wood = Image.new("RGBA", (8, 8), (171, 139, 111, 255))
        out = preview.apply_look(wood, 1.25)
        r, g, b, a = out.getpixel((4, 4))
        assert a == 255 and r > 171 and b < 111          # more chroma, alpha kept
        assert preview.apply_look(wood, 1.0).getpixel((4, 4)) == (171, 139, 111, 255)

    def test_old_rig_would_have_failed(self, monkeypatch):
        """Regression guard: the pre-round-4 rig is measurably blue."""
        monkeypatch.setattr(preview, "KEY_RADIANCE", (5.0, 4.5, 4.0))
        monkeypatch.setattr(preview, "KEY_ANGLE_DEG", 8.0)
        monkeypatch.setattr(preview, "FILL_RADIANCE", (1.0, 1.2, 1.5))
        monkeypatch.setattr(preview, "FILL_ANGLE_DEG", 30.0)
        monkeypatch.setattr(preview, "SKY_RADIANCE", (0.4, 0.5, 0.7))
        monkeypatch.setattr(preview, "ENV_RADIANCE", (0.06, 0.06, 0.066))
        assert preview.rig_neutrality((0.587, -0.656, 0.475)) > 1.5


def test_scene_text_has_sphere_lights_and_env():
    text = preview._scene_text("mat")
    assert "mat sphere ball" in text
    assert "key_l source key" in text and "fill_l source fill" in text
    assert "void glow sky_g" in text and "sky_g source sky" in text   # sky is a glow (seen by reflections)
    assert "3 2.4 2.4 2.4" in text          # neutral key
    assert "-0.6 -0.6 0.8 40" in text      # broad soft key disc
    # Environment is a distant glow source on the lower hemisphere — not an
    # enclosing sphere, which would shadow the light sources.
    assert "void glow env_g" in text
    assert "env_g source env\n0\n0\n4 0 0 -1 180" in text


def test_render_preview_without_radiance(monkeypatch, tmp_path):
    monkeypatch.setattr(preview, "radiance_available", lambda: False)
    assert preview.render_preview(tmp_path, tmp_path / "x.rad", tmp_path / "p.png") is False
