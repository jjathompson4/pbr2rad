"""Tests for the public-hosting hardening: rate limits, upload limits,
path traversal, downscaling, and the mocked Poly Haven convert path."""

from __future__ import annotations

import io
import json
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
        monkeypatch.setattr(api_mod, "_load_catalog", lambda source: [])
        codes = [
            client.get("/api/v1/polyhaven/search?q=x").status_code
            for _ in range(61)
        ]
        assert codes.count(200) == 60
        assert codes[-1] == 429

    def test_429_carries_retry_after(self, client, monkeypatch):
        monkeypatch.setattr(api_mod, "_load_catalog", lambda source: [])
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

        # The route dispatches through sources.get_downloader(), which resolves
        # the module attribute at call time — patch it where it lives.
        monkeypatch.setattr("pbr2rad.fetch.download_texture_set", fake_download)
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        resp = client.post(
            "/api/v1/convert/polyhaven",
            json={"slug": "fake_brick", "resolution": "1k", "fmt": "png"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "fake_brick"
        assert body["download_url"].startswith("/api/v1/download/")
        assert body["source"] == "polyhaven"
        assert body["source_url"] == "https://polyhaven.com/a/fake_brick"
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


# ---------------------------------------------------------------------------
# Multi-source routes (/api/v1/sources/*) — ambientCG + Poly Haven aliases
# ---------------------------------------------------------------------------

_ACG_CATALOG = [
    {
        "id": "Bricks104", "name": "Bricks 104",
        "preview": "https://cdn.example/Bricks104.jpg",
        "tags": ["bricks", "red", "wall", "masonry"],
        "maps": ["color", "displacement", "normal", "roughness", "ambient-occlusion"],
        "dims_cm": None,
        "downloads": {"1K-JPG": 4252352, "1K-PNG": 9190179, "2K-JPG": 13008436, "2K-PNG": 33458789},
    },
    {
        "id": "WoodFloor051", "name": "Wood Floor 051",
        "preview": "https://cdn.example/WoodFloor051.jpg",
        "tags": ["wood", "floor", "planks", "brown"],
        "maps": ["color", "displacement", "normal", "roughness", "ambient-occlusion"],
        "dims_cm": [180.0, 180.0],
        "downloads": {"1K-JPG": 5000000, "2K-JPG": 16000000},   # no PNG packs
    },
    {
        "id": "Metal032", "name": "Metal 032",
        "preview": None,
        "tags": ["metal", "painted", "red"],
        "maps": ["color", "normal", "roughness", "metalness"],
        "dims_cm": None,
        "downloads": {"1K-JPG": 3000000, "1K-PNG": 7000000},
    },
]


@pytest.fixture()
def acg_catalog(monkeypatch):
    """Serve a canned ambientCG catalog; Poly Haven stays empty.

    Map thumbnails are stubbed out (no pack download) unless a test
    monkeypatches ``acg.map_thumbnails`` itself.
    """
    from pbr2rad import ambientcg as acg

    def fake_load(source):
        return list(_ACG_CATALOG) if source == "ambientcg" else []
    monkeypatch.setattr(api_mod, "_load_catalog", fake_load)
    monkeypatch.setattr(acg, "map_thumbnails", lambda asset_id, **kw: {})
    api_mod._INFO_CACHE.clear()
    yield
    api_mod._INFO_CACHE.clear()


class TestSources:
    def test_sources_list(self, client):
        resp = client.get("/api/v1/sources")
        assert resp.status_code == 200
        keys = {s["key"]: s for s in resp.json()}
        assert set(keys) == {"polyhaven", "ambientcg"}
        assert keys["ambientcg"]["default_fmt"] == "jpg"
        assert keys["ambientcg"]["formats"] == ["png", "jpg"]
        assert keys["ambientcg"]["license"] == "CC0-1.0"
        assert keys["polyhaven"]["formats"] == ["png", "jpg", "exr"]

    def test_unknown_source_404(self, client, acg_catalog):
        assert client.get("/api/v1/sources/texturehaven/search?q=x").status_code == 404
        assert client.get("/api/v1/sources/texturehaven/Bricks104/info").status_code == 404
        resp = client.post("/api/v1/sources/texturehaven/convert",
                           json={"asset_id": "Bricks104"})
        assert resp.status_code == 404

    def test_search_matches_tags_id_and_tokens(self, client, acg_catalog):
        ids = lambda r: [x["id"] for x in r.json()]  # noqa: E731
        # Tag hit
        r = client.get("/api/v1/sources/ambientcg/search?q=red")
        assert r.status_code == 200 and ids(r) == ["Bricks104", "Metal032"]
        # Id hit (case-insensitive)
        assert ids(client.get("/api/v1/sources/ambientcg/search?q=bricks104")) == ["Bricks104"]
        # Token-AND
        assert ids(client.get("/api/v1/sources/ambientcg/search?q=red brick")) == ["Bricks104"]
        assert ids(client.get("/api/v1/sources/ambientcg/search?q=red floor")) == []
        # Empty query → catalog order
        assert ids(client.get("/api/v1/sources/ambientcg/search")) == ["Bricks104", "WoodFloor051", "Metal032"]
        # Result shape
        first = client.get("/api/v1/sources/ambientcg/search?q=wood").json()[0]
        assert first == {"id": "WoodFloor051", "name": "Wood Floor 051",
                         "preview": "https://cdn.example/WoodFloor051.jpg",
                         "preview_dark": None,
                         # large falls back to the small preview when absent
                         "preview_large": "https://cdn.example/WoodFloor051.jpg",
                         "preview_large_dark": None}

    def test_polyhaven_alias_search_keeps_slug(self, client, monkeypatch):
        monkeypatch.setattr(
            api_mod, "_load_catalog",
            lambda source: [{"id": "wood_floor_03", "name": "Wood Floor 03",
                             "preview": "https://cdn/p.png", "tags": ["wood"],
                             "maps": None, "dims_cm": None, "downloads": None}],
        )
        r = client.get("/api/v1/polyhaven/search?q=wood")
        assert r.status_code == 200
        assert r.json()[0]["slug"] == "wood_floor_03"
        assert r.json()[0]["id"] == "wood_floor_03"

    def test_search_502_when_catalog_unavailable(self, client, monkeypatch):
        def boom(source):
            raise RuntimeError("upstream down")
        monkeypatch.setattr(api_mod, "_load_catalog", boom)
        r = client.get("/api/v1/sources/ambientcg/search?q=x")
        assert r.status_code == 502
        assert "ambientCG" in r.json()["detail"]

    def test_info_from_catalog(self, client, acg_catalog):
        r = client.get("/api/v1/sources/ambientcg/woodfloor051/info")   # case-insensitive
        assert r.status_code == 200
        info = r.json()
        assert info["source"] == "ambientcg"
        assert info["id"] == "WoodFloor051"
        assert info["name"] == "Wood Floor 051"
        assert info["resolutions"] == ["1k", "2k"]
        assert info["formats"] == ["jpg"]            # this one ships JPG packs only
        assert info["default_fmt"] == "jpg"
        assert info["dimensions_cm"] == [180.0, 180.0]
        assert info["asset_url"] == "https://ambientcg.com/a/WoodFloor051"
        assert info["preview_url"] == "https://cdn.example/WoodFloor051.jpg"
        assert info["categories"] == ["wood", "floor", "planks", "brown"]
        # Thumbnails stubbed out here → label-only tiles, in display order.
        channels = [m["channel"] for m in info["maps"]]
        assert channels == ["albedo", "normal_gl", "roughness", "ao", "displacement"]
        assert all(m["thumbnail_url"] is None for m in info["maps"])
        assert info["preview_large_url"] == "https://cdn.example/WoodFloor051.jpg"

    def test_info_inlines_map_thumbnails(self, client, acg_catalog, monkeypatch, tmp_path):
        """ambientCG has no per-map images; /info carries ours as data URIs."""
        from pbr2rad import ambientcg as acg

        jpg = tmp_path / "albedo.jpg"
        Image.new("RGB", (8, 8), (200, 50, 50)).save(jpg, format="JPEG")
        calls = []

        def fake_thumbs(asset_id, **kw):
            calls.append(asset_id)
            return {"albedo": jpg, "roughness": jpg}

        monkeypatch.setattr(acg, "map_thumbnails", fake_thumbs)
        info = client.get("/api/v1/sources/ambientcg/Bricks104/info").json()
        assert calls == ["Bricks104"]
        by_ch = {m["channel"]: m["thumbnail_url"] for m in info["maps"]}
        assert by_ch["albedo"].startswith("data:image/jpeg;base64,")
        assert by_ch["roughness"].startswith("data:image/jpeg;base64,")
        assert by_ch["normal_gl"] is None          # listed by the API, no picture
        assert list(by_ch) == ["albedo", "normal_gl", "roughness", "ao", "displacement"]
        # Cached: second call doesn't regenerate.
        client.get("/api/v1/sources/ambientcg/Bricks104/info")
        assert calls == ["Bricks104"]

    def test_info_survives_thumbnail_failure_and_retries_later(self, client, acg_catalog, monkeypatch, tmp_path):
        import time as _time
        from pbr2rad import ambientcg as acg

        def boom(asset_id, **kw):
            raise RuntimeError("upstream down")
        monkeypatch.setattr(acg, "map_thumbnails", boom)
        r = client.get("/api/v1/sources/ambientcg/Bricks104/info")
        assert r.status_code == 200
        assert all(m["thumbnail_url"] is None for m in r.json()["maps"])
        assert "_cacheable" not in r.json()

        # A payload without thumbnails is cached only for the short retry TTL,
        # not the full hour — so the next click after it lapses retries.
        key = "ambientcg:bricks104"
        expires, payload = api_mod._INFO_CACHE[key]
        assert expires - _time.time() <= api_mod._INFO_RETRY_TTL_SECONDS + 1
        assert expires - _time.time() < api_mod._INFO_TTL_SECONDS / 2

        # Simulate the retry TTL lapsing; thumbnails now work → served, and
        # the fresh entry gets the long TTL.
        api_mod._INFO_CACHE[key] = (0.0, payload)
        jpg = tmp_path / "a.jpg"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(jpg, format="JPEG")
        monkeypatch.setattr(acg, "map_thumbnails", lambda asset_id, **kw: {"albedo": jpg})
        r2 = client.get("/api/v1/sources/ambientcg/Bricks104/info")
        by_ch = {m["channel"]: m["thumbnail_url"] for m in r2.json()["maps"]}
        assert by_ch["albedo"].startswith("data:image/jpeg;base64,")
        expires2, _ = api_mod._INFO_CACHE[key]
        assert expires2 - _time.time() > api_mod._INFO_TTL_SECONDS / 2

    def test_info_only_1k_when_no_2k_pack(self, client, acg_catalog):
        info = client.get("/api/v1/sources/ambientcg/Metal032/info").json()
        assert info["resolutions"] == ["1k"]
        assert info["formats"] == ["jpg", "png"]
        assert "metalness" in [m["channel"] for m in info["maps"]]

    def test_info_falls_back_to_live_lookup(self, client, acg_catalog, monkeypatch):
        from pbr2rad import ambientcg as acg

        calls = []

        def fake_fetch_asset(asset_id):
            calls.append(asset_id)
            return {
                "id": "Ground109", "title": "Ground 109", "tags": ["dirt"],
                "maps": ["color", "normal", "roughness"],
                "thumbnails": {"256-JPG-FFFFFF": "https://cdn.example/g.jpg"},
                "downloads": [{"attributes": "1K-JPG", "extension": "zip",
                               "url": "https://ambientcg.com/get?file=Ground109_1K-JPG.zip",
                               "size": 100}],
            }

        monkeypatch.setattr(acg, "fetch_asset", fake_fetch_asset)
        monkeypatch.setattr(acg, "map_thumbnails", lambda asset_id, **kw: {})
        r = client.get("/api/v1/sources/ambientcg/ground109/info")
        assert r.status_code == 200
        assert r.json()["id"] == "Ground109"
        assert r.json()["resolutions"] == ["1k"]
        assert calls == ["ground109"]
        # Second hit is served from the info cache (short retry TTL since
        # thumbnails were empty, but still cached) — no live call.
        client.get("/api/v1/sources/ambientcg/ground109/info")
        assert calls == ["ground109"]

    def test_info_unknown_asset_404(self, client, acg_catalog, monkeypatch):
        from pbr2rad import ambientcg as acg
        from pbr2rad.fetch import FetchError

        def missing(asset_id):
            raise FetchError(f"asset not found on ambientCG: {asset_id!r}")

        monkeypatch.setattr(acg, "fetch_asset", missing)
        r = client.get("/api/v1/sources/ambientcg/Nope999/info")
        assert r.status_code == 404

    def test_convert_ambientcg_mocked(self, client, monkeypatch):
        calls = []

        def fake_download(asset_id, dest, *, resolution, fmt, verbose=False):
            calls.append((asset_id, resolution, fmt))
            mat_dir = Path(dest) / "Bricks104"      # canonical id, as the real client does
            mat_dir.mkdir(parents=True)
            (mat_dir / "Bricks104_1K-JPG_Color.png").write_bytes(_png_bytes(16))
            (mat_dir / "Bricks104_1K-JPG_Roughness.png").write_bytes(_png_bytes(16, (90, 90, 90)))
            return mat_dir

        monkeypatch.setattr("pbr2rad.ambientcg.download_texture_set", fake_download)
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        resp = client.post(
            "/api/v1/sources/ambientcg/convert",
            json={"asset_id": "bricks104", "resolution": "1k", "fmt": "jpg"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert calls == [("bricks104", "1k", "jpg")]
        assert body["name"] == "Bricks104"
        assert body["source"] == "ambientcg"
        assert body["source_url"] == "https://ambientcg.com/a/Bricks104"
        assert "albedo" in body["channels_used"] and "roughness" in body["channels_used"]

    def test_convert_exr_rejected_for_ambientcg(self, client):
        resp = client.post(
            "/api/v1/sources/ambientcg/convert",
            json={"asset_id": "Bricks104", "resolution": "1k", "fmt": "exr"},
        )
        assert resp.status_code == 400
        assert "does not offer" in resp.json()["detail"]

    def test_convert_fetch_error_400(self, client, monkeypatch):
        from pbr2rad.fetch import FetchError

        def failing(asset_id, dest, *, resolution, fmt, verbose=False):
            raise FetchError("asset not found on ambientCG: 'Nope999'")

        monkeypatch.setattr("pbr2rad.ambientcg.download_texture_set", failing)
        resp = client.post(
            "/api/v1/sources/ambientcg/convert",
            json={"asset_id": "Nope999", "resolution": "1k", "fmt": "jpg"},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"]

    def test_convert_resolution_above_2k_rejected(self, client):
        resp = client.post(
            "/api/v1/sources/ambientcg/convert",
            json={"asset_id": "Bricks104", "resolution": "4k", "fmt": "jpg"},
        )
        assert resp.status_code == 422


class TestCatalogCache:
    def test_stale_on_failure_and_disk_cache(self, tmp_path, monkeypatch):
        """First load fetches + writes disk; a later failed refresh serves stale."""
        monkeypatch.setenv("PBR2RAD_CACHE_DIR", str(tmp_path))
        api_mod._CATALOG_CACHE.clear()
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"id": "Bricks104", "name": "Bricks 104", "preview": None,
                         "tags": [], "maps": [], "dims_cm": None, "downloads": {}}]
            raise RuntimeError("upstream down")

        monkeypatch.setattr(api_mod, "get_catalog_fetcher", lambda source: fetcher)
        try:
            data = api_mod._load_catalog("ambientcg")
            assert [e["id"] for e in data] == ["Bricks104"]
            disk = tmp_path / "ambientcg" / "catalog.v3.json"
            assert disk.is_file()

            # Expire L1 and L2, force a refresh that fails → stale copy served.
            api_mod._CATALOG_CACHE["ambientcg"]["fetched_at"] = 0.0
            import os as _os
            _os.utime(disk, (0, 0))
            data2 = api_mod._load_catalog("ambientcg")
            assert [e["id"] for e in data2] == ["Bricks104"]
            assert calls["n"] == 2
            # Marked fresh again — the next call must not hit upstream.
            api_mod._load_catalog("ambientcg")
            assert calls["n"] == 2
        finally:
            api_mod._CATALOG_CACHE.clear()

    def test_old_disk_shape_treated_as_miss(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PBR2RAD_CACHE_DIR", str(tmp_path))
        api_mod._CATALOG_CACHE.clear()
        disk = tmp_path / "polyhaven" / "catalog.v3.json"
        disk.parent.mkdir(parents=True)
        disk.write_text('{"wood_floor_03": {"name": "Wood Floor 03"}}')  # old raw dict shape
        monkeypatch.setattr(
            api_mod, "get_catalog_fetcher",
            lambda source: (lambda: [{"id": "x", "name": "X", "preview": None, "tags": [],
                                      "maps": None, "dims_cm": None, "downloads": None}]),
        )
        try:
            data = api_mod._load_catalog("polyhaven")
            assert [e["id"] for e in data] == ["x"]
        finally:
            api_mod._CATALOG_CACHE.clear()


# ---------------------------------------------------------------------------
# Re-render an existing job with overrides (Output-panel sliders)
# ---------------------------------------------------------------------------

class TestRerender:
    def _convert(self, client, monkeypatch):
        def fake_download(asset_id, dest, *, resolution, fmt, verbose=False):
            mat_dir = Path(dest) / "Bricks104"
            mat_dir.mkdir(parents=True)
            (mat_dir / "Bricks104_1K-JPG_Color.png").write_bytes(_png_bytes(16, (128, 128, 128)))
            (mat_dir / "Bricks104_1K-JPG_Roughness.png").write_bytes(_png_bytes(16, (128, 128, 128)))
            return mat_dir

        monkeypatch.setattr("pbr2rad.ambientcg.download_texture_set", fake_download)
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        resp = client.post("/api/v1/sources/ambientcg/convert",
                           json={"asset_id": "Bricks104", "resolution": "1k", "fmt": "jpg"})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_rerender_applies_overrides_and_composes(self, client, monkeypatch):
        from pbr2rad.web import tempdir
        body = self._convert(client, monkeypatch)
        job = body["job_id"]
        assert body["specularity"] == 0.05 and body["diffuse_scale"] == 1.0
        assert body["reflectance"]["specular_rgb"] == [0.05, 0.05, 0.05]
        assert tempdir.read_job_state(job)["kind"] == "source"

        r = client.post(f"/api/v1/jobs/{job}/rerender", json={"specularity": 0.2, "roughness": 0.4})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["job_id"] == job and d["specularity"] == 0.2 and d["roughness"] == 0.4
        assert d["roughness_radiance"] == 0.16
        assert d["reflectance"]["specular_rgb"] == [0.2, 0.2, 0.2]
        rad = next((tempdir.get_output_dir(job)).rglob("Bricks104.rad")).read_text()
        assert "5 1 1 1 0.2 0.16" in rad
        # Download reflects the tuned material.
        dl = client.get(d["download_url"])
        assert dl.status_code == 200

        # A second move keeps the earlier overrides (they compose).
        r2 = client.post(f"/api/v1/jobs/{job}/rerender", json={"diffuse_scale": 1.5})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["specularity"] == 0.2 and d2["roughness"] == 0.4 and d2["diffuse_scale"] == 1.5
        assert d2["reflectance"]["diffuse_rgb"][0] > d["reflectance"]["diffuse_rgb"][0]

    def test_rerender_metalness_flips_primitive(self, client, monkeypatch):
        body = self._convert(client, monkeypatch)
        job = body["job_id"]
        assert body["primitive"] == "plastic"
        r = client.post(f"/api/v1/jobs/{job}/rerender", json={"metalness": 0.8})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["primitive"] == "metal" and d["metalness"] == 0.8
        assert d["specularity"] == 1.0                        # primitive default re-seeded
        assert d["reflectance"]["diffuse_rgb"] == [0.0, 0.0, 0.0]
        # Back to plastic, dropping any stored specularity override.
        r2 = client.post(f"/api/v1/jobs/{job}/rerender", json={"metalness": 0.0, "reset_specularity": True})
        assert r2.json()["primitive"] == "plastic" and r2.json()["specularity"] == 0.05

    def test_rerender_with_options_patch(self, client, monkeypatch):
        """Conversion settings (projection etc.) can be re-applied to the job."""
        body = self._convert(client, monkeypatch)
        job = body["job_id"]
        from pbr2rad.web import tempdir
        out = tempdir.get_output_dir(job)
        assert "planar" not in next(out.rglob("Bricks104.cal")).read_text()
        r = client.post(f"/api/v1/jobs/{job}/rerender", json={
            "options": {"projection": "planar", "planar_axis": "xz", "u_scale": 2.0},
            "roughness": 0.3,
        })
        assert r.status_code == 200, r.text
        cal = next(out.rglob("Bricks104.cal")).read_text()
        assert "planar XZ" in cal and "u_scale : 2" in cal
        assert r.json()["roughness"] == 0.3
        # Stored options now reflect the patch (later moves compose on them).
        assert tempdir.read_job_state(job)["options"]["projection"] == "planar"

    def test_rerender_unknown_job_404_and_validation(self, client):
        assert client.post("/api/v1/jobs/doesnotexist/rerender", json={"specularity": 0.1}).status_code == 404
        r = client.post("/api/v1/jobs/doesnotexist/rerender", json={"specularity": 1.5})
        assert r.status_code == 422

    def test_web_always_renders_reference_wrap(self, client, monkeypatch):
        """The web app always writes the reference-wrap chain for the preview
        sphere (preview_<name>.rad) next to the exported chain; nothing about
        it leaks into the job state, the response or the options."""
        from pbr2rad.web import tempdir

        def fake_download(asset_id, dest, *, resolution, fmt, verbose=False):
            mat_dir = Path(dest) / "Bricks104"
            mat_dir.mkdir(parents=True)
            (mat_dir / "Bricks104_1K-JPG_Color.png").write_bytes(_png_bytes(16, (128, 128, 128)))
            return mat_dir

        monkeypatch.setattr("pbr2rad.ambientcg.download_texture_set", fake_download)
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        resp = client.post("/api/v1/sources/ambientcg/convert",
                           json={"asset_id": "Bricks104", "resolution": "1k", "fmt": "jpg"})
        assert resp.status_code == 200, resp.text
        job = resp.json()["job_id"]
        assert "preview_mapping" not in resp.json()
        state = tempdir.read_job_state(job)
        assert "preview_mapping" not in state and "preview_mapping" not in state["options"]
        out = tempdir.get_output_dir(job)
        names = {p.name for p in out.rglob("*")}
        assert {"Bricks104.rad", "Bricks104.cal", "preview_Bricks104.rad", "preview_Bricks104.cal"} <= names
        r2 = client.post(f"/api/v1/jobs/{job}/rerender", json={"roughness": 0.3})
        assert r2.status_code == 200, r2.text
        names = {p.name for p in out.rglob("*")}
        assert "preview_Bricks104.rad" in names and "Bricks104.rad" in names
        # The exported chain carries the user's projection; the preview chain the wrap.
        exported = next(out.rglob("Bricks104.cal")).read_text()
        wrap = next(out.rglob("preview_Bricks104.cal")).read_text()
        assert "spherical" in wrap and "u_scale : 3" in wrap and "transposed" not in wrap
        assert "spherical" not in exported

    def test_rerender_upload_job_with_labels(self, client, monkeypatch):
        from pbr2rad.web import tempdir
        monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
        files = [_upload("a.png", _png_bytes(16, (120, 90, 60))), _upload("r.png", _png_bytes(16, (90, 90, 90)))]
        channels = json.dumps([{"filename": "a.png", "channel": "albedo"}, {"filename": "r.png", "channel": "roughness"}])
        resp = client.post("/api/v1/convert/upload", files=files, data={"channels": channels, "name": "mymat", "options": "{}"})
        assert resp.status_code == 200, resp.text
        job = resp.json()["job_id"]
        assert tempdir.read_job_state(job)["kind"] == "upload"
        r = client.post(f"/api/v1/jobs/{job}/rerender", json={"specularity": 0.1})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "mymat" and r.json()["specularity"] == 0.1
