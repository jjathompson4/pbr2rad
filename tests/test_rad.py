from pbr2rad.rad import MaterialParams, generate


def test_plastic_primitive_for_nonmetal() -> None:
    params = MaterialParams(
        name="wood", hdr_file="wood.hdr", cal_file="wood.cal",
        roughness=0.5, metalness=0.0,
    )
    out = generate(params)
    assert "void colorpict wood_pat" in out
    assert "wood_pat plastic wood" in out
    # default dielectric specularity is 0.05
    assert "5 1 1 1 0.05" in out


def test_metal_primitive_for_metalness_above_half() -> None:
    params = MaterialParams(
        name="steel", hdr_file="steel.hdr", cal_file="steel.cal",
        roughness=0.3, metalness=0.9,
    )
    out = generate(params)
    assert "steel_pat metal steel" in out
    assert "5 1 1 1 1" in out  # specularity=1 for metal


def test_roughness_is_squared() -> None:
    params = MaterialParams(
        name="m", hdr_file="m.hdr", cal_file="m.cal",
        roughness=0.5, metalness=0.0,
    )
    out = generate(params)
    # 0.5² = 0.25
    assert " 0.25" in out


def test_colorpict_references_files() -> None:
    params = MaterialParams(
        name="mat", hdr_file="mat.hdr", cal_file="mat.cal",
        roughness=0.1, metalness=0.0,
    )
    out = generate(params)
    # The picture lookup uses the aspect-scaled pic_u/pic_v from the .cal;
    # the data modifiers keep u v.
    assert "7 red green blue mat.hdr mat.cal pic_u pic_v" in out
