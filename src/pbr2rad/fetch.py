"""Poly Haven API client for downloading PBR texture sets.

Downloads PBR materials from ``api.polyhaven.com`` into a local folder
suitable for conversion with ``pbr2rad``.

Uses only stdlib (``urllib.request``) — no new dependencies.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

_BASE_URL = "https://api.polyhaven.com"
_USER_AGENT = "pbr2rad/0.1"


class FetchError(Exception):
    """Raised when a Poly Haven API or download operation fails."""


def _api_get(path: str) -> dict:
    """GET a JSON endpoint from the Poly Haven API."""
    url = f"{_BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FetchError(f"asset not found: {path}") from exc
        raise FetchError(f"API error {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error: {exc.reason}") from exc


def fetch_asset_info(slug: str) -> dict:
    """Fetch metadata for a single Poly Haven asset."""
    return _api_get(f"/info/{slug}")


def fetch_asset_files(slug: str) -> dict:
    """Fetch the file map for a Poly Haven asset.

    Returns a nested dict: ``{channel: {resolution: {format: {url, md5, size}}}}``.
    """
    return _api_get(f"/files/{slug}")


def _pick_files(
    files: dict,
    resolution: str = "2k",
    fmt: str = "png",
) -> list[tuple[str, str, str | None, int]]:
    """Select download URLs for each available channel at the given resolution.

    Returns a list of ``(channel_key, url, md5, size)`` tuples.
    """
    picked: list[tuple[str, str, str | None, int]] = []

    for channel_key, channel_data in files.items():
        if not isinstance(channel_data, dict):
            continue

        # Look for the requested resolution
        res_data = channel_data.get(resolution)
        if res_data is None:
            continue

        # Look for the requested format
        fmt_data = res_data.get(fmt)
        if fmt_data is None:
            # Fall back to any available format
            for fallback_fmt in ("png", "jpg", "exr"):
                fmt_data = res_data.get(fallback_fmt)
                if fmt_data is not None:
                    break
        if fmt_data is None:
            continue

        url = fmt_data.get("url")
        if url is None:
            continue

        md5 = fmt_data.get("md5")
        size = fmt_data.get("size", 0)
        picked.append((channel_key, url, md5, size))

    return picked


def _download_file(
    url: str,
    dest: Path,
    expected_md5: str | None = None,
    *,
    verbose: bool = False,
) -> None:
    """Download a single file, optionally verifying its MD5 checksum."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()

    if expected_md5:
        actual = hashlib.md5(data).hexdigest()
        if actual != expected_md5:
            raise FetchError(
                f"checksum mismatch for {dest.name}: "
                f"expected {expected_md5}, got {actual}"
            )

    dest.write_bytes(data)
    if verbose:
        size_mb = len(data) / (1024 * 1024)
        print(f"  downloaded: {dest.name} ({size_mb:.1f} MB)")


def download_texture_set(
    slug: str,
    output_dir: Path,
    *,
    resolution: str = "2k",
    fmt: str = "png",
    verbose: bool = False,
) -> Path:
    """Download a PBR texture set from Poly Haven.

    Creates ``<output_dir>/<slug>/`` with the downloaded texture files.
    Returns the path to the created material folder.
    """
    # Validate the asset exists and is a texture
    info = fetch_asset_info(slug)
    asset_type = info.get("type")
    if asset_type not in (1, "textures", "texture", None):
        raise FetchError(
            f"asset {slug!r} is type {asset_type!r}, not a texture"
        )

    # Get available files
    files = fetch_asset_files(slug)

    # Pick files for each channel at the requested resolution
    to_download = _pick_files(files, resolution=resolution, fmt=fmt)
    if not to_download:
        raise FetchError(
            f"no files found for {slug!r} at resolution={resolution!r}, "
            f"format={fmt!r}"
        )

    # Create output folder
    mat_dir = Path(output_dir) / slug
    mat_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"downloading {slug} ({len(to_download)} maps, {resolution}):")

    for channel_key, url, md5, _size in to_download:
        # Derive a filename from the URL
        url_filename = url.rsplit("/", 1)[-1]
        dest = mat_dir / url_filename
        _download_file(url, dest, md5, verbose=verbose)

    if verbose:
        print(f"  saved to: {mat_dir}")

    return mat_dir
