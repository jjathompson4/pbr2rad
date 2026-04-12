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
