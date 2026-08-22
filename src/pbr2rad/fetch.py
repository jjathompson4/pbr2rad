"""Poly Haven API client for downloading PBR texture sets.

Downloads PBR materials from ``api.polyhaven.com`` into a local folder
suitable for conversion with ``pbr2rad``.

Uses only stdlib (``urllib.request``) — no new dependencies.

This module also hosts the bits shared by every texture source (the
``FetchError`` type, the per-source cache root, the streaming download
helper) — see ``sources.py`` for the registry and ``ambientcg.py`` for the
second source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("pbr2rad.fetch")

_BASE_URL = "https://api.polyhaven.com"
_CATALOG_URL = f"{_BASE_URL}/assets?type=textures"
# Poly Haven's CDN resizes on demand; 200 px for the grid, 512 px for the hero.
_THUMB_URL = "https://cdn.polyhaven.com/asset_img/thumbs/{slug}.png?width={width}"
# Shared by every source client — identify ourselves politely to upstream.
_USER_AGENT = "pbr2rad/0.1 (+https://pbr2rad.com)"

# Provenance sidecar written next to the downloaded maps; convert_set() reads
# it into the manifest. Non-image, so discover()/downscaling ignore it.
SIDECAR_NAME = "pbr2rad_source.json"

_STREAM_CHUNK = 1 << 20  # 1 MiB

# Cache budget (all sources together). $PBR2RAD_CACHE_MAX_MB overrides;
# 0 disables eviction. Catalog files are never evicted (tiny, and losing
# them costs a paged upstream rebuild).
DEFAULT_CACHE_MAX_MB = 1024
_CATALOG_GLOB = "catalog"          # filename prefix exempt from eviction
_PART_GRACE_SECONDS = 600          # in-flight .part files younger than this are exempt
_USAGE_MEMO_SECONDS = 60.0         # walk the (slow, ephemeral) disk at most once a minute


def _cache_base() -> Path:
    """Root of the download cache for every source."""
    override = os.environ.get("PBR2RAD_CACHE_DIR")
    return Path(override) if override else Path.home() / ".cache" / "pbr2rad"


def _cache_root(source: str = "polyhaven") -> Path:
    """Return the on-disk cache directory for one texture source.

    Overridable via $PBR2RAD_CACHE_DIR. Default: ~/.cache/pbr2rad/<source>
    """
    return _cache_base() / source


def cache_budget_bytes() -> int:
    """Configured cache ceiling in bytes (0 = unlimited)."""
    raw = os.environ.get("PBR2RAD_CACHE_MAX_MB")
    try:
        mb = int(raw) if raw not in (None, "") else DEFAULT_CACHE_MAX_MB
    except ValueError:
        mb = DEFAULT_CACHE_MAX_MB
    return max(0, mb) << 20


_USAGE_LOCK = threading.Lock()
_USAGE_MEMO: dict = {"root": None, "at": 0.0, "files": []}


def _walk_cache(root: Path) -> list[tuple[Path, int, float]]:
    files: list[tuple[Path, int, float]] = []
    if not root.is_dir():
        return files
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((p, st.st_size, st.st_mtime))
    return files


def cache_usage(*, refresh: bool = False) -> list[tuple[Path, int, float]]:
    """``[(path, size, mtime)]`` for every cached file, memoised for a minute."""
    root = _cache_base()
    now = time.time()
    with _USAGE_LOCK:
        if (
            not refresh
            and _USAGE_MEMO["root"] == root
            and now - _USAGE_MEMO["at"] < _USAGE_MEMO_SECONDS
        ):
            return list(_USAGE_MEMO["files"])
        files = _walk_cache(root)
        _USAGE_MEMO.update(root=root, at=now, files=files)
        return list(files)


def cache_total_bytes(*, refresh: bool = False) -> int:
    return sum(size for _p, size, _m in cache_usage(refresh=refresh))


def _evictable(path: Path, mtime: float, now: float) -> bool:
    name = path.name
    if name.startswith(_CATALOG_GLOB) and name.endswith(".json"):
        return False
    if name.startswith(".catalog_") and name.endswith(".tmp"):
        return False
    if name.endswith(".part") and now - mtime < _PART_GRACE_SECONDS:
        return False
    return True


def enforce_cache_budget(*, budget: int | None = None) -> int:
    """Delete least-recently-modified cache files until under budget.

    Returns the number of bytes freed. Safe to call often: it walks the
    cache at most once a minute unless it actually evicts. Partial
    Poly Haven sets / missing zips simply re-download on next use (md5 /
    size validation covers integrity); thumbnail indexes regenerate.
    """
    budget = cache_budget_bytes() if budget is None else budget
    if budget <= 0:
        return 0
    files = cache_usage()
    total = sum(size for _p, size, _m in files)
    if total <= budget:
        return 0
    # Re-walk for an accurate picture before deleting anything.
    files = cache_usage(refresh=True)
    total = sum(size for _p, size, _m in files)
    if total <= budget:
        return 0
    now = time.time()
    freed = 0
    touched_dirs: set[Path] = set()
    for path, size, mtime in sorted(files, key=lambda t: t[2]):
        if total - freed <= budget:
            break
        if not _evictable(path, mtime, now):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("could not evict %s", path, exc_info=True)
            continue
        freed += size
        touched_dirs.add(path.parent)
    # Tidy emptied directories (bottom-up), never the per-source roots.
    base = _cache_base()
    for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
        cur = d
        while cur != base and cur.parent != base and cur.is_dir():
            try:
                cur.rmdir()          # only succeeds when empty
            except OSError:
                break
            cur = cur.parent
    if freed:
        log.info("cache budget: evicted %.1f MB (budget %.0f MB)", freed / 2**20, budget / 2**20)
        cache_usage(refresh=True)
    return freed


def _cached_path(slug: str, resolution: str, fmt: str, filename: str) -> Path:
    return _cache_root() / slug / resolution / fmt / filename


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    resolution: str = "1k",
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
    cache: Path | None = None,
) -> None:
    """Download a file, with optional on-disk cache and MD5 verification.

    If ``cache`` is given and already contains a valid copy (matching md5
    when provided), we hardlink/copy it into ``dest`` instead of re-fetching.
    Otherwise we download, validate, save into the cache, and place at dest.
    """
    if cache is not None and cache.exists():
        if expected_md5 is None or _file_md5(cache) == expected_md5:
            _place_from_cache(cache, dest)
            if verbose:
                size_mb = cache.stat().st_size / (1024 * 1024)
                print(f"  cache hit: {dest.name} ({size_mb:.1f} MB)")
            return
        # md5 mismatch — fall through and re-download

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

    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        _place_from_cache(cache, dest)
        enforce_cache_budget()
    else:
        dest.write_bytes(data)

    if verbose:
        size_mb = len(data) / (1024 * 1024)
        print(f"  downloaded: {dest.name} ({size_mb:.1f} MB)")


def _place_from_cache(cache: Path, dest: Path) -> None:
    """Materialize a cached file at dest (hardlink if same filesystem, else copy)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(cache, dest)
    except OSError:
        shutil.copy2(cache, dest)


def _stream_to_file(
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    timeout: int = 180,
    verbose: bool = False,
) -> int:
    """Stream ``url`` to ``dest`` in chunks; return the byte count.

    Writes to ``dest.part`` and ``os.replace``s on success so a partial
    transfer never masquerades as a finished file. Aborts (and removes the
    partial) with ``FetchError`` once more than ``max_bytes`` have arrived —
    the declared size from an API is a hint, never a guarantee.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, part.open("wb") as out:
            while True:
                chunk = resp.read(_STREAM_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise FetchError(
                        f"download of {dest.name} exceeded the "
                        f"{max_bytes // (1024 * 1024)} MB limit — aborted"
                    )
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code == 404:
            raise FetchError(f"file not found: {url}") from exc
        raise FetchError(f"download error {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise FetchError(f"network error: {exc.reason}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    if verbose:
        print(f"  downloaded: {dest.name} ({written / (1024 * 1024):.1f} MB)")
    return written


def _write_sidecar(mat_dir: Path, payload: dict) -> Path:
    """Write the provenance sidecar for a downloaded material folder."""
    path = Path(mat_dir) / SIDECAR_NAME
    payload = {"generator": "pbr2rad", **payload}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def fetch_catalog() -> list[dict]:
    """Fetch the Poly Haven texture catalog, normalized to the shared entry shape.

    Every source's catalog entries look like::

        {"id", "name", "preview", "preview_dark", "preview_large",
         "preview_large_dark", "tags", "maps", "dims_cm", "downloads"}

    so the web layer can search and display them without source-specific
    code. Poly Haven's listing carries no per-asset map/size/download data,
    so those are ``None`` here.
    """
    raw = _api_get("/assets?type=textures")
    entries: list[dict] = []
    if not isinstance(raw, dict):
        return entries
    for slug, data in raw.items():
        if not isinstance(data, dict):
            continue
        tags: list[str] = []
        for key in ("categories", "tags"):
            for t in data.get(key) or []:
                t = str(t)
                if t not in tags:
                    tags.append(t)
        entries.append({
            "id": str(slug),
            "name": str(data.get("name") or slug),
            "preview": _THUMB_URL.format(slug=slug, width=200),
            "preview_dark": None,   # Poly Haven thumbs already sit on a dark ground
            "preview_large": _THUMB_URL.format(slug=slug, width=512),
            "preview_large_dark": None,
            "tags": tags,
            "maps": None,
            "dims_cm": None,
            "downloads": None,
        })
    return entries


def download_texture_set(
    slug: str,
    output_dir: Path,
    *,
    resolution: str = "1k",
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
        cache = _cached_path(slug, resolution, fmt, url_filename)
        _download_file(url, dest, md5, verbose=verbose, cache=cache)

    _write_sidecar(mat_dir, {
        "source": "polyhaven",
        "source_name": "Poly Haven",
        "asset_id": slug,
        "name": str(info.get("name") or slug),
        "asset_url": f"https://polyhaven.com/a/{slug}",
        "license": "CC0-1.0",
        "authors": info.get("authors") or None,
        "resolution": resolution,
        "fmt": fmt,
        "dims_cm": None,
        "maps": sorted(ch for ch, *_ in to_download),
        "tags": [str(t) for t in (info.get("categories") or [])],
    })

    if verbose:
        print(f"  saved to: {mat_dir}")

    return mat_dir
