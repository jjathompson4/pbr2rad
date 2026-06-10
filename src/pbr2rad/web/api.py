"""REST API router for pbr2rad."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

from .. import __version__
from ..convert import ConvertOptions, convert_set, write_manifest
from ..discover import PBRSet, discover
from ..fetch import FetchError, download_texture_set
from . import tempdir
from .limits import rate_limit
from .models import (
    ChannelMap,
    ConvertOptionsRequest,
    ConvertResponse,
    DiscoverChannelResponse,
    DiscoverResponse,
    HealthResponse,
    PolyHavenConvertRequest,
)
from .preview import radiance_available, render_preview

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Upload safety limits (public-tool hardening)
# ---------------------------------------------------------------------------

MAX_FILES = 12                          # files per request
MAX_FILE_BYTES = 25 * 1024 * 1024       # 25 MB per file
MAX_TOTAL_BYTES = 80 * 1024 * 1024      # 80 MB per request
MAX_RESOLUTION = 2048                   # downscale ceiling (longest edge, px)
_UPLOAD_CHUNK = 1024 * 1024             # 1 MB streaming read

# Raster formats Pillow can open + the HDR/EXR maps the pipeline accepts.
_ALLOWED_EXTS = {
    ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    ".bmp", ".webp", ".exr", ".hdr",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _opts_from_request(req: ConvertOptionsRequest) -> ConvertOptions:
    """Convert a Pydantic model to a library ConvertOptions."""
    return ConvertOptions(
        projection=req.projection,
        planar_axis=req.planar_axis,
        u_scale=req.u_scale,
        v_scale=req.v_scale,
        u_offset=req.u_offset,
        v_offset=req.v_offset,
        roughness_override=req.roughness_override,
        metalness_override=req.metalness_override,
        normal=req.normal,
        bump_scale=req.bump_scale,
        varying_roughness=req.varying_roughness,
        rough_modulation=req.rough_modulation,
        estimate_maps=req.estimate_maps,
        rotate_per_map=req.rotate_per_map or {},
        flip_h=req.flip_h,
        flip_v=req.flip_v,
    )


def _build_pbrset_from_labels(
    channels: list[ChannelMap],
    upload_dir: Path,
    name: str,
) -> PBRSet:
    """Construct a PBRSet from explicit user labels."""
    pbr = PBRSet(name=name, root=upload_dir)
    for cm in channels:
        filepath = upload_dir / cm.filename
        if filepath.exists():
            pbr.maps[cm.channel] = filepath
    return pbr


_SKIP_EXTS = {".oct", ".bmp"}
_SKIP_PREFIXES = {"preview_"}


def _zip_directory(directory: Path) -> io.BytesIO:
    """Zip a directory tree into an in-memory buffer.

    Skips Radiance temp files (octrees, BMP intermediates) from preview rendering.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            # Skip preview intermediates
            if f.suffix in _SKIP_EXTS:
                continue
            if any(f.name.startswith(p) for p in _SKIP_PREFIXES):
                continue
            zf.write(f, f.relative_to(directory.parent))
    buf.seek(0)
    return buf


def _make_response(
    job_id: str,
    result,
    has_preview: bool = False,
) -> ConvertResponse:
    return ConvertResponse(
        job_id=job_id,
        name=result.name,
        primitive=result.primitive,
        roughness=round(result.roughness, 4),
        metalness=round(result.metalness, 4),
        avg_rgb=[round(c, 4) for c in result.avg_rgb],
        resolution=[result.width, result.height],
        download_url=f"/api/v1/download/{job_id}",
        preview_url=f"/api/v1/preview/{job_id}" if has_preview else None,
        channels_used=list(getattr(result, "channels_used", []) or []),
    )


async def _save_uploads(
    files: list[UploadFile],
    upload_dir: Path,
) -> list[str]:
    """Save uploaded files to disk, return list of filenames.

    Enforces count, per-file size, total size, and extension limits, and
    streams to disk in chunks so an oversized upload is rejected before it is
    fully buffered in memory. Filenames are reduced to their basename to
    prevent path-traversal escapes out of ``upload_dir``.
    """
    if not files:
        raise HTTPException(400, "No files uploaded.")
    if len(files) > MAX_FILES:
        raise HTTPException(
            413, f"Too many files ({len(files)}). Limit is {MAX_FILES} per request."
        )

    filenames: list[str] = []
    total = 0
    for f in files:
        name = Path(f.filename or "").name  # strip any directory components
        if not name:
            raise HTTPException(400, "An uploaded file is missing a filename.")
        ext = Path(name).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(
                415,
                f"Unsupported file type: {name!r}. Allowed: "
                "png, jpg, jpeg, tif, tiff, bmp, webp, exr, hdr.",
            )

        dest = upload_dir / name
        size = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await f.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(
                            413,
                            f"{name!r} exceeds the "
                            f"{MAX_FILE_BYTES // (1024 * 1024)} MB per-file limit.",
                        )
                    if total > MAX_TOTAL_BYTES:
                        raise HTTPException(
                            413,
                            "Upload exceeds the "
                            f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB total limit.",
                        )
                    out.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        filenames.append(name)
    return filenames


def _downscale_uploads(upload_dir: Path, max_dim: int = MAX_RESOLUTION) -> None:
    """Downscale any image whose longest edge exceeds ``max_dim``, in place.

    Caps per-job memory and output size for a public deployment. Non-raster or
    unreadable files (e.g. .hdr/.exr with no Pillow decoder) are left untouched.
    """
    for p in upload_dir.iterdir():
        if not p.is_file():
            continue
        try:
            with Image.open(p) as im:
                im.load()
                w, h = im.size
                longest = max(w, h)
                if longest <= max_dim:
                    continue
                scale = max_dim / longest
                new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
                im.resize(new_size, Image.LANCZOS).save(p)
        except Exception:
            # Not a Pillow-decodable raster; leave as-is.
            continue


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        version=__version__,
        radiance_available=radiance_available(),
    )


@router.post("/discover", response_model=DiscoverResponse, dependencies=[Depends(rate_limit)])
async def discover_channels(
    files: list[UploadFile] = File(...),
):
    """Upload images and auto-detect PBR channels."""
    job_id, upload_dir, _ = tempdir.new_job()
    await _save_uploads(files, upload_dir)

    pbr = discover(upload_dir)

    # Build response: for each file, show what channel it was assigned
    channels = []
    assigned_files = {p.name for p in pbr.maps.values()}
    for f in files:
        ch = None
        for channel_name, path in pbr.maps.items():
            if path.name == f.filename:
                ch = channel_name
                break
        channels.append(DiscoverChannelResponse(filename=f.filename, channel=ch))

    extras = [p.name for p in pbr.extras]
    return DiscoverResponse(name=pbr.name, channels=channels, extras=extras)


@router.post("/convert/upload", response_model=ConvertResponse, dependencies=[Depends(rate_limit)])
async def convert_upload(
    files: list[UploadFile] = File(...),
    options: str = Form("{}"),
    channels: str = Form(""),
    name: str = Form(""),
):
    """Upload images and convert to Radiance material."""
    job_id, upload_dir, output_dir = tempdir.new_job()
    await _save_uploads(files, upload_dir)
    _downscale_uploads(upload_dir)

    # Parse options
    try:
        opts_req = ConvertOptionsRequest(**json.loads(options))
    except (json.JSONDecodeError, Exception) as exc:
        raise HTTPException(400, f"Invalid options JSON: {exc}")

    opts = _opts_from_request(opts_req)

    # Build PBRSet: user labels or auto-discover
    if channels.strip():
        try:
            channel_list = [ChannelMap(**c) for c in json.loads(channels)]
        except (json.JSONDecodeError, Exception) as exc:
            raise HTTPException(400, f"Invalid channels JSON: {exc}")
        mat_name = name or "material"
        pbr = _build_pbrset_from_labels(channel_list, upload_dir, mat_name)
    else:
        pbr = discover(upload_dir, name=name or None)

    if pbr.albedo is None:
        raise HTTPException(400, "No albedo/diffuse map found. Label at least one file as 'albedo'.")

    try:
        result = convert_set(pbr, output_dir, opts)
        write_manifest([result], output_dir)
    except Exception as exc:
        raise HTTPException(500, f"Conversion failed: {exc}")

    # Try render preview
    has_preview = False
    if radiance_available():
        preview_png = output_dir / result.name / "preview.png"
        has_preview = render_preview(
            result.out_dir, result.rad_file, preview_png,
        )

    return _make_response(job_id, result, has_preview)


@router.post("/convert/polyhaven", response_model=ConvertResponse, dependencies=[Depends(rate_limit)])
async def convert_polyhaven(req: PolyHavenConvertRequest):
    """Fetch a Poly Haven material and convert to Radiance."""
    job_id, upload_dir, output_dir = tempdir.new_job()

    try:
        mat_dir = download_texture_set(
            req.slug, upload_dir,
            resolution=req.resolution, fmt=req.fmt,
        )
    except FetchError as exc:
        raise HTTPException(400, str(exc))

    _downscale_uploads(mat_dir)
    pbr = discover(mat_dir)
    if pbr.albedo is None:
        raise HTTPException(400, f"No albedo map found in Poly Haven asset '{req.slug}'")

    opts = _opts_from_request(req.options)

    try:
        result = convert_set(pbr, output_dir, opts)
        write_manifest([result], output_dir)
    except Exception as exc:
        raise HTTPException(500, f"Conversion failed: {exc}")

    has_preview = False
    if radiance_available():
        preview_png = output_dir / result.name / "preview.png"
        has_preview = render_preview(
            result.out_dir, result.rad_file, preview_png,
        )

    return _make_response(job_id, result, has_preview)


@router.get("/download/{job_id}")
async def download(job_id: str):
    """Download the converted material as a zip file."""
    out_dir = tempdir.get_output_dir(job_id)
    if out_dir is None:
        raise HTTPException(404, "Job not found or expired")

    buf = _zip_directory(out_dir)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=pbr2rad_{job_id}.zip"},
    )


@router.get("/preview/{job_id}")
async def preview(job_id: str):
    """Return the render preview PNG for a job."""
    out_dir = tempdir.get_output_dir(job_id)
    if out_dir is None:
        raise HTTPException(404, "Job not found or expired")

    # Find the preview.png in any material subdirectory
    for png in out_dir.rglob("preview.png"):
        return FileResponse(png, media_type="image/png")

    raise HTTPException(404, "No preview available (Radiance not installed?)")


# ---------------------------------------------------------------------------
# Poly Haven proxy (avoids CORS)
# ---------------------------------------------------------------------------

_CATALOG_CACHE: dict[str, object] = {"data": None, "fetched_at": 0.0}
_CATALOG_TTL_SECONDS = 3600  # refresh hourly


def _catalog_disk_path() -> Path:
    from ..fetch import _cache_root
    return _cache_root() / "catalog_textures.v1.json"


def _polyhaven_catalog() -> dict:
    """Two-tier cache for the Poly Haven texture catalog.

    L1: in-process dict (zero I/O on hot path).
    L2: JSON file on disk (survives process restarts; shared across workers).
    Miss both: fetch from api.polyhaven.com, atomically write to L2,
    populate L1.
    """
    import time
    import os as _os
    import tempfile
    import urllib.request
    import json as _json

    now = time.time()

    # L1: in-memory
    if (
        _CATALOG_CACHE["data"] is not None
        and now - _CATALOG_CACHE["fetched_at"] < _CATALOG_TTL_SECONDS
    ):
        return _CATALOG_CACHE["data"]  # type: ignore[return-value]

    # L2: disk file, if fresh enough
    disk_path = _catalog_disk_path()
    if disk_path.exists():
        try:
            mtime = disk_path.stat().st_mtime
            if now - mtime < _CATALOG_TTL_SECONDS:
                data = _json.loads(disk_path.read_text())
                _CATALOG_CACHE["data"] = data
                _CATALOG_CACHE["fetched_at"] = mtime
                return data
        except (OSError, ValueError):
            pass  # corrupt or unreadable — fall through and refetch

    # Miss: fetch from Poly Haven
    url = "https://api.polyhaven.com/assets?type=textures"
    req = urllib.request.Request(url, headers={"User-Agent": "pbr2rad/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read())

    # Atomic write to disk (tempfile + os.replace — survives concurrent writes)
    try:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(disk_path.parent),
            prefix=".catalog_", suffix=".tmp", delete=False,
        ) as tmp:
            _json.dump(data, tmp)
            tmp_path = Path(tmp.name)
        _os.replace(tmp_path, disk_path)
    except OSError:
        pass  # disk cache is best-effort

    _CATALOG_CACHE["data"] = data
    _CATALOG_CACHE["fetched_at"] = now
    return data


@router.get("/polyhaven/search")
async def polyhaven_search(q: str = ""):
    """Search Poly Haven textures (catalog cached for 1 hour)."""
    try:
        all_assets = _polyhaven_catalog()
    except Exception as exc:
        raise HTTPException(502, f"Poly Haven API error: {exc}")

    # Filter by query
    if q:
        q_lower = q.lower()
        filtered = {
            k: v for k, v in all_assets.items()
            if q_lower in k.lower() or q_lower in str(v.get("name", "")).lower()
        }
    else:
        filtered = dict(list(all_assets.items())[:50])

    results = []
    for slug, data in list(filtered.items())[:30]:
        results.append({
            "slug": slug,
            "name": data.get("name", slug),
            "preview": f"https://cdn.polyhaven.com/asset_img/thumbs/{slug}.png?width=200",
        })
    return results


@router.get("/polyhaven/{slug}/info")
async def polyhaven_info(slug: str):
    """Get Poly Haven asset info and available resolutions."""
    from ..fetch import fetch_asset_info, fetch_asset_files, FetchError

    try:
        info = fetch_asset_info(slug)
        files = fetch_asset_files(slug)
    except FetchError as exc:
        raise HTTPException(404, str(exc))

    # Extract available resolutions
    resolutions = set()
    for channel_data in files.values():
        if isinstance(channel_data, dict):
            resolutions.update(channel_data.keys())

    # Build per-map previews. Map Poly Haven channel keys to our discover
    # channel names so the frontend can pass rotations back under the same
    # keys the conversion pipeline uses.
    _PH_TO_INTERNAL = {
        "Diffuse": "albedo",
        "nor_gl": "normal_gl",
        "nor_dx": "normal_dx",
        "Rough": "roughness",
        "Metal": "metalness",
        "AO": "ao",
        "Displacement": "displacement",
        "arm": "arm",
    }
    # Prefer a low-res JPG for the UI thumbnail to keep the browser grid snappy.
    PREVIEW_RES_ORDER = ("1k", "2k", "4k")
    maps = []
    for ph_key, internal in _PH_TO_INTERNAL.items():
        ch = files.get(ph_key)
        if not isinstance(ch, dict):
            continue
        thumb_url = None
        for res in PREVIEW_RES_ORDER:
            res_data = ch.get(res)
            if not isinstance(res_data, dict):
                continue
            for fmt in ("jpg", "png"):
                fmt_data = res_data.get(fmt)
                if isinstance(fmt_data, dict) and fmt_data.get("url"):
                    thumb_url = fmt_data["url"]
                    break
            if thumb_url:
                break
        if thumb_url:
            maps.append({"channel": internal, "thumbnail_url": thumb_url})

    return {
        "slug": slug,
        "name": info.get("name", slug),
        "categories": info.get("categories", []),
        "resolutions": sorted(resolutions),
        "maps": maps,
    }
