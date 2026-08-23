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
        return True

    out = tmp_path / "out"
    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        result, has_preview = api._convert_and_preview(
            discover(src), out, ConvertOptions(),
        )

    assert has_preview
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
        return True

    with mock.patch.object(api, "radiance_available", return_value=True), \
            mock.patch.object(api, "render_preview", side_effect=fake_render):
        api._convert_and_preview(discover(src), tmp_path / "a", ConvertOptions(write_preview_variant=True))
        api._convert_and_preview(discover(src), tmp_path / "b", ConvertOptions())
    assert seen == ["preview_mat.rad", "mat.rad"]


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
