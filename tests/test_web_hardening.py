"""Tests for the public-hosting hardening: rate limits, upload limits,
path traversal, downscaling, and the mocked Poly Haven convert path."""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from pbr2rad.web import api as api_mod  # noqa: E402
from pbr2rad.web.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The limiters are module-global; isolate their counters per test."""
    from pbr2rad.web import limits

    limits.rate_limit.reset()
    limits.rate_limit_light.reset()
    yield
    limits.rate_limit.reset()
    limits.rate_limit_light.reset()


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def _png_bytes(size: int = 8, color=(120, 100, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _upload(name: str, data: bytes):
    return ("files", (name, data, "image/png"))


# ---------------------------------------------------------------------------
# Upload limits
# ---------------------------------------------------------------------------

class TestUploadLimits:
    def test_too_many_files_413(self, client):
        files = [_upload(f"map_{i}_diff.png", _png_bytes()) for i in range(api_mod.MAX_FILES + 1)]
        resp = client.post("/api/v1/discover", files=files)
        assert resp.status_code == 413
        assert "Too many files" in resp.json()["detail"]

    def test_disallowed_extension_415(self, client):
        resp = client.post(
            "/api/v1/discover",
            files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))],
        )
        assert resp.status_code == 415

    def test_filename_reduced_to_basename(self, client):
        # A path-traversal filename must not escape the upload dir; the
        # basename is still a valid png so discovery succeeds.
        resp = client.post(
            "/api/v1/discover",
            files=[("files", ("../../etc/x_diff.png", _png_bytes(), "image/png"))],
        )
        assert resp.status_code == 200

    def test_traversal_material_name_400(self, client):
        resp = client.post(
            "/api/v1/convert/upload",
            files=[_upload("wood_diff.png", _png_bytes())],
            data={"name": "../../escape", "options": "{}", "channels": ""},
        )
        assert resp.status_code == 400
        assert "Invalid material name" in resp.json()["detail"]

    def test_oversized_image_downscaled(self, client, monkeypatch):
        # 3000px longest edge must come back capped at MAX_RESOLUTION.
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        big = io.BytesIO()
        Image.new("RGB", (3000, 1500), (90, 90, 90)).save(big, format="PNG")
        resp = client.post(
            "/api/v1/convert/upload",
            files=[_upload("big_diff.png", big.getvalue())],
            data={
                "name": "big",
                "options": '{"estimate_maps": false}',
                "channels": '[{"filename": "big_diff.png", "channel": "albedo"}]',
            },
        )
        assert resp.status_code == 200
        w, h = resp.json()["resolution"]
        assert max(w, h) == api_mod.MAX_RESOLUTION
        assert (w, h) == (2048, 1024)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_light_limiter_trips_at_60(self, client, monkeypatch):
        monkeypatch.setattr(api_mod, "_polyhaven_catalog", lambda: {})
        codes = [
            client.get("/api/v1/polyhaven/search?q=x").status_code
            for _ in range(61)
        ]
        assert codes.count(200) == 60
        assert codes[-1] == 429

    def test_429_carries_retry_after(self, client, monkeypatch):
        monkeypatch.setattr(api_mod, "_polyhaven_catalog", lambda: {})
        resp = None
        for _ in range(61):
            resp = client.get("/api/v1/polyhaven/search?q=y")
            if resp.status_code == 429:
                break
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_health_is_never_limited(self, client):
        codes = {client.get("/api/v1/health").status_code for _ in range(80)}
        assert codes == {200}


# ---------------------------------------------------------------------------
# Poly Haven convert (mocked download) + busy semaphore
# ---------------------------------------------------------------------------

class TestPolyHavenConvert:
    def test_convert_polyhaven_mocked(self, client, monkeypatch, tmp_path):
        def fake_download(slug, dest, *, resolution, fmt, verbose=False):
            mat_dir = Path(dest) / slug
            mat_dir.mkdir(parents=True)
            (mat_dir / f"{slug}_diff_1k.png").write_bytes(_png_bytes(16))
            return mat_dir

        monkeypatch.setattr(api_mod, "download_texture_set", fake_download)
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        resp = client.post(
            "/api/v1/convert/polyhaven",
            json={"slug": "fake_brick", "resolution": "1k", "fmt": "png"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "fake_brick"
        assert body["download_url"].startswith("/api/v1/download/")
        # Estimated maps are reported separately from source channels.
        assert body["channels_used"] == ["albedo"]
        assert set(body["channels_estimated"]) == {"normal", "roughness"}

        # The download zip must not contain scratch intermediates.
        dl = client.get(body["download_url"])
        assert dl.status_code == 200
        import zipfile

        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        assert not any(".pbr2rad_work" in n or "_est_" in n for n in names)

    def test_resolution_above_2k_rejected(self, client):
        resp = client.post(
            "/api/v1/convert/polyhaven",
            json={"slug": "brick", "resolution": "4k", "fmt": "png"},
        )
        assert resp.status_code == 422  # pydantic literal validation

    def test_busy_semaphore_503(self, client, monkeypatch):
        assert api_mod._CONVERT_LOCK.acquire(blocking=False)
        try:
            resp = client.post(
                "/api/v1/convert/polyhaven",
                json={"slug": "brick", "resolution": "1k", "fmt": "png"},
            )
            assert resp.status_code == 503
            assert "Retry-After" in resp.headers
        finally:
            api_mod._CONVERT_LOCK.release()
