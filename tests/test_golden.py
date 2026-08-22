"""Golden-file regression tests for the conversion pipeline.

These pin the exact bytes of every file convert_set produces for small
deterministic fixture sets. They exist so the performance rewrites of
hdr.py / normal.py / estimate.py can be verified as behavior-preserving:
any byte-level drift in .hdr encoding, .dat formatting, estimated maps,
or manifest content fails here.

Regenerate hashes (after an INTENTIONAL behavior change only):

    PYTHONPATH=src python tests/test_golden.py --regen
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pbr2rad.convert import ConvertOptions, convert_set, write_manifest  # noqa: E402
from pbr2rad.discover import discover  # noqa: E402

GOLDEN_PATH = Path(__file__).parent / "golden_hashes.json"
SIZE = 48  # small enough to run the pure-Python paths fast


# ---------------------------------------------------------------------------
# Deterministic fixture images (no RNG — pure functions of pixel coords)
# ---------------------------------------------------------------------------

def _make_albedo(path: Path) -> None:
    img = Image.new("RGB", (SIZE, SIZE))
    px = img.load()
    for y in range(SIZE):
        for x in range(SIZE):
            # Diagonal gradient + checker so estimation has real structure.
            checker = 40 if (x // 6 + y // 6) % 2 else 0
            px[x, y] = (
                (x * 5 + checker) % 256,
                (y * 5 + checker) % 256,
                (x * 2 + y * 2) % 256,
            )
    img.save(path)


def _make_normal(path: Path) -> None:
    img = Image.new("RGB", (SIZE, SIZE))
    px = img.load()
    for y in range(SIZE):
        for x in range(SIZE):
            px[x, y] = ((x * 4) % 256, (y * 4) % 256, 255)
    img.save(path)


def _make_rough(path: Path, bits: int = 8) -> None:
    if bits == 16:
        img = Image.new("I;16", (SIZE, SIZE))
        px = img.load()
        for y in range(SIZE):
            for x in range(SIZE):
                px[x, y] = (x * 1365 + y * 256) % 65536
    else:
        img = Image.new("L", (SIZE, SIZE))
        px = img.load()
        for y in range(SIZE):
            for x in range(SIZE):
                px[x, y] = (x * 5 + y * 3) % 256
    img.save(path)


def _build_case(root: Path, case: str) -> Path:
    """Create one fixture set directory; returns the set dir."""
    d = root / case
    d.mkdir(parents=True)
    if case == "full_set":
        _make_albedo(d / "fixture_diff.png")
        _make_normal(d / "fixture_nor_gl.png")
        _make_rough(d / "fixture_rough.png")
    elif case == "albedo_only_estimated":
        _make_albedo(d / "fixture_diff.png")
    elif case == "rough16_set":
        _make_albedo(d / "fixture_diff.png")
        _make_rough(d / "fixture_rough.png", bits=16)
    elif case == "oriented":
        _make_albedo(d / "fixture_diff.png")
        _make_normal(d / "fixture_nor_gl.png")
    else:
        raise ValueError(case)
    return d


_CASE_OPTS = {
    "full_set": ConvertOptions(projection="box", u_scale=0.5, v_scale=2.0),
    "albedo_only_estimated": ConvertOptions(projection="uv", bump_scale=1.5),
    "rough16_set": ConvertOptions(projection="planar", planar_axis="xz"),
    "oriented": ConvertOptions(
        projection="box", rotate_per_map={"albedo": 90}, flip_h=True,
    ),
}


def _run_case(case: str, work: Path) -> dict[str, str]:
    """Convert one fixture case and return {relative_path: sha256}."""
    src = _build_case(work / "src", case)
    out = work / "out" / case
    out.mkdir(parents=True)
    pbr = discover(src)
    result = convert_set(pbr, out, _CASE_OPTS[case])
    write_manifest([result], out)

    hashes: dict[str, str] = {}
    for f in sorted(out.rglob("*")):
        if not f.is_file():
            continue
        # .pvw embeds a Pillow-encoded PNG, whose bytes shift with the Pillow
        # version. Pinning it here would fail on every dependency bump for no
        # signal; test_pvw.py checks its structure instead.
        if f.suffix == ".pvw":
            continue
        hashes[f.relative_to(out).as_posix()] = hashlib.sha256(
            f.read_bytes()
        ).hexdigest()
    return hashes


def _all_cases(work: Path) -> dict[str, dict[str, str]]:
    return {case: _run_case(case, work) for case in _CASE_OPTS}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_golden_outputs(tmp_path):
    expected = json.loads(GOLDEN_PATH.read_text())
    actual = _all_cases(tmp_path)
    for case, files in expected.items():
        got = actual.get(case, {})
        assert sorted(got) == sorted(files), (
            f"{case}: file list changed.\n"
            f"missing: {sorted(set(files) - set(got))}\n"
            f"extra:   {sorted(set(got) - set(files))}"
        )
        mismatched = [p for p in files if got[p] != files[p]]
        assert not mismatched, f"{case}: content drift in {mismatched}"


# ---------------------------------------------------------------------------
# Regeneration entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--regen" not in sys.argv:
        sys.exit("Usage: python tests/test_golden.py --regen")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        data = _all_cases(Path(td))
    GOLDEN_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    n = sum(len(v) for v in data.values())
    print(f"Wrote {GOLDEN_PATH} ({len(data)} cases, {n} files)")
