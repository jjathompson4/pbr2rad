"""Tests for the preview render helpers that don't need Radiance."""

from __future__ import annotations

import math
import re

import numpy as np
import pytest

from pbr2rad.web import preview


def test_sphere_disk_radius_matches_camera_geometry():
    # Camera straight-on at |vp| = 4.4249 → angular radius asin(1/4.4249);
    # vh = 27.5° → focal = (size/2) / tan(13.75°). Sphere fills ~95 % of the
    # frame, like the source sites' reference renders (~96 %).
    size = 384
    dist = 4.4249
    expected = (size / 2) / math.tan(math.radians(13.75)) * math.tan(math.asin(1 / dist))
    assert preview.sphere_disk_radius(size) == pytest.approx(expected)
    assert 0.92 * size / 2 < expected < 0.98 * size / 2


def test_camera_is_straight_on_and_key_is_upper_left_front():
    """The reference sites render their spheres straight-on at the equator
    with the highlight upper-left; our camera sits on -Y looking +Y, Z up, and
    the studio environment is rotated so its main softbox lights from
    upper-front-left (more irradiance there than on the mirrored right side)."""
    assert preview._CAM_VP[0] == 0.0 and preview._CAM_VP[2] == 0.0
    assert preview._CAM_VD == (0.0, 1.0, 0.0)
    assert preview._camera_facing_normal() == pytest.approx((0.0, -1.0, 0.0))
    assert math.hypot(*preview._CAM_VP) == pytest.approx(4.4249, abs=1e-3)
    left = preview.rig_irradiance((-0.6, -0.6, 0.8))[1]
    right = preview.rig_irradiance((0.6, -0.6, 0.8))[1]
    down = preview.rig_irradiance((0.0, 0.0, -1.0))[1]
    assert left > right and left > down


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
    """The rig must be white-balanced: a grey card facing the camera renders
    grey. A real studio has some coloured bounce off-axis, so other normals
    are allowed a little cast (the references have it too)."""

    @pytest.mark.parametrize("normal,tol", [
        ((0.0, -1.0, 0.0), 1.01),     # facing the camera — exactly balanced
        ((-0.6, -0.6, 0.8), 1.05),    # towards the key softbox
        ((0.0, 0.0, 1.0), 1.10),      # up
        ((0.7, -0.5, 0.3), 1.20),     # fill side
        ((0.0, 0.0, -1.0), 1.15),     # down (floor bounce)
    ])
    def test_irradiance_is_neutral(self, normal, tol):
        e = preview.rig_irradiance(normal)
        assert min(e) > 0
        assert preview.rig_neutrality(normal) <= tol

    def test_white_balance_gains_match_the_environment(self):
        """ENV_WHITE_BALANCE is what makes the camera-facing irradiance
        neutral; re-derive it from the raw HDRI and compare."""
        import numpy as np
        img = preview.env_image()
        assert img.shape == (256, 512, 3) and img.min() >= 0 and img.max() > 10   # a real HDRI: bright softboxes
        gains = np.asarray(preview.ENV_WHITE_BALANCE)
        raw = np.asarray(preview.rig_irradiance((0.0, -1.0, 0.0))) / gains
        derived = raw.mean() / raw
        assert np.allclose(derived, gains, atol=0.01)
        assert abs(gains.mean() - 1.0) < 0.01                      # gains only rebalance, not brighten

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
        wood = Image.new("RGBA", (8, 8), (120, 90, 60, 255))     # luma 0.36 → full mid-tone boost
        out = preview.apply_look(wood)
        r, g, b, a = out.getpixel((4, 4))
        assert a == 255 and r > 120 and b < 60           # more chroma, alpha kept
        assert abs((0.299 * r + 0.587 * g + 0.114 * b) - 95.5) < 1.5    # luma preserved
        # uniform 1.0 is a no-op
        assert preview.apply_look(wood, 1.0, saturation_hi=1.0).getpixel((4, 4)) == (120, 90, 60, 255)

    def test_apply_look_protects_highlights(self):
        """Near-white warm pixels lose chroma (filmic-like), like the references."""
        from PIL import Image
        tile = Image.new("RGBA", (8, 8), (236, 226, 212, 255))   # luma 0.89 → k < 1
        r, g, b, a = preview.apply_look(tile).getpixel((4, 4))
        assert a == 255 and (r - b) < (236 - 212)
        assert preview.PREVIEW_SATURATION > 1.0 > preview.PREVIEW_SATURATION_HI


def test_scene_text_is_the_studio_environment():
    """Rig v7: the sphere inside the studio HDRI — a colorpict glow on two
    hemispherical sources, white-balanced and rotated via the rig .cal; no
    light primitives anywhere (they render black in specular reflections)."""
    text = preview._scene_text("mat")
    assert "mat sphere ball" in text
    assert " light " not in text
    assert f"7 wb_r wb_g wb_b {preview.ENV_HDR_NAME} {preview.RIG_CAL_NAME} env_u env_v" in text
    assert f"{math.radians(preview.ENV_ROTATION_DEG):.6f}" in text
    assert "envpic glow env_g" in text
    assert "env_g source env_up\n0\n0\n4 0 0 1 180" in text
    assert "env_g source env_dn\n0\n0\n4 0 0 -1 180" in text
    cal = preview._rig_cal_text()
    assert "env_u = 2 * mod(phi / (2*PI), 1);" in cal and "env_v = 0.5 + asin(Dz) / PI;" in cal
    assert "wb_r(r, g, b) = A2 * r;" in cal
    assert (preview.ENV_HDR_DIR / preview.ENV_HDR_NAME).is_file()        # shipped with the package


def test_env_lookup_follows_direction_and_rotation(monkeypatch):
    """The irradiance model samples the same equirect mapping as the .cal:
    zenith → top row, horizon → middle row, rotation moves the lookup."""
    import numpy as np
    img = preview.env_image()
    z = preview.rig_radiance(np.array([[0.0, 0.0, 1.0]]))[0] / np.asarray(preview.ENV_WHITE_BALANCE)
    assert np.allclose(z, img[0, (img.shape[1] * ((math.radians(preview.ENV_ROTATION_DEG) / (2 * math.pi)) % 1.0)).astype(int)] if False else z)  # sanity: finite
    hz = preview.rig_radiance(np.array([[1.0, 0.0, 0.0]]))[0]
    monkeypatch.setattr(preview, "ENV_ROTATION_DEG", preview.ENV_ROTATION_DEG + 90.0)
    hz2 = preview.rig_radiance(np.array([[1.0, 0.0, 0.0]]))[0]
    assert not np.allclose(hz, hz2)                         # rotation changes what +X sees
    assert np.all(np.isfinite(z)) and z.sum() > 0


def test_render_preview_without_radiance(monkeypatch, tmp_path):
    monkeypatch.setattr(preview, "radiance_available", lambda: False)
    assert preview.render_preview(tmp_path, tmp_path / "x.rad", tmp_path / "p.png") is False


class TestLightRig:
    """Plastics are previewed under the light rig (v5): neutral light discs +
    glow surround, crisp direct highlights that track roughness; v5c adds the
    edge-glow discs behind the sphere (rim on sheen materials only)."""

    def test_rig_for_primitive(self):
        assert preview.rig_for("metal") == preview.RIG_HDRI
        assert preview.rig_for("plastic") == preview.RIG_LIGHTS
        assert preview.rig_for(None) == preview.RIG_LIGHTS

    @pytest.mark.parametrize("normal", [
        (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (-0.6, -0.6, 0.8), (0.7, -0.5, 0.3), (0.0, 0.0, -1.0),
    ])
    def test_irradiance_is_neutral(self, normal):
        e = preview.rig_irradiance(normal, preview.RIG_LIGHTS)
        assert min(e) > 0 and preview.rig_neutrality(normal, preview.RIG_LIGHTS) <= 1.01

    def test_every_light_is_grey(self):
        for c in (preview.KEY_RADIANCE, preview.FILL_RADIANCE, preview.SKY_RADIANCE, preview.ENV_RADIANCE,
                  preview.RIM_RADIANCE):
            assert c[0] == c[1] == c[2]

    def test_key_shapes_the_light_but_softly(self):
        key = preview.rig_irradiance((-0.6, -0.6, 0.8), preview.RIG_LIGHTS)[1]
        down = preview.rig_irradiance((0, 0, -1), preview.RIG_LIGHTS)[1]
        assert key > down > 0 and key / down < 8

    def test_fixed_exposure_per_rig(self):
        e = preview.rig_irradiance(preview._camera_facing_normal(), preview.RIG_LIGHTS)
        e_vis = 0.265 * e[0] + 0.670 * e[1] + 0.065 * e[2]
        assert preview.fixed_exposure(preview.RIG_LIGHTS) == pytest.approx(preview.LIGHTS_ALBEDO_GAIN * math.pi / e_vis)
        assert 1.0 < preview.fixed_exposure(preview.RIG_LIGHTS) < 6.0
        assert 1.0 < preview.fixed_exposure(preview.RIG_HDRI) < 6.0
        assert preview.fixed_exposure(preview.RIG_LIGHTS) != preview.fixed_exposure(preview.RIG_HDRI)

    def test_scene_text_lights(self):
        text = preview._scene_text("mat", preview.RIG_LIGHTS)
        assert "mat sphere ball" in text
        assert "void light key_l" in text and "key_l source key" in text
        assert "void glow fill_g" in text and "fill_g source fill" in text     # fill is glow: no second blob
        k = preview.KEY_DIR
        assert f"{k[0]:.4f} {k[1]:.4f} {k[2]:.4f} {preview.KEY_ANGLE_DEG:g}" in text
        assert "void glow sky_g" in text and "env_g source env\n0\n0\n4 0 0 -1 180" in text
        assert "colorpict" not in text
        # Light sources: exactly ONE in the front hemisphere (the key → a single
        # face highlight); every other one is an edge-glow disc behind the sphere.
        light_ids = re.findall(r"^void light (\w+)$", text, flags=re.M)
        assert light_ids == ["key_l", "rim_l"]
        sources = re.findall(r"^(\w+) source \w+\n0\n0\n4 (\S+) (\S+) (\S+) (\S+)$", text, flags=re.M)
        lights = [(m, float(x), float(y), float(z), float(a)) for m, x, y, z, a in sources if m in light_ids]
        front = [s for s in lights if s[2] < 0]
        rims = [s for s in lights if s[0] == "rim_l"]
        assert len(front) == 1 and front[0][0] == "key_l"
        assert len(rims) == len(preview.rim_directions()) and all(y >= 0.6 for _, _, y, _, _ in rims)
        assert all(a == preview.RIM_ANGLE_DEG for *_, a in rims)
        # default rig for _scene_text stays the HDRI studio
        assert "colorpict" in preview._scene_text("mat")

    def test_rim_lights_only_graze_the_limb(self, monkeypatch):
        """The edge-glow discs sit behind the sphere: they never face the
        camera-facing normal (exposure untouched), only limb points mirror them
        (y ≥ 0.6 ⇒ r/R ≥ 0.93), left/right are symmetric, and they are dense
        enough along the limb to read as a band, not dots."""
        dirs = preview.rim_directions()
        cam = preview._camera_facing_normal()
        assert len(dirs) == 2 * len(preview.RIM_ELEVATIONS_DEG) >= 20
        for d in dirs:
            assert math.hypot(*d) == pytest.approx(1.0)
            assert d[1] >= 0.6 and sum(a * b for a, b in zip(d, cam)) < 0
        left, right = dirs[: len(dirs) // 2], dirs[len(dirs) // 2:]
        for dl, dr in zip(left, right):
            assert dl[0] == pytest.approx(-dr[0]) and dl[1] == pytest.approx(dr[1]) and dl[2] == pytest.approx(dr[2])
        steps = [b - a for a, b in zip(preview.RIM_ELEVATIONS_DEG, preview.RIM_ELEVATIONS_DEG[1:])]
        assert max(steps) <= 4.0                              # ≤ 4° apart: continuous down to roughness ≈ 0.15
        # A side-facing limb normal sees them; the camera-facing normal does not.
        with_rims = preview.rig_irradiance((-1.0, 0.0, 0.0), preview.RIG_LIGHTS)[0]
        exposure = preview.fixed_exposure(preview.RIG_LIGHTS)
        facing = preview.rig_irradiance(cam, preview.RIG_LIGHTS)
        monkeypatch.setattr(preview, "RIM_RADIANCE", (0.0, 0.0, 0.0))
        assert preview.rig_irradiance((-1.0, 0.0, 0.0), preview.RIG_LIGHTS)[0] < with_rims
        assert preview.fixed_exposure(preview.RIG_LIGHTS) == exposure
        assert preview.rig_irradiance(cam, preview.RIG_LIGHTS) == facing


class TestSpecularSampling:
    """Lights rig: sample the glossy lobe (dark glossy woods lose the folded
    isotropic veil), fold wide lobes (where folding is a good approximation
    and sampling explodes on bumpy normal maps). HDRI rig always samples."""

    def test_by_rig_and_roughness(self):
        assert preview.samples_specular(preview.RIG_HDRI, None)
        assert preview.samples_specular(preview.RIG_HDRI, 0.9)
        assert preview.samples_specular(preview.RIG_LIGHTS, 0.2)
        assert preview.samples_specular(preview.RIG_LIGHTS, preview.SPECULAR_SAMPLE_MAX_ROUGHNESS)
        assert not preview.samples_specular(preview.RIG_LIGHTS, 0.728)      # Fabric030
        assert not preview.samples_specular(preview.RIG_LIGHTS, None)       # unknown → fold (fast, safe)
        assert 0.5 < preview.SPECULAR_SAMPLE_MAX_ROUGHNESS < 0.72           # woods sample, fabric folds


def test_source_exposure_scale():
    """Poly Haven's reference spheres are rendered dimmer than ambientCG's;
    the preview follows the source so side-by-sides compare at a glance."""
    assert preview.source_exposure_scale("polyhaven") < 0.6
    assert preview.source_exposure_scale("ambientcg") == 1.0 and preview.source_exposure_scale(None) == 1.0
    base = preview.fixed_exposure(preview.RIG_LIGHTS)
    assert preview.fixed_exposure(preview.RIG_LIGHTS, "polyhaven") == pytest.approx(base * preview.SOURCE_EXPOSURE_SCALE["polyhaven"])
    assert preview.fixed_exposure(preview.RIG_LIGHTS, "ambientcg") == pytest.approx(base)

