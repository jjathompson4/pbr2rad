"""REST API router for pbr2rad."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

from .. import __version__
from .. import ambientcg as acg
from ..convert import ConvertOptions, convert_set, write_manifest
from ..discover import PBRSet, discover
from ..fetch import FetchError, _cache_root
from ..pvw import make_preview_png, write_pvw
from ..sources import SOURCES, TextureSource, get_catalog_fetcher, get_downloader, get_source
from . import tempdir
from .limits import rate_limit, rate_limit_light
from .models import (
    ChannelMap,
    ConvertOptionsRequest,
    ConvertResponse,
    DiscoverChannelResponse,
    DiscoverResponse,
    HealthResponse,
    PolyHavenConvertRequest,
    RerenderRequest,
    SourceConvertRequest,
)
from .preview import radiance_available, render_preview, rig_for

log = logging.getLogger("pbr2rad.web")

router = APIRouter(prefix="/api/v1")

# One conversion at a time: the deployment target is a single shared vCPU,
# so a second concurrent convert only adds thrash and memory pressure.
# Contention returns a fast 503 instead of queueing.
_CONVERT_LOCK = threading.BoundedSemaphore(1)
_BUSY_DETAIL = (
    "Another conversion is already running — this tool processes one at a "
    "time. Try again in a few seconds."
)


# Flat colour behind the (transparent) preview render in the ClimateStudio .pvw.
_PVW_BACKGROUND = (32, 32, 35)

# Display order for per-map tiles in the UI.
_MAP_ORDER = ("albedo", "normal_gl", "normal_dx", "normal", "roughness", "metalness", "ao", "displacement", "arm")

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

def _opts_from_request(
    req: ConvertOptionsRequest, *, source: str | None = None,
) -> ConvertOptions:
    """Convert a Pydantic model to a library ConvertOptions.

    The web app always asks convert_set for the extra reference-wrap chain
    (``preview_<name>.rad``): the Output-panel sphere renders the texture the
    way the source site renders its reference spheres (``source`` picks the
    site's convention; uploads get the default), so the side-by-side compares
    like with like. The exported material is the user's projection
    (box/triplanar by default) and is unaffected.
    """
    return ConvertOptions(
        write_preview_variant=True,
        preview_wrap_source=source,
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
        dat_resolution=req.dat_resolution,
        specularity_override=req.specularity_override,
        diffuse_scale=req.diffuse_scale,
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
# render_preview() writes both "preview_*" (scene, filtered HDR) and "preview.*"
# (octree, raw HDR, PNG) files into the material directory.  "preview." must be
# covered too: preview.hdr otherwise ships next to the material's real
# <name>.hdr albedo, where it reads as a texture map.
_SKIP_PREFIXES = {"preview_", "preview."}
# ...except the finished thumbnail, which is a deliverable in its own right.
_KEEP_NAMES = {"preview.png"}


def _zip_directory(directory: Path) -> io.BytesIO:
    """Zip a directory tree into an in-memory buffer.

    Skips Radiance temp files (octrees, HDR/BMP intermediates) from preview
    rendering, but keeps the rendered preview.png thumbnail.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            # Skip preview intermediates
            if f.name not in _KEEP_NAMES:
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
    *,
    source: str | None = None,
    preview_rev: int | None = None,
) -> ConvertResponse:
    source_url = None
    if source:
        # The material name is the canonical asset id for every source.
        source_url = get_source(source).asset_url(result.name)
    preview_url = None
    if has_preview:
        preview_url = f"/api/v1/preview/{job_id}"
        if preview_rev is not None:
            preview_url += f"?r={preview_rev}"   # re-renders must bust the <img> cache
    dims = None
    src_meta = getattr(result, "source", None) or {}
    if isinstance(src_meta, dict) and src_meta.get("dims_cm"):
        dims = [float(v) for v in src_meta["dims_cm"]]
    return ConvertResponse(
        job_id=job_id,
        name=result.name,
        primitive=result.primitive,
        roughness=round(result.roughness, 4),
        metalness=round(result.metalness, 4),
        avg_rgb=[round(c, 4) for c in result.avg_rgb],
        resolution=[result.width, result.height],
        download_url=f"/api/v1/download/{job_id}",
        preview_url=preview_url,
        channels_used=list(getattr(result, "channels_used", []) or []),
        channels_estimated=list(getattr(result, "channels_estimated", []) or []),
        source=source,
        source_url=source_url,
        specularity=round(float(getattr(result, "specularity", 0.05)), 4),
        roughness_radiance=round(float(getattr(result, "roughness_radiance", 0.0)), 4),
        diffuse_scale=round(float(getattr(result, "diffuse_scale", 1.0)), 4),
        reflectance=getattr(result, "reflectance", None) or None,
        avg_srgb_hex=getattr(result, "avg_srgb_hex", None),
        dimensions_cm=dims,
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
        tmp = p.with_name(f".{p.name}.resize_tmp")
        try:
            with Image.open(p) as im:
                im.load()
                w, h = im.size
                longest = max(w, h)
                if longest <= max_dim:
                    continue
                scale = max_dim / longest
                new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
                # Write to a sibling temp file and os.replace() over the
                # original. Saving in place would write THROUGH the hardlink
                # that _place_from_cache creates, silently corrupting the
                # shared Poly Haven cache entry; replace() breaks the link.
                im.resize(new_size, Image.LANCZOS).save(tmp, format=im.format)
            os.replace(tmp, p)
        except Exception:
            # Not a Pillow-decodable raster; leave as-is.
            tmp.unlink(missing_ok=True)
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

    pbr = await run_in_threadpool(discover, upload_dir)

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


def _convert_and_preview(pbr: PBRSet, output_dir: Path, opts: ConvertOptions):
    """Run the conversion + optional preview render. Blocking; call in a thread."""
    try:
        result = convert_set(pbr, output_dir, opts)
        write_manifest([result], output_dir)
    except HTTPException:
        raise
    except ValueError as exc:
        # Bad input (e.g. invalid material name) — the caller's fault.
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("conversion failed for %r", pbr.name)
        raise HTTPException(500, f"Conversion failed: {exc}")

    has_preview = False
    if radiance_available():
        preview_png = output_dir / result.name / "preview.png"
        # The reference-wrap chain convert_set wrote for us (see
        # _opts_from_request); falls back to the exported chain if absent.
        rad_for_preview = getattr(result, "preview_rad_file", None) or result.rad_file
        src_meta = getattr(result, "source", None) or {}
        has_preview = render_preview(
            result.out_dir, rad_for_preview, preview_png,
            rig=rig_for(result.primitive),    # metals: studio HDRI; others: light rig
            source=src_meta.get("source") if isinstance(src_meta, dict) else None,   # per-source reference exposure
            roughness=result.roughness,       # glossy: sampled specular; rough: folded
        )
        # convert_set already wrote a .pvw from the albedo swatch; a real
        # render of the material is a better thumbnail, so replace it. The
        # render has a transparent surround — flatten it for ClimateStudio.
        if has_preview and result.pvw_file is not None:
            try:
                write_pvw(
                    result.pvw_file, result.name,
                    make_preview_png(preview_png, background=_PVW_BACKGROUND),
                )
            except Exception:
                log.warning(
                    "could not refresh %s from render", result.pvw_file.name,
                    exc_info=True,
                )
    return result, has_preview


async def _run_conversion(
    job_id: str, work, *, source: str | None = None, preview_rev: int | None = None,
) -> ConvertResponse:
    """Run blocking conversion work in the threadpool, one job at a time.

    The event loop stays free to serve health checks and browsing traffic
    while the conversion grinds on the CPU.
    """
    if not _CONVERT_LOCK.acquire(blocking=False):
        raise HTTPException(503, _BUSY_DETAIL, headers={"Retry-After": "15"})
    try:
        result, has_preview = await run_in_threadpool(work)
    finally:
        _CONVERT_LOCK.release()
    return _make_response(job_id, result, has_preview, source=source, preview_rev=preview_rev)


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

    # Parse options/channels up front: cheap, and bad input should fail
    # before any heavy work starts.
    try:
        opts_req = ConvertOptionsRequest(**json.loads(options))
    except Exception as exc:
        raise HTTPException(400, f"Invalid options JSON: {exc}")
    opts = _opts_from_request(opts_req)

    channel_list: list[ChannelMap] | None = None
    if channels.strip():
        try:
            channel_list = [ChannelMap(**c) for c in json.loads(channels)]
        except Exception as exc:
            raise HTTPException(400, f"Invalid channels JSON: {exc}")

    def work():
        _downscale_uploads(upload_dir)
        if channel_list is not None:
            pbr = _build_pbrset_from_labels(channel_list, upload_dir, name or "material")
        else:
            pbr = discover(upload_dir, name=name or None)
        if pbr.albedo is None:
            raise HTTPException(
                400, "No albedo/diffuse map found. Label at least one file as 'albedo'."
            )
        # Remember how this job was built so the Output-panel sliders can
        # re-render it with overrides.
        tempdir.write_job_state(job_id, {
            "kind": "upload",
            "name": name or None,
            "mat_dir": "upload",
            "channels": [c.model_dump() for c in channel_list] if channel_list is not None else None,
            "options": opts_req.model_dump(),
        })
        return _convert_and_preview(pbr, output_dir, opts)

    return await _run_conversion(job_id, work)


def _source_or_404(source: str) -> TextureSource:
    try:
        return get_source(source)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


async def _convert_from_source(
    source: str,
    asset_id: str,
    resolution: str,
    fmt: str,
    options: ConvertOptionsRequest,
) -> ConvertResponse:
    """Fetch one asset from a registered source and convert it to Radiance."""
    src = _source_or_404(source)
    if fmt not in src.formats:
        raise HTTPException(
            400,
            f"{src.display_name} does not offer {fmt!r} "
            f"(choose from: {', '.join(src.formats)})",
        )
    download = get_downloader(source)
    job_id, upload_dir, output_dir = tempdir.new_job()
    opts = _opts_from_request(options, source=source)

    def work():
        try:
            mat_dir = download(asset_id, upload_dir, resolution=resolution, fmt=fmt)
        except FetchError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            # Disk full / cache dir unwritable — ours, not the user's fault.
            log.error("download/cache I/O failed for %s/%s: %s", source, asset_id, exc)
            raise HTTPException(503, "Download cache unavailable — try again shortly.",
                                headers={"Retry-After": "60"})

        _downscale_uploads(mat_dir)
        pbr = discover(mat_dir)
        if pbr.albedo is None:
            raise HTTPException(
                400, f"No albedo map found in {src.display_name} asset '{asset_id}'"
            )
        tempdir.write_job_state(job_id, {
            "kind": "source",
            "source": source,
            "asset_id": asset_id,
            "name": None,
            "mat_dir": os.path.relpath(mat_dir, upload_dir.parent),
            "channels": None,
            "options": options.model_dump(),
        })
        return _convert_and_preview(pbr, output_dir, opts)

    return await _run_conversion(job_id, work, source=source)


@router.post("/jobs/{job_id}/rerender", response_model=ConvertResponse, dependencies=[Depends(rate_limit)])
async def rerender_job(job_id: str, req: RerenderRequest):
    """Re-convert + re-render an existing job with material overrides.

    Drives the Output-panel sliders: the job's source maps and options are
    kept for its lifetime (30 min from the last touch), so a specularity /
    roughness / diffuse change re-bakes the material and preview in a few
    seconds without re-fetching anything.
    """
    job_dir = tempdir.get_job_dir(job_id)
    state = tempdir.read_job_state(job_id)
    if job_dir is None or state is None:
        raise HTTPException(404, "Job expired — convert the material again to keep tuning it.")

    try:
        if req.options is not None:
            opts_req = req.options.model_copy()
        else:
            opts_req = ConvertOptionsRequest(**(state.get("options") or {}))
    except Exception as exc:
        raise HTTPException(500, f"Stored job options are invalid: {exc}")
    if req.reset_specularity:
        opts_req.specularity_override = None
    if req.specularity is not None:
        opts_req.specularity_override = req.specularity
    if req.roughness is not None:
        opts_req.roughness_override = req.roughness
    if req.metalness is not None:
        opts_req.metalness_override = req.metalness
    if req.diffuse_scale is not None:
        opts_req.diffuse_scale = req.diffuse_scale
    state["options"] = opts_req.model_dump()
    source = state.get("source")
    opts = _opts_from_request(opts_req, source=source)

    mat_dir = job_dir / str(state.get("mat_dir") or "upload")
    output_dir = job_dir / "output"
    name = state.get("name")
    channels = state.get("channels")

    def work():
        if not mat_dir.is_dir():
            raise HTTPException(404, "Job expired — convert the material again to keep tuning it.")
        if channels:
            pbr = _build_pbrset_from_labels(
                [ChannelMap(**c) for c in channels], mat_dir, name or "material",
            )
        else:
            pbr = discover(mat_dir, name=name or None)
        if pbr.albedo is None:
            raise HTTPException(400, "No albedo map found for this job.")
        tempdir.write_job_state(job_id, state)   # successive slider moves compose
        out = _convert_and_preview(pbr, output_dir, opts)
        tempdir.touch_job(job_id)
        return out

    return await _run_conversion(
        job_id, work, source=source, preview_rev=int(time.time() * 1000),
    )


@router.post("/sources/{source}/convert", response_model=ConvertResponse, dependencies=[Depends(rate_limit)])
async def source_convert(source: str, req: SourceConvertRequest):
    """Fetch an asset from a texture source (Poly Haven, ambientCG) and convert it."""
    return await _convert_from_source(
        source, req.asset_id, req.resolution, req.fmt, req.options,
    )


@router.post("/convert/polyhaven", response_model=ConvertResponse, dependencies=[Depends(rate_limit)])
async def convert_polyhaven(req: PolyHavenConvertRequest):
    """Fetch a Poly Haven material and convert to Radiance (legacy alias)."""
    return await _convert_from_source(
        "polyhaven", req.slug, req.resolution, req.fmt, req.options,
    )


@router.get("/download/{job_id}", dependencies=[Depends(rate_limit_light)])
async def download(job_id: str):
    """Download the converted material as a zip file."""
    out_dir = tempdir.get_output_dir(job_id)
    if out_dir is None:
        raise HTTPException(404, "Job not found or expired")

    buf = await run_in_threadpool(_zip_directory, out_dir)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=pbr2rad_{job_id}.zip"},
    )


@router.get("/preview/{job_id}", dependencies=[Depends(rate_limit_light)])
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
# Texture-source catalogs (Poly Haven, ambientCG) — proxied to dodge CORS and
# so the whole listing is fetched once per TTL, not once per visitor
# ---------------------------------------------------------------------------

# source → {"data": list[entry] | None, "fetched_at": float, "index": dict}
# Entries share one shape across sources (see fetch.fetch_catalog):
#   {"id", "name", "preview", "preview_dark", "preview_large",
#    "preview_large_dark", "tags", "maps", "dims_cm", "downloads"}
_CATALOG_CACHE: dict[str, dict] = {}
# One catalog build at a time per source — a cold ambientCG build is ~5
# paged requests, and a thundering herd of first searches must not fan out.
_CATALOG_LOCKS: dict[str, threading.Lock] = {k: threading.Lock() for k in SOURCES}


def _catalog_slot(source: str) -> dict:
    return _CATALOG_CACHE.setdefault(
        source, {"data": None, "fetched_at": 0.0, "index": None, "index_of": None}
    )


def _catalog_disk_path(source: str) -> Path:
    return _cache_root(source) / get_source(source).catalog_file


def _load_catalog(source: str) -> list[dict]:
    """Two-tier cache for a source's catalog. Blocking.

    L1: in-process list (zero I/O on hot path).
    L2: JSON file on disk (survives process restarts; shared across workers).
    Miss both: fetch upstream (paged for ambientCG), atomically write L2,
    populate L1. If upstream fails, serve whatever stale copy exists rather
    than surfacing "Search failed" — both APIs are community-run.
    """
    src = get_source(source)
    ttl = src.catalog_ttl
    slot = _catalog_slot(source)

    now = time.time()
    if slot["data"] is not None and now - slot["fetched_at"] < ttl:
        return slot["data"]  # type: ignore[return-value]

    lock = _CATALOG_LOCKS.setdefault(source, threading.Lock())
    with lock:
        # Another thread may have filled the cache while we waited.
        now = time.time()
        if slot["data"] is not None and now - slot["fetched_at"] < ttl:
            return slot["data"]  # type: ignore[return-value]

        # L2: read regardless of age so we can fall back to a stale copy.
        disk_path = _catalog_disk_path(source)
        disk_data = None
        disk_mtime = 0.0
        if disk_path.exists():
            try:
                disk_mtime = disk_path.stat().st_mtime
                loaded = json.loads(disk_path.read_text())
                # Pre-v2 files held Poly Haven's raw slug→dict response —
                # treat anything that isn't the normalized list as a miss.
                disk_data = loaded if isinstance(loaded, list) else None
            except (OSError, ValueError):
                disk_data = None

        if disk_data is not None and now - disk_mtime < ttl:
            _set_catalog(slot, disk_data, disk_mtime)
            return disk_data

        try:
            data = get_catalog_fetcher(source)()
        except Exception:
            stale = disk_data if disk_data is not None else slot["data"]
            if stale is not None:
                # Serve the stale copy and mark it fresh for a full TTL so we
                # don't re-hit a down upstream on every request.
                log.warning("%s catalog refresh failed; serving stale copy", source, exc_info=True)
                _set_catalog(slot, stale, now)
                return stale  # type: ignore[return-value]
            raise  # nothing cached anywhere — let the caller surface a 502

        # Atomic write to disk (tempfile + os.replace — survives concurrent writes)
        try:
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", dir=str(disk_path.parent),
                prefix=".catalog_", suffix=".tmp", delete=False,
            ) as tmp:
                json.dump(data, tmp)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, disk_path)
            # Retire catalog files from older schema versions (tiny, but
            # pointless to keep; they are never read again).
            for old in disk_path.parent.glob("catalog*.json"):
                if old != disk_path:
                    old.unlink(missing_ok=True)
        except OSError:
            pass  # disk cache is best-effort

        _set_catalog(slot, data, now)
        return data


def _set_catalog(slot: dict, data: list[dict], fetched_at: float) -> None:
    slot["data"] = data
    slot["fetched_at"] = fetched_at
    slot["index"] = None
    slot["index_of"] = None


def _catalog_index(source: str) -> dict[str, dict]:
    """Lower-cased id → entry for the source's current catalog. Blocking."""
    data = _load_catalog(source)
    slot = _catalog_slot(source)
    if slot["index"] is None or slot["index_of"] is not data:
        slot["index"] = {
            str(e.get("id", "")).lower(): e for e in data if isinstance(e, dict)
        }
        slot["index_of"] = data
    return slot["index"]


def _search_catalog(catalog: list[dict], q: str, *, limit: int = 30) -> list[dict]:
    """Token-AND substring search over id, name and tags.

    Names on ambientCG are generic ("Bricks 104"); the tags carry the meaning,
    so "red brick" should find it. Empty query → the first ``limit`` entries
    (catalogs are popular-first).
    """
    tokens = [t for t in q.lower().split() if t]
    results: list[dict] = []
    for e in catalog:
        if not isinstance(e, dict):
            continue
        if tokens:
            hay = " ".join((
                str(e.get("id") or ""),
                str(e.get("name") or ""),
                " ".join(str(t) for t in (e.get("tags") or [])),
            )).lower()
            if not all(t in hay for t in tokens):
                continue
        results.append({
            "id": e.get("id"),
            "name": e.get("name") or e.get("id"),
            "preview": e.get("preview"),
            # Dark-background variant when the source publishes one; the UI
            # picks by theme and falls back to ``preview``. The large pair
            # feeds the selected-asset hero the instant a tile is clicked.
            "preview_dark": e.get("preview_dark"),
            "preview_large": e.get("preview_large") or e.get("preview"),
            "preview_large_dark": e.get("preview_large_dark") or e.get("preview_dark"),
        })
        if len(results) >= limit:
            break
    return results


@router.get("/sources", dependencies=[Depends(rate_limit_light)])
async def list_sources():
    """The texture sources this deployment can fetch from."""
    return [
        {
            "key": s.key,
            "display_name": s.display_name,
            "home_url": s.home_url,
            "license": s.license,
            "formats": list(s.formats),
            "default_fmt": s.web_default_fmt,
            "id_hint": s.id_hint,
        }
        for s in SOURCES.values()
    ]


@router.get("/sources/{source}/search", dependencies=[Depends(rate_limit_light)])
async def source_search(source: str, q: str = ""):
    """Search a source's catalog (cached per source TTL)."""
    src = _source_or_404(source)
    try:
        catalog = await run_in_threadpool(_load_catalog, source)
    except Exception as exc:
        raise HTTPException(502, f"{src.display_name} API error: {exc}")
    return _search_catalog(catalog, q)


@router.get("/polyhaven/search", dependencies=[Depends(rate_limit_light)])
async def polyhaven_search(q: str = ""):
    """Search Poly Haven textures (legacy alias; also returns ``slug``)."""
    results = await source_search("polyhaven", q)
    return [{"slug": r["id"], **r} for r in results]


# ---------------------------------------------------------------------------
# Per-asset info (resolutions, map list / thumbnails) — cached per source
# ---------------------------------------------------------------------------

# Every thumbnail click used to cost two uncached upstream round-trips (up to
# 60s). Keyed "source:id". Same TTL policy as the catalogs.
# key → (expires_at, payload)
_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_INFO_TTL_SECONDS = 3600
# Payloads whose map thumbnails were skipped/failed (throttled, transient
# error) are kept only briefly so the next click retries soon — but not on
# every click, which for a live-lookup asset would mean an API call each.
_INFO_RETRY_TTL_SECONDS = 120
# ambientCG payloads carry inline map thumbnails (~40-80 KB each) → ≤ ~20 MB.
_INFO_CACHE_MAX = 256
_INFO_CACHE_LOCK = threading.Lock()


def _info_cache_get(key: str) -> dict | None:
    with _INFO_CACHE_LOCK:
        hit = _INFO_CACHE.get(key)
    if hit is not None and time.time() < hit[0]:
        return hit[1]
    return None


def _info_cache_put(key: str, payload: dict, *, ttl: float = _INFO_TTL_SECONDS) -> None:
    now = time.time()
    with _INFO_CACHE_LOCK:
        if len(_INFO_CACHE) >= _INFO_CACHE_MAX:
            # Drop expired entries first, then the soonest-to-expire.
            for k in [k for k, (exp, _p) in _INFO_CACHE.items() if now >= exp]:
                _INFO_CACHE.pop(k, None)
        if len(_INFO_CACHE) >= _INFO_CACHE_MAX:
            oldest = min(_INFO_CACHE, key=lambda k: _INFO_CACHE[k][0])
            _INFO_CACHE.pop(oldest, None)
        _INFO_CACHE[key] = (now + ttl, payload)


def _polyhaven_info_data(slug: str) -> dict:
    """Fetch (or serve cached) Poly Haven asset info + file listing. Blocking."""
    from ..fetch import _THUMB_URL, fetch_asset_files, fetch_asset_info

    key = f"polyhaven:{slug}"
    cached = _info_cache_get(key)
    if cached is not None:
        return cached

    info = fetch_asset_info(slug)
    files = fetch_asset_files(slug)
    payload = _build_info_payload(slug, info, files)
    src = get_source("polyhaven")
    payload.update({
        "source": "polyhaven",
        "id": slug,
        "formats": list(src.formats),
        "default_fmt": src.web_default_fmt,
        "preview_url": _THUMB_URL.format(slug=slug, width=200),
        "preview_large_url": _THUMB_URL.format(slug=slug, width=512),
        "preview_large_dark_url": None,
        "asset_url": src.asset_url(slug),
        "dimensions_cm": None,
    })
    _info_cache_put(key, payload)
    return payload


def _resolutions_from_downloads(downloads: dict | None) -> list[str]:
    res = set()
    for attr in downloads or {}:
        r = acg._ATTR_RES.get(str(attr)[:2])
        if r:
            res.add(r)
    return sorted(res)


def _formats_from_downloads(downloads: dict | None) -> list[str]:
    fmts = set()
    for attr in downloads or {}:
        tail = str(attr).rsplit("-", 1)[-1].lower()
        if tail in ("png", "jpg"):
            fmts.add(tail)
    return sorted(fmts)


def _data_uri(path: Path, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _ambientcg_map_thumbnails(asset_id: str, entry: dict | None = None) -> tuple[dict[str, str], bool]:
    """Per-map thumbnails as data URIs plus an "is this final" flag.

    Generated from the cached 1K-JPG pack (downloaded on first request,
    throttled); failures or a throttled/skipped download degrade to
    label-only tiles rather than failing ``/info``. The flag is False in
    those cases so the payload isn't cached for an hour — the next request
    gets another chance once the pack is there / the budget refills.
    """
    try:
        thumbs = acg.map_thumbnails(asset_id, entry=entry)
    except Exception:
        log.warning("ambientCG map thumbnails unavailable for %s", asset_id, exc_info=True)
        return {}, False
    out: dict[str, str] = {}
    for channel, path in thumbs.items():
        try:
            out[channel] = _data_uri(path)
        except OSError:
            continue
    return out, bool(out)


def _build_source_info(source: str, entry: dict) -> dict:
    """Info payload for a catalog-backed source (currently ambientCG).

    ambientCG publishes no per-map images, so the tiles are thumbnails we
    generate from the pack itself (``_ambientcg_map_thumbnails``); any map
    we can't picture still gets a label-only tile (``thumbnail_url: None``).
    """
    src = get_source(source)
    downloads = entry.get("downloads") or None
    resolutions = _resolutions_from_downloads(downloads) if downloads else ["1k", "2k"]
    formats = _formats_from_downloads(downloads) if downloads else list(src.formats)
    default_fmt = src.web_default_fmt if src.web_default_fmt in formats else (
        formats[0] if formats else src.web_default_fmt
    )
    asset_id = str(entry.get("id") or "")

    thumbs: dict[str, str] = {}
    thumbs_final = True
    if source == "ambientcg" and asset_id:
        thumbs, thumbs_final = _ambientcg_map_thumbnails(asset_id, entry)
    listed: list[str] = []
    for m in entry.get("maps") or []:
        internal = acg.ACG_TO_INTERNAL.get(str(m))
        if internal and internal not in listed:
            listed.append(internal)
    channels = [c for c in _MAP_ORDER if c in thumbs or c in listed]
    # Anything the API lists under a name we don't order still shows up.
    channels += [c for c in listed if c not in channels]
    maps = [{"channel": c, "thumbnail_url": thumbs.get(c)} for c in channels]

    return {
        "source": source,
        "id": asset_id,
        "name": entry.get("name") or asset_id,
        "categories": list(entry.get("tags") or []),
        "resolutions": resolutions,
        "formats": formats,
        "default_fmt": default_fmt,
        "maps": maps,
        "preview_url": entry.get("preview"),
        "preview_large_url": entry.get("preview_large") or entry.get("preview"),
        "preview_large_dark_url": entry.get("preview_large_dark") or entry.get("preview_dark"),
        "asset_url": src.asset_url(asset_id),
        "dimensions_cm": entry.get("dims_cm"),
        # Internal: False when thumbnails were skipped/failed → don't cache.
        "_cacheable": thumbs_final,
    }


def _source_info(source: str, asset_id: str) -> dict:
    """Info payload for one asset of any registered source. Blocking.

    Raises ``FetchError`` when the asset is unknown.
    """
    if source == "polyhaven":
        return _polyhaven_info_data(asset_id)

    key = f"{source}:{asset_id.lower()}"
    cached = _info_cache_get(key)
    if cached is not None:
        return cached

    entry = None
    try:
        entry = _catalog_index(source).get(asset_id.lower())
    except Exception:
        # Catalog unavailable (upstream down, nothing cached) — the live
        # per-asset lookup below can still answer for a known id.
        log.warning("%s catalog unavailable for info lookup", source, exc_info=True)
    if entry is None:
        if source != "ambientcg":
            raise FetchError(f"asset not found: {asset_id!r}")
        # Not in the cached catalog (brand-new asset, or catalog stale).
        entry = acg.normalize_asset(acg.fetch_asset(asset_id))

    payload = _build_source_info(source, entry)
    final = payload.pop("_cacheable", True)
    _info_cache_put(key, payload, ttl=_INFO_TTL_SECONDS if final else _INFO_RETRY_TTL_SECONDS)
    return payload


@router.get("/sources/{source}/{asset_id}/info", dependencies=[Depends(rate_limit_light)])
async def source_info(source: str, asset_id: str):
    """Asset info: name, available resolutions/formats, map list (cached)."""
    _source_or_404(source)
    try:
        return await run_in_threadpool(_source_info, source, asset_id)
    except FetchError as exc:
        raise HTTPException(404, str(exc))


@router.get("/polyhaven/{slug}/info", dependencies=[Depends(rate_limit_light)])
async def polyhaven_info(slug: str):
    """Get Poly Haven asset info and available resolutions (legacy alias)."""
    return await source_info("polyhaven", slug)


def _build_info_payload(slug: str, info: dict, files: dict) -> dict:
    # Extract available resolutions
    # (capped at 2k — the tool never fetches anything larger)
    resolutions = set()
    for channel_data in files.values():
        if isinstance(channel_data, dict):
            resolutions.update(channel_data.keys())
    resolutions &= {"1k", "2k"}

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
    PREVIEW_RES_ORDER = ("1k", "2k")
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
