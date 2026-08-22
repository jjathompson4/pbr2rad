from pathlib import Path

from pbr2rad.discover import discover, discover_many


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def test_polyhaven_naming(tmp_path: Path) -> None:
    root = tmp_path / "wood_floor_03"
    for name in (
        "wood_floor_03_diff_2k.png",
        "wood_floor_03_rough_2k.png",
        "wood_floor_03_nor_gl_2k.png",
        "wood_floor_03_disp_2k.png",
    ):
        _touch(root / name)

    pbr = discover(root)
    assert pbr.name == "wood_floor_03"
    assert pbr.albedo.name.endswith("diff_2k.png")
    assert pbr.roughness.name.endswith("rough_2k.png")
    assert pbr.normal.name.endswith("nor_gl_2k.png")
    assert pbr.maps["displacement"].name.endswith("disp_2k.png")


def test_ambientcg_naming(tmp_path: Path) -> None:
    root = tmp_path / "Bricks075A"
    for name in (
        "Bricks075A_2K_Color.jpg",
        "Bricks075A_2K_Roughness.jpg",
        "Bricks075A_2K_NormalGL.jpg",
        "Bricks075A_2K_Displacement.jpg",
        "Bricks075A_2K_Metalness.jpg",
    ):
        _touch(root / name)

    pbr = discover(root)
    assert pbr.albedo is not None and "Color" in pbr.albedo.name
    assert pbr.roughness is not None
    assert pbr.metalness is not None
    assert pbr.normal is not None


def test_ambientcg_new_naming(tmp_path: Path) -> None:
    """Current ambientCG packs embed the format in the name: Id_1K-JPG_Map.jpg."""
    root = tmp_path / "Bricks104"
    for name in (
        "Bricks104_1K-JPG_Color.jpg",
        "Bricks104_1K-JPG_NormalGL.jpg",
        "Bricks104_1K-JPG_NormalDX.jpg",
        "Bricks104_1K-JPG_Roughness.jpg",
        "Bricks104_1K-JPG_Displacement.jpg",
        "Bricks104_1K-JPG_AmbientOcclusion.jpg",
        "Bricks104_1K-JPG_Metalness.jpg",
        "Bricks104_1K-JPG_Opacity.jpg",
        "Bricks104_1K-JPG_Emission.jpg",
        "Bricks104.mtlx",
    ):
        _touch(root / name)

    pbr = discover(root)
    assert pbr.name == "Bricks104"
    assert pbr.albedo.name == "Bricks104_1K-JPG_Color.jpg"
    assert pbr.maps["normal_gl"].name == "Bricks104_1K-JPG_NormalGL.jpg"
    assert pbr.maps["normal_dx"].name == "Bricks104_1K-JPG_NormalDX.jpg"
    assert pbr.normal.name == "Bricks104_1K-JPG_NormalGL.jpg"  # GL preferred
    assert pbr.roughness.name == "Bricks104_1K-JPG_Roughness.jpg"
    assert pbr.metalness.name == "Bricks104_1K-JPG_Metalness.jpg"
    assert pbr.maps["displacement"].name == "Bricks104_1K-JPG_Displacement.jpg"
    assert pbr.maps["ao"].name == "Bricks104_1K-JPG_AmbientOcclusion.jpg"
    # Unknown channels and non-image extras are kept out of the map set.
    extras = sorted(p.name for p in pbr.extras)
    assert extras == ["Bricks104_1K-JPG_Emission.jpg", "Bricks104_1K-JPG_Opacity.jpg"]


def test_highest_resolution_wins(tmp_path: Path) -> None:
    root = tmp_path / "stone"
    _touch(root / "stone_diff_1k.png")
    _touch(root / "stone_diff_4k.png")
    pbr = discover(root)
    assert pbr.albedo.name == "stone_diff_4k.png"


def test_discover_many_flat_folder(tmp_path: Path) -> None:
    _touch(tmp_path / "single_diff_2k.png")
    sets = discover_many(tmp_path)
    assert len(sets) == 1
    assert sets[0].albedo is not None


def test_discover_many_parent_folder(tmp_path: Path) -> None:
    _touch(tmp_path / "a" / "a_diff_2k.png")
    _touch(tmp_path / "b" / "b_diff_2k.png")
    sets = discover_many(tmp_path)
    names = sorted(s.name for s in sets)
    assert names == ["a", "b"]


class TestClassifierAnchoring:
    """Trailing-segment anchoring: channel words in the base name must not
    outrank the true specifier at the end of the filename."""

    def test_metal_in_basename_still_albedo(self, tmp_path):
        from pbr2rad.discover import discover

        (tmp_path / "plate_metal_diff_2k.png").write_bytes(b"x")
        (tmp_path / "plate_metal_rough_2k.png").write_bytes(b"x")
        pbr = discover(tmp_path)
        assert pbr.albedo is not None and pbr.albedo.name == "plate_metal_diff_2k.png"
        assert pbr.roughness is not None
        assert "metalness" not in pbr.maps

    def test_joined_pair_beats_trailing(self, tmp_path):
        from pbr2rad.discover import discover

        (tmp_path / "brick_nor_gl_2k.png").write_bytes(b"x")
        pbr = discover(tmp_path)
        assert "normal_gl" in pbr.maps

    def test_untagged_master_beats_tagged_variant(self, tmp_path):
        from pbr2rad.discover import discover

        (tmp_path / "wood_diff_1k.png").write_bytes(b"x")
        (tmp_path / "wood_diff.png").write_bytes(b"x")
        pbr = discover(tmp_path)
        assert pbr.albedo.name == "wood_diff.png"
