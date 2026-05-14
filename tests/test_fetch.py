"""Tests for the Poly Haven API fetcher."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from unittest import mock

import pytest

from pbr2rad.fetch import (
    FetchError,
    _pick_files,
    download_texture_set,
    fetch_asset_files,
    fetch_asset_info,
)


# ---------------------------------------------------------------------------
# Canned API responses
# ---------------------------------------------------------------------------

_INFO_TEXTURE = {
    "name": "Wood Floor 03",
    "type": 1,
    "categories": ["wood", "floor"],
}

_INFO_HDRI = {
    "name": "Studio Light",
    "type": 0,
}

# Simplified /files response matching Poly Haven's structure
_FILES = {
    "Diffuse": {
        "1k": {
            "png": {
                "url": "https://dl.polyhaven.org/file/ph-assets/Textures/png/1k/wood_floor_03/wood_floor_03_diff_1k.png",
                "md5": "abc123",
                "size": 102400,
            },
        },
        "2k": {
            "png": {
                "url": "https://dl.polyhaven.org/file/ph-assets/Textures/png/2k/wood_floor_03/wood_floor_03_diff_2k.png",
                "md5": "def456",
                "size": 409600,
            },
        },
    },
    "Rough": {
        "2k": {
            "png": {
                "url": "https://dl.polyhaven.org/file/ph-assets/Textures/png/2k/wood_floor_03/wood_floor_03_rough_2k.png",
                "md5": "ghi789",
                "size": 204800,
            },
        },
    },
    "nor_gl": {
        "2k": {
            "png": {
                "url": "https://dl.polyhaven.org/file/ph-assets/Textures/png/2k/wood_floor_03/wood_floor_03_nor_gl_2k.png",
                "md5": "jkl012",
                "size": 307200,
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_urlopen(canned: dict):
    """Return a context manager mock that yields canned JSON."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(canned).encode()
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    return mock.patch("pbr2rad.fetch.urllib.request.urlopen", return_value=resp)


def _mock_urlopen_404():
    """Return a mock that raises a 404 HTTPError."""
    import urllib.error
    exc = urllib.error.HTTPError(
        url="https://api.polyhaven.com/info/nonexistent",
        code=404,
        msg="Not Found",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )
    return mock.patch("pbr2rad.fetch.urllib.request.urlopen", side_effect=exc)


# ---------------------------------------------------------------------------
# Tests: API calls
# ---------------------------------------------------------------------------

class TestFetchAssetInfo:
    def test_returns_info_dict(self):
        with _mock_urlopen(_INFO_TEXTURE):
            info = fetch_asset_info("wood_floor_03")
        assert info["name"] == "Wood Floor 03"
        assert info["type"] == 1

    def test_404_raises_fetch_error(self):
        with _mock_urlopen_404():
            with pytest.raises(FetchError, match="asset not found"):
                fetch_asset_info("nonexistent")


class TestFetchAssetFiles:
    def test_returns_file_dict(self):
        with _mock_urlopen(_FILES):
            files = fetch_asset_files("wood_floor_03")
        assert "Diffuse" in files
        assert "2k" in files["Diffuse"]


# ---------------------------------------------------------------------------
# Tests: file picker
# ---------------------------------------------------------------------------

class TestPickFiles:
    def test_picks_2k_png(self):
        picked = _pick_files(_FILES, resolution="2k", fmt="png")
        channels = {ch for ch, *_ in picked}
        assert "Diffuse" in channels
        assert "Rough" in channels
        assert "nor_gl" in channels
        assert len(picked) == 3

    def test_picks_1k(self):
        picked = _pick_files(_FILES, resolution="1k", fmt="png")
        # Only Diffuse has a 1k variant
        assert len(picked) == 1
        assert picked[0][0] == "Diffuse"

    def test_no_match_returns_empty(self):
        picked = _pick_files(_FILES, resolution="8k", fmt="png")
        assert picked == []

    def test_format_fallback(self):
        # If requested format doesn't exist but png does, falls back
        picked = _pick_files(_FILES, resolution="2k", fmt="jpg")
        # Should fall back to png
        assert len(picked) == 3


# ---------------------------------------------------------------------------
# Tests: download_texture_set (integration with mocked HTTP)
# ---------------------------------------------------------------------------

class TestDownloadTextureSet:
    def test_downloads_files(self, tmp_path):
        """Full download flow with mocked HTTP responses."""
        # We need to mock multiple urlopen calls:
        # 1. /info/ -> texture info
        # 2. /files/ -> file listing
        # 3+ -> actual file downloads (one per channel)

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG bytes
        diff_md5 = hashlib.md5(fake_png).hexdigest()

        call_count = 0
        responses = [
            json.dumps(_INFO_TEXTURE).encode(),  # /info/
            json.dumps({
                "Diffuse": {
                    "2k": {
                        "png": {
                            "url": "https://dl.polyhaven.org/diff_2k.png",
                            "md5": diff_md5,
                            "size": len(fake_png),
                        },
                    },
                },
            }).encode(),  # /files/
            fake_png,  # download
        ]

        def mock_urlopen(req, **kw):
            nonlocal call_count
            resp = mock.MagicMock()
            resp.read.return_value = responses[call_count]
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            call_count += 1
            return resp

        with mock.patch("pbr2rad.fetch.urllib.request.urlopen", side_effect=mock_urlopen):
            mat_dir = download_texture_set(
                "wood_floor_03", tmp_path, resolution="2k", fmt="png",
            )

        assert mat_dir == tmp_path / "wood_floor_03"
        assert mat_dir.is_dir()
        downloaded = list(mat_dir.iterdir())
        assert len(downloaded) == 1
        assert downloaded[0].name == "diff_2k.png"

    def test_checksum_mismatch_raises(self, tmp_path):
        """Bad MD5 should raise FetchError."""
        fake_png = b"not a real png"

        call_count = 0
        responses = [
            json.dumps(_INFO_TEXTURE).encode(),
            json.dumps({
                "Diffuse": {
                    "2k": {
                        "png": {
                            "url": "https://dl.polyhaven.org/diff_2k.png",
                            "md5": "wrong_checksum",
                            "size": len(fake_png),
                        },
                    },
                },
            }).encode(),
            fake_png,
        ]

        def mock_urlopen(req, **kw):
            nonlocal call_count
            resp = mock.MagicMock()
            resp.read.return_value = responses[call_count]
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            call_count += 1
            return resp

        with mock.patch("pbr2rad.fetch.urllib.request.urlopen", side_effect=mock_urlopen):
            with pytest.raises(FetchError, match="checksum mismatch"):
                download_texture_set(
                    "wood_floor_03", tmp_path, resolution="2k", fmt="png",
                )


# ---------------------------------------------------------------------------
# Tests: CLI integration
# ---------------------------------------------------------------------------

class TestFetchCLI:
    def test_fetch_subcommand_parses(self):
        """Verify the fetch subcommand is wired into the argument parser."""
        from pbr2rad.cli import _build_fetch_parser

        parser = _build_fetch_parser()
        args = parser.parse_args(["wood_floor_03", "-o", "/tmp/out"])
        assert args.slug == "wood_floor_03"
        assert args.resolution == "1k"
        assert args.fmt == "png"
        assert args.output == Path("/tmp/out")

    def test_fetch_resolution_option(self):
        """Verify fetch resolution option."""
        from pbr2rad.cli import _build_fetch_parser

        parser = _build_fetch_parser()
        args = parser.parse_args(["rock_ground", "-o", "/tmp/out", "--resolution", "4k"])
        assert args.resolution == "4k"

    def test_convert_subcommand_parses(self):
        """Verify convert subcommand still works via main()."""
        from pbr2rad.cli import _build_convert_parser

        parser = _build_convert_parser()
        args = parser.parse_args(["/some/input", "-o", "/tmp/out"])
        assert args.input == Path("/some/input")

    def test_main_dispatches_fetch(self):
        """Verify main() routes 'fetch' to the fetch handler."""
        from pbr2rad.cli import main

        # Should fail gracefully (network error), but proves routing works
        rc = main(["fetch", "nonexistent_slug_xyz", "-o", "/tmp/out"])
        assert rc == 1  # FetchError → exit 1
