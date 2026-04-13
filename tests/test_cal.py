from pbr2rad import cal


def test_uv_passthrough_uses_lu_lv() -> None:
    text = cal.uv_passthrough()
    assert "u = Lu;" in text
    assert "v = Lv;" in text


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
