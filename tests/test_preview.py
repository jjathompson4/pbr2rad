"""Tests for the preview render helpers that don't need Radiance."""

from __future__ import annotations

import math

import numpy as np
import pytest

from pbr2rad.web import preview


def test_sphere_disk_radius_matches_camera_geometry():
    # |vp| = sqrt(2.6² + 2.9² + 2.1²) ≈ 4.425 → angular radius asin(1/4.425);
    # vh = 42° → focal = (size/2) / tan(21°).
    size = 384
    dist = math.sqrt(2.6**2 + 2.9**2 + 2.1**2)
    expected = (size / 2) / math.tan(math.radians(21)) * math.tan(math.asin(1 / dist))
    assert preview.sphere_disk_radius(size) == pytest.approx(expected)
    assert 0.55 * size / 2 < expected < 0.65 * size / 2     # ≈ 0.605 × half-size


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


def test_scene_text_has_sphere_lights_and_env():
    text = preview._scene_text("mat")
    assert "mat sphere ball" in text
    assert "key_l source key" in text and "fill_l source fill" in text
    assert "sky_dome source sky" in text
    # Environment is a distant glow source on the lower hemisphere — not an
    # enclosing sphere, which would shadow the light sources.
    assert "void glow env_g" in text
    assert "env_g source env\n0\n0\n4 0 0 -1 180" in text


def test_render_preview_without_radiance(monkeypatch, tmp_path):
    monkeypatch.setattr(preview, "radiance_available", lambda: False)
    assert preview.render_preview(tmp_path, tmp_path / "x.rad", tmp_path / "p.png") is False
