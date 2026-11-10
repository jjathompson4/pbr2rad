"""Tests for the pbr2rad web API."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from pbr2rad.web.app import create_app
from pbr2rad.web.preview import PREVIEW_OK, PreviewResult

from test_pvw import parse_pvw as _pvw_fields

try:
    from fastapi.testclient import TestClient
except ImportError:
    pytest.skip("web dependencies not installed", allow_module_level=True)


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def albedo_png(tmp_path) -> Path:
    """Create a small synthetic albedo PNG."""
    p = tmp_path / "test_diff_2k.png"
    Image.new("RGB", (16, 16), (180, 140, 100)).save(p)
    return p


@pytest.fixture
def rough_png(tmp_path) -> Path:
    """Create a small synthetic roughness PNG."""
    p = tmp_path / "test_rough_2k.png"
    Image.new("L", (16, 16), 128).save(p)
    return p


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "radiance_available" in data


# ---------------------------------------------------------------------------
# Canonical-host redirect (*.fly.dev bypasses Cloudflare)
# ---------------------------------------------------------------------------

def test_fly_dev_host_redirects_to_canonical(client):
    resp = client.get(
        "/some/path?q=1",
        headers={"host": "pbr2rad.fly.dev"},
        follow_redirects=False,
    )
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://pbr2rad.com/some/path?q=1"


def test_fly_dev_host_health_check_still_served(client):
    resp = client.get(
        "/api/v1/health",
        headers={"host": "pbr2rad.fly.dev"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_canonical_host_not_redirected(client):
    resp = client.get(
        "/api/v1/health",
        headers={"host": "pbr2rad.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------

def test_discover_auto_detects(client, albedo_png, rough_png):
    resp = client.post(
        "/api/v1/discover",
        files=[
            ("files", (albedo_png.name, open(albedo_png, "rb"), "image/png")),
            ("files", (rough_png.name, open(rough_png, "rb"), "image/png")),
        ],
    )
    assert resp.status_code == 200
    data = resp.json()
    channels = {ch["filename"]: ch["channel"] for ch in data["channels"]}
    assert channels[albedo_png.name] == "albedo"
    assert channels[rough_png.name] == "roughness"


# ---------------------------------------------------------------------------
# Convert (upload)
# ---------------------------------------------------------------------------

def test_convert_upload_with_labels(client, albedo_png):
    resp = client.post(
        "/api/v1/convert/upload",
        files=[("files", (albedo_png.name, open(albedo_png, "rb"), "image/png"))],
        data={
            "options": json.dumps({"projection": "box"}),
            "channels": json.dumps([{"filename": albedo_png.name, "channel": "albedo"}]),
            "name": "test_mat",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test_mat"
    assert data["primitive"] == "plastic"
    assert data["download_url"].startswith("/api/v1/download/")
    assert "job_id" in data


def test_convert_upload_auto_discover(client, albedo_png):
    resp = client.post(
        "/api/v1/convert/upload",
        files=[("files", (albedo_png.name, open(albedo_png, "rb"), "image/png"))],
        data={"options": "{}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["primitive"] == "plastic"


def test_convert_upload_no_albedo(client, rough_png):
    resp = client.post(
        "/api/v1/convert/upload",
        files=[("files", (rough_png.name, open(rough_png, "rb"), "image/png"))],
        data={
            "channels": json.dumps([{"filename": rough_png.name, "channel": "roughness"}]),
        },
    )
    assert resp.status_code == 400
    assert "albedo" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def test_download_after_convert(client, albedo_png):
    # Convert first
    resp = client.post(
        "/api/v1/convert/upload",
        files=[("files", (albedo_png.name, open(albedo_png, "rb"), "image/png"))],
        data={
            "channels": json.dumps([{"filename": albedo_png.name, "channel": "albedo"}]),
            "name": "dl_test",
        },
    )
    job_id = resp.json()["job_id"]

    # Download
    dl_resp = client.get(f"/api/v1/download/{job_id}")
    assert dl_resp.status_code == 200
    assert dl_resp.headers["content-type"] == "application/zip"
    assert len(dl_resp.content) > 100  # non-empty zip


def test_download_zip_includes_pvw(client, albedo_png):
    """The ClimateStudio preview file must survive into the delivered zip."""
    resp = client.post(
        "/api/v1/convert/upload",
        files=[("files", (albedo_png.name, open(albedo_png, "rb"), "image/png"))],
        data={
            "channels": json.dumps([{"filename": albedo_png.name, "channel": "albedo"}]),
            "name": "pvw_zip_test",
        },
    )
    job_id = resp.json()["job_id"]

    dl_resp = client.get(f"/api/v1/download/{job_id}")
    assert dl_resp.status_code == 200

    with zipfile.ZipFile(BytesIO(dl_resp.content)) as zf:
        names = zf.namelist()
    rads = [n for n in names if n.endswith(".rad")]
    assert rads, names
    for rad in rads:
        assert rad[:-4] + ".pvw" in names, names


def test_download_zip_excludes_preview_intermediates(tmp_path):
    """Every artifact render_preview() leaves behind is stripped but the PNG."""
    from pbr2rad.web.api import _zip_directory

    out_dir = tmp_path / "job"
    mat = out_dir / "mymat"
    mat.mkdir(parents=True)

    # The material itself.
    for keep in ("mymat.rad", "mymat.pvw", "mymat.hdr", "mymat.cal"):
        (mat / keep).write_bytes(b"keep")

    # Everything render_preview() writes into the same directory, plus the
    # reference-wrap preview chain convert_set writes for the web.
    for junk in (
        "preview.scene.rad",
        "preview.rig.cal",
        "preview.oct",
        "preview.hdr",
        "preview_filt.hdr",
        "preview.bmp",
        "preview_mymat.rad",
        "preview_mymat.cal",
        "preview_mymat_normal.cal",
    ):
        (mat / junk).write_bytes(b"junk")
    Image.new("RGB", (8, 8), (255, 0, 255)).save(mat / "preview.png")

    with zipfile.ZipFile(_zip_directory(out_dir)) as zf:
        names = [Path(n).name for n in zf.namelist()]

    assert "preview.hdr" not in names, names
    assert not [n for n in names if n.startswith("preview.") and n != "preview.png"], names
    assert not [n for n in names if n.startswith("preview_")], names
    # The material files -- including its real .hdr albedo -- survive.
    assert {"mymat.rad", "mymat.pvw", "mymat.hdr", "mymat.cal"} <= set(names), names
    # The finished thumbnail is a deliverable and stays.
    assert "preview.png" in names, names


def test_pvw_is_refreshed_from_the_render(tmp_path, albedo_png):
    """With a renderer available, the .pvw carries the render, not the swatch."""
    from pbr2rad.convert import ConvertOptions
    from pbr2rad.discover import discover
    from pbr2rad.web import api

    src = tmp_path / "src" / "mat"
    src.mkdir(parents=True)
    Image.new("RGB", (16, 16), (180, 140, 100)).save(src / "mat_diff.png")

    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        Image.new("RGB", (384, 384), (255, 0, 255)).save(output_png)
        return PreviewResult(PREVIEW_OK)

    out = tmp_path / "out"
    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        result, preview = api._convert_and_preview(
            discover(src), out, ConvertOptions(),
        )

    assert preview.rendered
    png = _pvw_fields(result.pvw_file.read_bytes())[3]
    with Image.open(BytesIO(png)) as img:
        assert img.size == (256, 256)
        assert img.getpixel((128, 128)) == (255, 0, 255)  # the render, not the albedo


def test_convert_and_preview_renders_the_reference_wrap_variant(tmp_path):
    """With write_preview_variant the sphere renders preview_<name>.rad (the
    reference wrap); without it, the exported chain as before."""
    from pbr2rad.convert import ConvertOptions
    from pbr2rad.discover import discover
    from pbr2rad.web import api

    src = tmp_path / "src" / "mat"
    src.mkdir(parents=True)
    Image.new("RGB", (16, 16), (180, 140, 100)).save(src / "mat_diff.png")
    seen = []

    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        seen.append(Path(rad_file).name)
        Image.new("RGB", (384, 384), (1, 2, 3)).save(output_png)
        return PreviewResult(PREVIEW_OK)

    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        api._convert_and_preview(discover(src), tmp_path / "a", ConvertOptions(write_preview_variant=True))
        api._convert_and_preview(discover(src), tmp_path / "b", ConvertOptions())
    assert seen == ["preview_mat.rad", "mat.rad"]


@pytest.mark.parametrize("status,has_url", [
    ("ok", True), ("draft", True), ("timeout", False), ("error", False),
])
def test_rerender_reports_preview_status(client, albedo_png, status, has_url):
    """A re-render whose preview runs out of time (or fails) says so instead of
    silently returning no preview_url; the material itself is still updated."""
    from pbr2rad.web import api

    resp = client.post(
        "/api/v1/convert/upload",
        files=[("files", (albedo_png.name, open(albedo_png, "rb"), "image/png"))],
        data={"options": "{}", "name": "m"},
    )
    assert resp.status_code == 200
    job = resp.json()["job_id"]

    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        if status in ("ok", "draft"):
            Image.new("RGB", (384, 384), (9, 9, 9)).save(output_png)
            return PreviewResult(status)
        return PreviewResult(status, f"The preview render {status}.")

    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        resp = client.post(f"/api/v1/jobs/{job}/rerender", json={"diffuse_scale": 0.8})
    assert resp.status_code == 200
    data = resp.json()
    assert data["preview_status"] == status
    assert data["diffuse_scale"] == pytest.approx(0.8)
    if has_url:
        assert data["preview_url"].startswith(f"/api/v1/preview/{job}?r=")
        assert data["preview_error"] is None
    else:
        assert data["preview_url"] is None
        assert data["preview_error"]


# ---------------------------------------------------------------------------
# Progressive previews: draft with the convert / re-render, then refine
# ---------------------------------------------------------------------------

def _upload(client, albedo_png, **data):
    return client.post(
        "/api/v1/convert/upload",
        files=[("files", (albedo_png.name, open(albedo_png, "rb"), "image/png"))],
        data={"options": "{}", "name": "m", **data},
    )


def _refine(client, job, *, polls=20):
    """Start a refine and long-poll it until it settles (as the web app does)."""
    resp = client.post(f"/api/v1/jobs/{job}/refine")
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["preview_status"] == "running" and data["refine_id"]
    for _ in range(polls):
        if data["preview_status"] != "running":
            return data
        resp = client.get(f"/api/v1/jobs/{job}/refine", params={"refine_id": data["refine_id"], "wait": 10})
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "no-store"
        data = resp.json()
    raise AssertionError(f"refine still running: {data}")


def test_progressive_draft_then_refine(client, albedo_png):
    import threading

    from pbr2rad.web import api
    from pbr2rad.web import preview as pv

    seen = []

    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        seen.append(kwargs)
        Image.new("RGB", (384, 384), (9, 9, 9)).save(output_png)
        return PreviewResult(pv.PREVIEW_DRAFT if kwargs.get("quality") == "draft" else PREVIEW_OK)

    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        resp = _upload(client, albedo_png, preview_quality="draft")
        assert resp.status_code == 200
        data = resp.json()
        job = data["job_id"]
        assert data["preview_status"] == "draft" and data["preview_url"]
        assert seen[-1]["quality"] == "draft"

        ref = _refine(client, job)
        assert ref["preview_status"] == "ok" and ref["preview_error"] is None
        assert ref["preview_url"].startswith(f"/api/v1/preview/{job}?r=")
        kw = seen[-1]
        assert kw["quality"] == "full" and kw["draft_fallback"] is False
        assert kw["keep_on_failure"] is True and isinstance(kw["cancel"], threading.Event)
        assert kw["full_timeout"] == pv.REFINE_TIMEOUT_S

        resp = client.post(f"/api/v1/jobs/{job}/rerender", json={"diffuse_scale": 0.9, "preview_quality": "draft"})
        assert resp.json()["preview_status"] == "draft" and seen[-1]["quality"] == "draft"

        # Old clients send no preview_quality: the full render, as before.
        resp = client.post(f"/api/v1/jobs/{job}/rerender", json={"diffuse_scale": 0.8})
        assert resp.json()["preview_status"] == "ok" and seen[-1]["quality"] == "full"


def test_refine_without_a_picture_keeps_the_draft(client, albedo_png):
    from pbr2rad.web import api

    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        if kwargs.get("quality") == "draft":
            Image.new("RGB", (384, 384), (9, 9, 9)).save(output_png)
            return PreviewResult("draft")
        return PreviewResult("timeout", "The preview render ran out of time.")

    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        job = _upload(client, albedo_png, preview_quality="draft").json()["job_id"]
        ref = _refine(client, job)
    assert ref["preview_status"] == "timeout" and ref["preview_url"] is None and ref["preview_error"]
    assert client.get(f"/api/v1/preview/{job}").status_code == 200        # the draft is still served


def test_refine_unknown_job(client):
    assert client.post("/api/v1/jobs/nonexistent/refine").status_code == 404
    assert client.get("/api/v1/jobs/nonexistent/refine").status_code == 404


def _draft_then_blocked_refine(api, gate, cancelled_seen=None):
    """A fake render: drafts at once; the full render waits for `gate`, or
    gives way when its cancel event is set."""
    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        if kwargs.get("quality") == "draft":
            Image.new("RGB", (384, 384), (9, 9, 9)).save(output_png)
            return PreviewResult("draft")
        cancel = kwargs["cancel"]
        while not gate.is_set():
            if cancel.wait(0.02):
                if cancelled_seen is not None:
                    cancelled_seen.set()
                return PreviewResult("cancelled", "The preview render gave way to a newer request.")
        Image.new("RGB", (384, 384), (7, 7, 7)).save(output_png)
        return PreviewResult(PREVIEW_OK)
    return fake_render


def test_refine_runs_in_the_background(client, albedo_png):
    """POST answers at once; a poll says "running" until the render settles,
    and a poll with wait answers as soon as it does."""
    import threading

    from pbr2rad.web import api

    gate = threading.Event()
    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=_draft_then_blocked_refine(api, gate)):
        job = _upload(client, albedo_png, preview_quality="draft").json()["job_id"]
        resp = client.post(f"/api/v1/jobs/{job}/refine")
        assert resp.status_code == 202
        rid = resp.json()["refine_id"]
        poll = client.get(f"/api/v1/jobs/{job}/refine", params={"refine_id": rid}).json()
        assert poll["preview_status"] == "running" and poll["preview_url"] is None
        # A second refine meanwhile: the one-job lock is held, so 503.
        assert client.post(f"/api/v1/jobs/{job}/refine").status_code == 503
        threading.Timer(0.3, gate.set).start()
        poll = client.get(f"/api/v1/jobs/{job}/refine", params={"refine_id": rid, "wait": 10}).json()
        assert poll["preview_status"] == "ok" and poll["preview_url"].startswith(f"/api/v1/preview/{job}?r=")
        assert poll["refine_id"] == rid
        # The lock is free again: the next refine starts, and an outdated id is superseded.
        new = _refine(client, job)
        assert new["preview_status"] == "ok" and new["refine_id"] != rid
        stale = client.get(f"/api/v1/jobs/{job}/refine", params={"refine_id": rid}).json()
        assert stale["preview_status"] == "cancelled"


def test_rerender_cancels_a_running_refine(client, albedo_png):
    """A slider move while the full render runs: the refine gives way at once,
    the re-render gets the lock (no 503), and the refine settles "cancelled"."""
    import threading

    from pbr2rad.web import api

    gate, cancelled = threading.Event(), threading.Event()
    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=_draft_then_blocked_refine(api, gate, cancelled)):
        job = _upload(client, albedo_png, preview_quality="draft").json()["job_id"]
        rid = client.post(f"/api/v1/jobs/{job}/refine").json()["refine_id"]
        resp = client.post(f"/api/v1/jobs/{job}/rerender", json={"diffuse_scale": 0.9, "preview_quality": "draft"})
        assert resp.status_code == 200 and resp.json()["preview_status"] == "draft"
        assert cancelled.is_set()
        poll = client.get(f"/api/v1/jobs/{job}/refine", params={"refine_id": rid, "wait": 5}).json()
        assert poll["preview_status"] == "cancelled" and poll["preview_url"] is None
    gate.set()


def test_refine_that_crashes_reports_error_and_frees_the_server(client, albedo_png):
    from pbr2rad.web import api

    def fake_render(mat_dir, rad_file, output_png, **kwargs):
        if kwargs.get("quality") == "draft":
            Image.new("RGB", (384, 384), (9, 9, 9)).save(output_png)
            return PreviewResult("draft")
        raise OSError("disk full")

    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        job = _upload(client, albedo_png, preview_quality="draft").json()["job_id"]
        ref = _refine(client, job)
        assert ref["preview_status"] == "error" and ref["preview_error"]
        assert api._CONVERT_LOCK.acquire(blocking=False)   # released by the refine thread
        api._CONVERT_LOCK.release()
        assert api._refine_cancel is None


def test_upload_rejects_unknown_preview_quality(client, albedo_png):
    assert _upload(client, albedo_png, preview_quality="ultra").status_code == 400


def test_new_job_preempts_a_refine_but_not_a_conversion():
    """A convert / re-render that finds a refine holding the one-job lock
    cancels it and takes the lock; any other holder still means 503."""
    import asyncio
    import threading

    from fastapi import HTTPException

    from pbr2rad.web import api

    assert api._CONVERT_LOCK.acquire(blocking=False)
    cancel = threading.Event()
    api._refine_cancel = cancel

    def refine_gives_way():
        cancel.wait(5)
        api._refine_cancel = None
        api._CONVERT_LOCK.release()

    threading.Thread(target=refine_gives_way).start()
    try:
        asyncio.run(api._acquire_for_job())
        assert cancel.is_set()
    finally:
        api._CONVERT_LOCK.release()

    assert api._CONVERT_LOCK.acquire(blocking=False)       # a conversion holds it
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(api._acquire_for_job())
        assert exc.value.status_code == 503
    finally:
        api._CONVERT_LOCK.release()


@pytest.mark.skipif(not __import__("shutil").which("rpict"), reason="Radiance not installed")
def test_progressive_preview_with_real_radiance(client, albedo_png):
    data = _upload(client, albedo_png, preview_quality="draft").json()
    assert data["preview_status"] == "draft"
    job = data["job_id"]
    ref = _refine(client, job)
    assert ref["preview_status"] == "ok", ref
    assert client.get(ref["preview_url"]).status_code == 200
    names = zipfile.ZipFile(BytesIO(client.get(f"/api/v1/download/{job}").content)).namelist()
    assert any(n.endswith("/preview.png") for n in names)
    assert not [n for n in names if n.endswith(".tmp")]


def test_download_invalid_job(client):
    resp = client.get("/api/v1/download/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_missing_job(client):
    resp = client.get("/api/v1/preview/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Poly Haven proxy
# ---------------------------------------------------------------------------

def test_polyhaven_search(client):
    resp = client.get("/api/v1/polyhaven/search?q=wood")
    # This hits the real API — skip if offline
    if resp.status_code == 502:
        pytest.skip("Poly Haven API unreachable")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    if data:
        assert "slug" in data[0]
        assert "name" in data[0]


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "pbr2rad" in resp.text
    # The page must be revalidated on every load, or a cached copy can pair
    # with a newer /static/app.js after a deploy.
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers.get("etag")
