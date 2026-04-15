"""REST API router for pbr2rad."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .. import __version__
from ..convert import ConvertOptions, convert_set, write_manifest
from ..discover import PBRSet, discover
from ..fetch import FetchError, download_texture_set
from . import tempdir
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
    )


async def _save_uploads(
    files: list[UploadFile],
    upload_dir: Path,
) -> list[str]:
    """Save uploaded files to disk, return list of filenames."""
    filenames = []
    for f in files:
        dest = upload_dir / f.filename
        content = await f.read()
        dest.write_bytes(content)
        filenames.append(f.filename)
    return filenames


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        version=__version__,
        radiance_available=radiance_available(),
    )


@router.post("/discover", response_model=DiscoverResponse)
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


@router.post("/convert/upload", response_model=ConvertResponse)
async def convert_upload(
    files: list[UploadFile] = File(...),
    options: str = Form("{}"),
    channels: str = Form(""),
    name: str = Form(""),
):
    """Upload images and convert to Radiance material."""
    job_id, upload_dir, output_dir = tempdir.new_job()
    await _save_uploads(files, upload_dir)

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


@router.post("/convert/polyhaven", response_model=ConvertResponse)
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

@router.get("/polyhaven/search")
async def polyhaven_search(q: str = ""):
    """Search Poly Haven textures."""
    import urllib.request
    import json as _json

    url = "https://api.polyhaven.com/assets?type=textures"
    req = urllib.request.Request(url, headers={"User-Agent": "pbr2rad/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            all_assets = _json.loads(resp.read())
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
