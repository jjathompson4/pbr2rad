"""Tests for the pbr2rad web API."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from pbr2rad.web.app import create_app

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
