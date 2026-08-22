"""ambientCG API client for downloading PBR material sets.

Downloads CC0 materials from ``ambientcg.com`` into a local folder suitable
for conversion with ``pbr2rad``. Uses only stdlib (``urllib.request``,
``zipfile``) — no new dependencies.

ambientCG ships one ZIP per (resolution, format) holding every map plus
extras (``.mtlx``, ``.usdc``, …). We download that ZIP once into the cache
and extract only the image members the conversion actually consumes. The
API publishes no checksums, so cache entries are validated by size.

The public API (v3) is documented at https://docs.ambientcg.com/api/ and
self-describes as built "with hobbyists and educators in mind" — callers
should cache aggressively and tolerate failures (see ``web/api.py``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import discover as discover_mod
from .fetch import (
    FetchError,
    _USER_AGENT,
    _cache_root,
    _stream_to_file,
    _write_sidecar,
    cache_total_bytes,
    enforce_cache_budget,
)

log = logging.getLogger("pbr2rad.ambientcg")

SOURCE_KEY = "ambientcg"
_API_URL = "https://ambientcg.com/api/v3/assets"
_API_HOST = "ambientcg.com"
_ASSET_INCLUDE = "title,tags,thumbnails,dimensions,maps,downloads"
_ASSET_URL = "https://ambientcg.com/a/{id}"

# Every ambientCG id is letters+digits (Bricks104, Plastic015A, …). Checked
# before any network call; the canonical id also becomes a directory name.
_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")

# (resolution, fmt) → download attribute. ambientCG offers PNG/JPG only.
_ATTRS = {
    ("1k", "png"): "1K-PNG", ("1k", "jpg"): "1K-JPG",
    ("2k", "png"): "2K-PNG", ("2k", "jpg"): "2K-JPG",
}
_ATTR_RES = {"1K": "1k", "2K": "2k"}

# Safety caps for a small public host. 2K-PNG packs top out near 48 MB.
MAX_ZIP_BYTES = 64 << 20
MAX_MEMBER_BYTES = 64 << 20
MAX_EXTRACT_BYTES = 192 << 20
_CHUNK = 1 << 20

# Channels convert_set() consumes. Displacement / AO / opacity / emission
# are left in the zip — they'd only cost disk and downscaling time.
_EXTRACT_CHANNELS = frozenset(
    {"albedo", "roughness", "metalness", "normal_gl", "normal_dx", "normal"}
)

# ambientCG ``maps`` vocabulary → discover channel names (for UI map lists).
ACG_TO_INTERNAL = {
    "color": "albedo",
    "normal": "normal_gl",       # packs carry NormalGL + NormalDX; GL wins in discover
    "roughness": "roughness",
    "metalness": "metalness",
    "ambient-occlusion": "ao",
    "displacement": "displacement",
}

# Thumbnail variants: white-background for light UIs, #242424 for dark ones;
# 256 px for the grid, 512 px for the selected-asset hero.
_PREVIEW_KEYS = ("256-JPG-FFFFFF", "256-PNG", "128-JPG-FFFFFF", "128-PNG", "256-WEBP")
_PREVIEW_DARK_KEYS = ("256-JPG-242424", "128-JPG-242424")
_PREVIEW_LARGE_KEYS = ("512-JPG-FFFFFF", "512-PNG", "256-JPG-FFFFFF", "256-PNG")
_PREVIEW_LARGE_DARK_KEYS = ("512-JPG-242424", "256-JPG-242424")

# Per-map thumbnails generated from the 1K-JPG pack for the web UI (ambientCG
# publishes none). Stored next to the cached zip; served inline as data URIs.
THUMB_SIZE = 192
MAX_THUMB_SRC_PIXELS = 4096 * 4096   # refuse absurd members before decoding
THUMB_CHANNELS = ("albedo", "normal_gl", "normal_dx", "roughness", "metalness", "ao", "displacement")
_THUMB_DIRNAME = "thumbs_1K-JPG"
_THUMB_INDEX = "index.json"
# Thumbnail requests trigger zip downloads; bound them so browsing can never
# turn the site into a bulk downloader of ambientCG, whatever the traffic:
#  * a process-wide token bucket on pack downloads started for thumbnails
#    (bursts of THUMB_DL_BURST, then THUMB_DL_PER_MIN sustained),
#  * at most THUMB_DL_CONCURRENCY such downloads/decodes in flight,
#  * no thumbnail downloads at all once the cache exceeds MAX_THUMB_CACHE_BYTES
#    (the LRU budget in fetch.py keeps the cache bounded anyway).
MAX_THUMB_CACHE_BYTES = 2 << 30
THUMB_DL_BURST = 30
THUMB_DL_PER_MIN = 3.0
THUMB_DL_CONCURRENCY = 2
_THUMB_LOCKS: dict[str, threading.Lock] = {}
_THUMB_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_DOWNLOAD_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_URL = "https://ambientcg.com/get?file={id}_{attr}.zip"


class TokenBucket:
    """Tiny thread-safe token bucket (``capacity`` burst, ``per_min`` refill)."""

    def __init__(self, capacity: float, per_min: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(per_min) / 60.0
        self.tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self, n: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


_THUMB_DOWNLOADS = TokenBucket(THUMB_DL_BURST, THUMB_DL_PER_MIN)
_THUMB_DL_SEM = threading.BoundedSemaphore(THUMB_DL_CONCURRENCY)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _api_get_url(url: str) -> dict:
    """GET a JSON document from the ambientCG API."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise FetchError(f"ambientCG API error {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error: {exc.reason}") from exc
    except ValueError as exc:
        raise FetchError(f"ambientCG API returned invalid JSON for {url}") from exc
    if not isinstance(data, dict):
        raise FetchError(f"ambientCG API returned unexpected payload for {url}")
    return data


def _api_get(params: dict[str, str]) -> dict:
    return _api_get_url(f"{_API_URL}?{urllib.parse.urlencode(params)}")


def asset_url(asset_id: str) -> str:
    return _ASSET_URL.format(id=asset_id)


def fetch_asset(asset_id: str) -> dict:
    """Fetch metadata for one material (canonical id, downloads, maps, …).

    Ids are matched case-insensitively by the API; the returned ``id`` is the
    canonical spelling. Unknown or non-material ids raise ``FetchError``.
    """
    if not _ID_RE.match(asset_id or ""):
        raise FetchError(
            f"invalid ambientCG asset id {asset_id!r} "
            "(expected letters and digits only, e.g. 'Bricks104')"
        )
    data = _api_get({"type": "material", "id": asset_id, "include": _ASSET_INCLUDE})
    assets = data.get("assets") or []
    if not assets or not isinstance(assets[0], dict):
        raise FetchError(f"asset not found on ambientCG (or not a material): {asset_id!r}")
    return assets[0]


def _zip_downloads(asset: dict) -> dict[str, tuple[str, int]]:
    """Map download attribute ("1K-JPG", …) → ``(url, size)`` for zip packs."""
    out: dict[str, tuple[str, int]] = {}
    for d in asset.get("downloads") or []:
        if not isinstance(d, dict):
            continue
        if str(d.get("extension") or "zip").lower() != "zip":
            continue
        attr = d.get("attributes") or d.get("attribute")
        url = d.get("url") or d.get("downloadLink")
        if not attr or not url:
            continue
        try:
            size = int(d.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out[str(attr)] = (str(url), size)
    return out


def _zip_attribute(resolution: str, fmt: str) -> str:
    attr = _ATTRS.get((str(resolution).lower(), str(fmt).lower()))
    if attr is None:
        raise FetchError(
            f"ambientCG offers png/jpg at 1k/2k only "
            f"(requested resolution={resolution!r}, format={fmt!r})"
        )
    return attr


def _pick_download(asset: dict, attr: str) -> tuple[str, int]:
    downloads = _zip_downloads(asset)
    hit = downloads.get(attr)
    if hit is None:
        available = sorted(a for a in downloads if a[:2] in _ATTR_RES)
        raise FetchError(
            f"{asset.get('id')!r} has no {attr} download "
            f"(available: {', '.join(available) or 'none'})"
        )
    return hit


def normalize_asset(asset: dict) -> dict:
    """Reduce a v3 asset record to the shared catalog entry shape.

    ``{"id", "name", "preview", "tags", "maps", "dims_cm", "downloads"}`` —
    the same keys ``fetch.fetch_catalog`` produces for Poly Haven.
    """
    aid = str(asset.get("id") or "")
    thumbs = asset.get("thumbnails")
    preview = None
    preview_dark = None
    preview_large = None
    preview_large_dark = None
    if isinstance(thumbs, dict):
        def _first(keys: tuple[str, ...]) -> str | None:
            for key in keys:
                if thumbs.get(key):
                    return str(thumbs[key])
            return None
        preview = _first(_PREVIEW_KEYS)
        if preview is None:
            for v in thumbs.values():
                if isinstance(v, str) and v:
                    preview = v
                    break
        preview_dark = _first(_PREVIEW_DARK_KEYS)
        preview_large = _first(_PREVIEW_LARGE_KEYS) or preview
        preview_large_dark = _first(_PREVIEW_LARGE_DARK_KEYS) or preview_dark
    dims = asset.get("dimensions") or {}
    dims_cm = None
    if isinstance(dims, dict):
        try:
            w, h = float(dims.get("width") or 0), float(dims.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0.0
        if w > 0 and h > 0:
            dims_cm = [w, h]
    downloads = {
        attr: size for attr, (_url, size) in _zip_downloads(asset).items()
        if attr[:2] in _ATTR_RES
    }
    return {
        "id": aid,
        "name": str(asset.get("title") or aid),
        "preview": preview,
        "preview_dark": preview_dark,
        "preview_large": preview_large,
        "preview_large_dark": preview_large_dark,
        "tags": [str(t) for t in (asset.get("tags") or [])],
        "maps": [str(m) for m in (asset.get("maps") or [])],
        "dims_cm": dims_cm,
        "downloads": downloads,
    }


def fetch_catalog(*, limit: int = 500, max_pages: int = 20) -> list[dict]:
    """Fetch every material (≈2k entries, ~5 pages), normalized and popular-first.

    Any page failure raises ``FetchError`` — a half catalog would silently
    hide assets, and the web layer already keeps a stale copy to fall back on.
    """
    params = {
        "type": "material", "sort": "popular",
        "limit": str(limit), "offset": "0", "include": _ASSET_INCLUDE,
    }
    url = f"{_API_URL}?{urllib.parse.urlencode(params)}"
    entries: list[dict] = []
    seen: set[str] = set()
    for _page in range(max_pages):
        data = _api_get_url(url)
        for raw in data.get("assets") or []:
            if not isinstance(raw, dict):
                continue
            entry = normalize_asset(raw)
            if entry["id"] and entry["id"] not in seen:
                seen.add(entry["id"])
                entries.append(entry)
        nxt = data.get("nextPageHttp")
        if not nxt:
            break
        # Only follow pagination back to the same API host.
        host = urllib.parse.urlparse(str(nxt)).hostname or ""
        if host.lower() != _API_HOST:
            log.warning("ignoring off-host nextPageHttp %r", nxt)
            break
        url = str(nxt)
    return entries


# ---------------------------------------------------------------------------
# Download + extract
# ---------------------------------------------------------------------------

def _cached_zip_path(asset_id: str, attr: str) -> Path:
    return _cache_root(SOURCE_KEY) / asset_id / f"{asset_id}_{attr}.zip"


def _download_lock(key: str) -> threading.Lock:
    """One lock per (asset, pack) so two callers never stream into the same
    ``.part`` file (e.g. a tile click building thumbnails while the user
    already hit Fetch & Convert for the same asset)."""
    with _DOWNLOAD_LOCKS_GUARD:
        lock = _DOWNLOAD_LOCKS.get(key)
        if lock is None:
            lock = _DOWNLOAD_LOCKS[key] = threading.Lock()
        return lock


def asset_record_from_entry(entry: dict) -> dict:
    """Build a minimal asset record from a normalized catalog entry.

    The catalog already knows every pack's attribute and size; the download
    link follows ambientCG's stable ``/get?file=<Id>_<ATTR>.zip`` form, so
    callers holding a catalog entry can skip the per-asset API call.
    """
    aid = str(entry.get("id") or "")
    downloads = []
    for attr, size in (entry.get("downloads") or {}).items():
        downloads.append({
            "attributes": str(attr),
            "extension": "zip",
            "url": _DOWNLOAD_URL.format(id=aid, attr=attr),
            "size": int(size or 0),
        })
    return {"id": aid, "title": entry.get("name") or aid, "downloads": downloads}


def _safe_member_name(raw: str) -> str | None:
    """Return the basename for a zip member, or None if it must be skipped.

    Members are flattened to their basename (ambientCG may add subfolders),
    but anything that smells like traversal — absolute paths, ``..`` parts,
    backslashes, drive colons — is refused outright rather than sanitized.
    """
    if not raw or raw.endswith("/"):
        return None
    if raw.startswith("/") or "\\" in raw or ":" in raw or "\0" in raw:
        return None
    parts = raw.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts[-1]


def _extract_maps(
    zip_path: Path,
    dest_dir: Path,
    *,
    channels: frozenset[str] = _EXTRACT_CHANNELS,
    verbose: bool = False,
) -> list[Path]:
    """Extract the image members classified into ``channels`` from ``zip_path``.

    Enforces per-member and total size caps and refuses members whose bytes
    exceed their declared size. A corrupt archive is evicted from the cache
    and reported as ``FetchError``.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = _safe_member_name(info.filename)
                if name is None:
                    log.warning("skipping unsafe zip member %r", info.filename)
                    continue
                if Path(name).suffix.lower() not in discover_mod._IMAGE_EXTS:
                    continue
                channel = discover_mod._classify(name)
                if channel not in channels:
                    continue
                if name in seen:
                    log.warning("duplicate zip member %r — keeping the first", name)
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise FetchError(
                        f"zip member {name!r} is {info.file_size >> 20} MB — "
                        f"over the {MAX_MEMBER_BYTES >> 20} MB per-file limit"
                    )
                total += info.file_size
                if total > MAX_EXTRACT_BYTES:
                    raise FetchError(
                        f"{zip_path.name} expands past the "
                        f"{MAX_EXTRACT_BYTES >> 20} MB limit — refusing to extract"
                    )
                dest = dest_dir / name
                written = 0
                try:
                    with zf.open(info) as src, dest.open("wb") as out:
                        while True:
                            chunk = src.read(_CHUNK)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > info.file_size:
                                raise FetchError(
                                    f"zip member {name!r} is larger than declared "
                                    "— refusing to extract"
                                )
                            out.write(chunk)
                except BaseException:
                    dest.unlink(missing_ok=True)
                    raise
                seen.add(name)
                extracted.append(dest)
                if verbose:
                    print(f"  extracted: {name} ({info.file_size / (1024 * 1024):.1f} MB, {channel})")
    except zipfile.BadZipFile as exc:
        Path(zip_path).unlink(missing_ok=True)  # evict — it will never get better
        raise FetchError(f"{Path(zip_path).name} is not a valid zip archive") from exc
    return extracted


def ensure_zip(
    asset_id: str,
    *,
    resolution: str = "1k",
    fmt: str = "png",
    verbose: bool = False,
    allow_download: bool = True,
    asset: dict | None = None,
) -> tuple[Path, dict]:
    """Return ``(cached_zip_path, asset_record)`` for one material pack.

    Validates the request, looks the asset up (canonical id) unless a record
    is supplied, and downloads the zip into the cache unless a size-matching
    copy is already there. Downloads of the same pack are serialized. With
    ``allow_download=False`` a cache miss raises instead of fetching.
    """
    attr = _zip_attribute(resolution, fmt)  # validate before touching the network
    if asset is None:
        asset = fetch_asset(asset_id)
    cid = str(asset.get("id") or asset_id)
    if not _ID_RE.match(cid):
        raise FetchError(f"ambientCG returned an unexpected asset id {cid!r}")

    url, size = _pick_download(asset, attr)
    if size > MAX_ZIP_BYTES:
        raise FetchError(
            f"{cid} {attr} is {size >> 20} MB — over the {MAX_ZIP_BYTES >> 20} MB limit"
        )

    zip_path = _cached_zip_path(cid, attr)

    def _is_cached() -> bool:
        return zip_path.exists() and (size == 0 or zip_path.stat().st_size == size)

    if _is_cached():
        if verbose:
            print(f"  cache hit: {zip_path.name} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB)")
        return zip_path, asset
    if not allow_download:
        raise FetchError(f"{zip_path.name} is not cached and downloads are disabled")

    with _download_lock(f"{cid}:{attr}"):
        if _is_cached():          # someone else finished it while we waited
            return zip_path, asset
        if verbose:
            print(f"downloading {cid} ({attr}, {size / (1024 * 1024):.1f} MB):")
        written = _stream_to_file(url, zip_path, max_bytes=MAX_ZIP_BYTES, verbose=verbose)
        if size and written != size:
            zip_path.unlink(missing_ok=True)
            raise FetchError(
                f"size mismatch for {zip_path.name}: expected {size} bytes, got {written}"
            )
    enforce_cache_budget()
    return zip_path, asset


def download_texture_set(
    asset_id: str,
    output_dir: Path,
    *,
    resolution: str = "1k",
    fmt: str = "png",
    verbose: bool = False,
) -> Path:
    """Download one ambientCG material and unpack its usable maps.

    Creates ``<output_dir>/<CanonicalId>/`` holding the color / roughness /
    metalness / normal maps plus a ``pbr2rad_source.json`` provenance sidecar.
    Returns the path to that folder.
    """
    zip_path, asset = ensure_zip(
        asset_id, resolution=resolution, fmt=fmt, verbose=verbose,
    )
    cid = str(asset.get("id") or asset_id)
    mat_dir = Path(output_dir) / cid
    mat_dir.mkdir(parents=True, exist_ok=True)
    extracted = _extract_maps(zip_path, mat_dir, verbose=verbose)
    if not extracted:
        raise FetchError(f"no usable texture maps found in {zip_path.name}")

    entry = normalize_asset(asset)
    _write_sidecar(mat_dir, {
        "source": SOURCE_KEY,
        "source_name": "ambientCG",
        "asset_id": cid,
        "name": entry["name"],
        "asset_url": asset_url(cid),
        "license": "CC0-1.0",
        "resolution": resolution,
        "fmt": fmt,
        "dims_cm": entry["dims_cm"],
        "maps": entry["maps"],
        "tags": entry["tags"],
    })

    if verbose:
        print(f"  saved to: {mat_dir}")
    return mat_dir


# ---------------------------------------------------------------------------
# Per-map thumbnails for the web UI
# ---------------------------------------------------------------------------

def _thumb_lock(asset_id: str) -> threading.Lock:
    with _THUMB_LOCKS_GUARD:
        lock = _THUMB_LOCKS.get(asset_id)
        if lock is None:
            lock = _THUMB_LOCKS[asset_id] = threading.Lock()
        return lock


def _thumb_dir(asset_id: str) -> Path:
    return _cache_root(SOURCE_KEY) / asset_id / _THUMB_DIRNAME


def _read_thumb_index(asset_id: str) -> dict[str, Path] | None:
    """Cached thumbnails, if the index says they're complete."""
    tdir = _thumb_dir(asset_id)
    index = tdir / _THUMB_INDEX
    if not index.is_file():
        return None
    try:
        names = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(names, dict):
        return None
    out: dict[str, Path] = {}
    for channel, fname in names.items():
        path = tdir / str(fname)
        if not path.is_file():
            return None  # incomplete — regenerate
        out[str(channel)] = path
    return out


def map_thumbnails(
    asset_id: str,
    *,
    size: int = THUMB_SIZE,
    verbose: bool = False,
    entry: dict | None = None,
) -> dict[str, Path]:
    """Return ``{channel: jpeg_path}`` thumbnails for a material's maps.

    Generated once from the cached 1K-JPG pack (the same one the web convert
    uses, so a later conversion is a cache hit) and stored beside it. The
    pack is read directly from the zip — nothing is extracted. ``normal_dx``
    is dropped when a GL normal exists (discover prefers GL).

    Returns ``{}`` (label-only tiles) — without touching the network — when
    the pack isn't cached and either the thumbnail download bucket is empty
    or the cache is over ``MAX_THUMB_CACHE_BYTES``. A catalog ``entry`` lets
    us skip the per-asset API lookup. Transient failures leave no index
    behind, so the next request retries.
    """
    from PIL import Image

    cached = _read_thumb_index(asset_id)
    if cached is not None:
        return cached

    with _thumb_lock(asset_id):
        cached = _read_thumb_index(asset_id)
        if cached is not None:
            return cached

        pack_cached = _cached_zip_path(asset_id, "1K-JPG").exists()
        if not pack_cached:
            if cache_total_bytes() > MAX_THUMB_CACHE_BYTES:
                log.warning("cache over %d MB; skipping thumbnail download for %s",
                            MAX_THUMB_CACHE_BYTES >> 20, asset_id)
                return {}
            if not _THUMB_DOWNLOADS.try_acquire():
                log.info("thumbnail download budget exhausted; label tiles for %s", asset_id)
                return {}

        record = asset_record_from_entry(entry) if entry and entry.get("downloads") else None
        with _THUMB_DL_SEM:
            zip_path, asset = ensure_zip(
                asset_id, resolution="1k", fmt="jpg", verbose=verbose, asset=record,
            )
            cid = str(asset.get("id") or asset_id)
            tdir = _thumb_dir(cid)
            tdir.mkdir(parents=True, exist_ok=True)
            found: dict[str, Path] = {}
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        name = _safe_member_name(info.filename)
                        if name is None or Path(name).suffix.lower() not in discover_mod._IMAGE_EXTS:
                            continue
                        channel = discover_mod._classify(name)
                        if channel not in THUMB_CHANNELS or channel in found:
                            continue
                        if info.file_size > MAX_MEMBER_BYTES:
                            continue
                        dest = tdir / f"{channel}.jpg"
                        try:
                            with zf.open(info) as src, Image.open(src) as img:
                                w, h = img.size
                                if w * h > MAX_THUMB_SRC_PIXELS:
                                    raise ValueError(f"{w}x{h} exceeds thumbnail source limit")
                                # JPEG draft mode decodes at a reduced scale —
                                # far less memory/CPU than a full 1K decode.
                                img.draft("RGB", (size * 2, size * 2))
                                img.load()
                                img = img.convert("RGB")
                                img.thumbnail((size, size), Image.LANCZOS)
                                img.save(dest, format="JPEG", quality=82, optimize=True)
                        except Exception:
                            log.warning("thumbnail failed for %s/%s", cid, name, exc_info=True)
                            dest.unlink(missing_ok=True)
                            continue
                        found[channel] = dest
            except zipfile.BadZipFile as exc:
                Path(zip_path).unlink(missing_ok=True)
                raise FetchError(f"{Path(zip_path).name} is not a valid zip archive") from exc

        if "normal_gl" in found and "normal_dx" in found:
            found["normal_dx"].unlink(missing_ok=True)
            del found["normal_dx"]

        if found:
            # Only a non-empty result is "done"; an empty one would pin the
            # asset to label tiles forever after one transient failure.
            (tdir / _THUMB_INDEX).write_text(
                json.dumps({ch: p.name for ch, p in found.items()}), encoding="utf-8",
            )
            enforce_cache_budget()
        return found
