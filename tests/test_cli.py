from pathlib import Path

from PIL import Image

from pbr2rad.cli import main


def _png(p: Path, color: tuple[int, int, int]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color).save(p)


def test_cli_converts_library(tmp_path: Path, capsys) -> None:
    inp = tmp_path / "in"
    _png(inp / "wood" / "wood_diff_2k.png", (180, 140, 90))
    _png(inp / "wood" / "wood_rough_2k.png", (64, 64, 64))
    _png(inp / "brick" / "brick_diff_2k.png", (200, 80, 60))

    out = tmp_path / "out"
    rc = main([
        str(inp),
        "-o", str(out),
        "--projection", "uv",
        "-v",
    ])
    assert rc == 0

    assert (out / "wood" / "wood.rad").exists()
    assert (out / "wood" / "wood.cal").exists()
    assert (out / "wood" / "wood.hdr").exists()
    assert (out / "brick" / "brick.rad").exists()
    assert (out / "manifest.json").exists()


def test_cli_missing_input(tmp_path: Path) -> None:
    rc = main([str(tmp_path / "nope"), "-o", str(tmp_path / "out")])
    assert rc == 2


def test_cli_empty_input(tmp_path: Path) -> None:
    inp = tmp_path / "in"
    inp.mkdir()
    rc = main([str(inp), "-o", str(tmp_path / "out")])
    # discover_many returns [] for empty dir → exit 1
    assert rc == 1
