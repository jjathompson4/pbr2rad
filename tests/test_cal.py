from pbr2rad import cal


def test_uv_passthrough_uses_lu_lv() -> None:
    text = cal.uv_passthrough()
    assert "u = Lu;" in text
    assert "v = Lv;" in text


def test_every_mode_emits_picture_lookup() -> None:
    """Square textures: pic_u/pic_v are identity multiples of u/v."""
    for mode in ("uv", "planar", "box", "cylindrical", "spherical"):
        text = cal.generate(mode)
        assert "pic_u = u * 1;" in text, mode
        assert "pic_v = v * 1;" in text, mode


def test_picture_lookup_scales_by_aspect() -> None:
    """Radiance spans a picture's long side over 0..aspect, so the lookup
    for a 2:1 texture must stretch u by 2 (and v by 2 for 1:2)."""
    text = cal.generate("planar", axis="xy", pic_u_scale=2.0, pic_v_scale=1.0)
    assert "pic_u = u * 2;" in text
    assert "pic_v = v * 1;" in text
    text = cal.box(pic_u_scale=1.0, pic_v_scale=2.0)
    assert "pic_u = u * 1;" in text
    assert "pic_v = v * 2;" in text


def test_picture_scales_from_dimensions() -> None:
    assert cal.picture_scales(1024, 512) == (2.0, 1.0)
    assert cal.picture_scales(512, 1024) == (1.0, 2.0)
    assert cal.picture_scales(2048, 2048) == (1.0, 1.0)
    assert cal.picture_scales(0, 10) == (1.0, 1.0)


def test_planar_axis_xy_uses_px_py() -> None:
    text = cal.planar("xy", u_scale=2.0, v_scale=3.0)
    assert "Px * u_scale" in text
    assert "Py * v_scale" in text
    assert "u_scale : 2" in text
    assert "v_scale : 3" in text


def test_planar_axis_xz() -> None:
    text = cal.planar("xz")
    assert "Px * u_scale" in text
    assert "Pz * v_scale" in text


def test_planar_axis_yz() -> None:
    text = cal.planar("yz")
    assert "Py * u_scale" in text
    assert "Pz * v_scale" in text


def test_box_references_normal_components() -> None:
    text = cal.box(u_scale=0.5, v_scale=0.5)
    assert "abs(Nx)" in text
    assert "abs(Ny)" in text
    assert "abs(Nz)" in text
    assert "mod(" in text


def test_cylindrical_uses_atan2() -> None:
    text = cal.cylindrical(u_scale=2.0, v_scale=3.0)
    assert "atan2(Py, Px)" in text
    assert "Pz * v_scale" in text
    assert "u_scale : 2" in text
    assert "mod(" in text


def test_spherical_uses_asin() -> None:
    text = cal.spherical(u_scale=1.0, v_scale=1.0)
    assert "atan2(Py, Px)" in text
    assert "asin(" in text
    assert "mod(" in text


def test_generate_dispatch_cylindrical() -> None:
    text = cal.generate("cylindrical", u_scale=1.0, v_scale=1.0)
    assert "atan2" in text


def test_generate_dispatch_spherical() -> None:
    text = cal.generate("spherical", u_scale=1.0, v_scale=1.0)
    assert "asin" in text


def test_generate_dispatch_invalid() -> None:
    import pytest

    with pytest.raises(ValueError):
        cal.generate("bogus")


def test_spherical_reference_wrap_text() -> None:
    """The web preview's reference wrap: 3 repeats around, 1.5 pole-to-pole,
    non-square pictures still aspect-scaled."""
    text = cal.generate("spherical", u_scale=3.0, v_scale=1.5, pic_u_scale=2.0, pic_v_scale=1.0)
    assert "u_scale : 3" in text and "v_scale : 1.5" in text
    assert "u = mod(atan2(Py, Px) / (2*PI) * u_scale" in text        # u around
    assert "v = mod(asin(Pz / r) / PI * v_scale + 0.5" in text       # v pole-to-pole
    assert "pic_u = u * 2;" in text and "pic_v = v * 1;" in text


def test_spherical_swap_uv_transposes_the_wrap() -> None:
    """Poly Haven's spheres: the picture's x axis runs pole-to-pole."""
    text = cal.generate("spherical", u_scale=1.5, v_scale=3.0, swap_uv=True)
    assert "u = mod(asin(Pz / r) / PI * u_scale + 0.5" in text       # u pole-to-pole
    assert "v = mod(atan2(Py, Px) / (2*PI) * v_scale" in text        # v around
    assert "transposed" in text
    # other modes ignore the flag
    assert cal.generate("box", swap_uv=True) == cal.generate("box")
