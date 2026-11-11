"""Tests for the 2026-11 site review: command injection through material
names, id / path validation, the client address the rate limiter trusts,
the Cloudflare-only switch, response headers, disk and pixel budgets, search
paging, zip naming, and the private stats page."""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from pbr2rad import fetch  # noqa: E402
from pbr2rad.convert import (  # noqa: E402
    ConvertOptions, convert_set, is_safe_material_name, safe_material_name,
)
from pbr2rad.discover import discover  # noqa: E402
from pbr2rad.web import api as api_mod  # noqa: E402
from pbr2rad.web import metrics, netsec, tempdir  # noqa: E402
from pbr2rad.web.app import create_app  # noqa: E402

CF_EDGE = "104.16.1.1"          # inside 104.16.0.0/13
NOT_CF = "8.8.8.8"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(api_mod, "radiance_available", lambda: False)
    with TestClient(create_app()) as c:
        yield c


def _png(size=(8, 8), color=(120, 100, 80), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color if mode == "RGB" else 0).save(buf, format="PNG")
    return buf.getvalue()


def _upload(name: str, data: bytes | None = None):
    return ("files", (name, data or _png(), "image/png"))


def _convert(client, name="wood", channels=""):
    return client.post(
        "/api/v1/convert/upload",
        files=[_upload("wood_diff.png")],
        data={"name": name, "options": '{"estimate_maps": false}', "channels": channels},
    )


# ---------------------------------------------------------------------------
# Material names: Radiance runs "!..." scene lines as shell commands
# ---------------------------------------------------------------------------

class TestMaterialNames:
    @pytest.mark.parametrize("name", [
        "m\n!touch pwned\nz", "a b", "a\tb", "!cmd", "a;b", "a$(id)", "a`id`",
        "../x", "a/b", "a..b", "", ".hidden", "x" * 200, "a\rb", "a'b", 'a"b',
    ])
    def test_unsafe_names_refused(self, name):
        assert not is_safe_material_name(name)

    @pytest.mark.parametrize("name", ["Bricks104", "wood_floor_03", "my-mat.v2", "upload", "A"])
    def test_ordinary_names_allowed(self, name):
        assert is_safe_material_name(name)

    def test_safe_material_name_reduces_free_text(self):
        assert safe_material_name("My brick wall") == "My_brick_wall"
        assert safe_material_name("x\n!rm -rf /") == "x_rm_-rf"
        assert safe_material_name("///") == "material"

    def test_convert_set_never_writes_a_command_line(self, tmp_path):
        src = tmp_path / "in"
        src.mkdir()
        (src / "x_albedo.png").write_bytes(_png())
        pbr = discover(src, name="m\n!touch pwned\nz")
        with pytest.raises(ValueError, match="Invalid material name"):
            convert_set(pbr, tmp_path / "out", ConvertOptions())
        assert not list((tmp_path / "out").rglob("*.rad"))

    def test_upload_with_newline_name_is_400(self, client):
        resp = _convert(client, name="m\n!touch pwned")
        assert resp.status_code == 400
        assert "Invalid material name" in resp.json()["detail"]

    def test_upload_name_with_spaces_becomes_underscores(self, client):
        resp = _convert(client, name="My  brick wall")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "My_brick_wall"


# ---------------------------------------------------------------------------
# Poly Haven ids
# ---------------------------------------------------------------------------

class TestPolyHavenIds:
    @pytest.mark.parametrize("slug", [
        "brick_wall_001?zz", "a/../b", "../../etc", "x y", "a\nb", "", "x" * 65, "a.b",
    ])
    def test_bad_slug_never_reaches_the_network(self, slug, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise AssertionError("network touched for an invalid id")
        monkeypatch.setattr(fetch.urllib.request, "urlopen", boom)
        for call in (fetch.fetch_asset_info, fetch.fetch_asset_files):
            with pytest.raises(fetch.FetchError, match="invalid Poly Haven asset id"):
                call(slug)
        with pytest.raises(fetch.FetchError, match="invalid Poly Haven asset id"):
            fetch.download_texture_set(slug, tmp_path)
        assert not any(tmp_path.iterdir())

    def test_info_route_rejects_bad_id(self, client, monkeypatch):
        monkeypatch.setattr(fetch.urllib.request, "urlopen",
                            lambda *a, **k: pytest.fail("network touched"))
        resp = client.get("/api/v1/sources/polyhaven/brick_wall_001%3Fzz/info")
        assert resp.status_code == 404

    def test_convert_route_rejects_bad_id(self, client, monkeypatch):
        monkeypatch.setattr(fetch.urllib.request, "urlopen",
                            lambda *a, **k: pytest.fail("network touched"))
        resp = client.post("/api/v1/sources/polyhaven/convert",
                           json={"asset_id": "x?/../../../escape"})
        assert resp.status_code == 400
        assert not (tempdir.get_root() / "escape").exists()


# ---------------------------------------------------------------------------
# Whose address the rate limiter uses
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, headers=None, peer="10.0.0.9"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = type("C", (), {"host": peer})()


class TestClientAddress:
    def test_forwarded_for_is_ignored(self):
        assert netsec.client_ip(_Req({"X-Forwarded-For": "203.0.113.7"})) == "10.0.0.9"

    def test_visitor_behind_cloudflare(self):
        req = _Req({"Fly-Client-IP": CF_EDGE, "CF-Connecting-IP": "198.51.100.4",
                    "X-Forwarded-For": "203.0.113.7"})
        assert netsec.client_ip(req) == "198.51.100.4"

    def test_direct_caller_cannot_claim_a_visitor_address(self):
        req = _Req({"Fly-Client-IP": NOT_CF, "CF-Connecting-IP": "198.51.100.4"})
        assert netsec.client_ip(req) == NOT_CF

    def test_ipv6_cloudflare_edge(self):
        req = _Req({"Fly-Client-IP": "2606:4700::1", "CF-Connecting-IP": "2001:db8::5"})
        assert netsec.client_ip(req) == "2001:db8::5"

    def test_spoofed_forwarded_for_does_not_reset_the_limit(self, client, monkeypatch):
        monkeypatch.setattr(api_mod, "_load_catalog", lambda source: [])
        codes = [
            client.get("/api/v1/sources/polyhaven/search?q=x",
                       headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
            for i in range(62)
        ]
        assert codes.count(200) == 60 and codes[-1] == 429

    def test_visitors_behind_cloudflare_get_separate_buckets(self, client, monkeypatch):
        monkeypatch.setattr(api_mod, "_load_catalog", lambda source: [])
        def get(visitor):
            return client.get("/api/v1/sources/polyhaven/search?q=x", headers={
                "Fly-Client-IP": CF_EDGE, "CF-Connecting-IP": visitor}).status_code
        assert [get("198.51.100.1") for _ in range(61)][-1] == 429
        assert get("198.51.100.2") == 200


class TestCloudflareOnly:
    def test_off_by_default(self, client):
        assert client.get("/", headers={"Fly-Client-IP": NOT_CF}).status_code == 200

    def test_direct_traffic_refused_when_required(self, client, monkeypatch):
        monkeypatch.setenv(netsec.REQUIRE_CLOUDFLARE_ENV, "1")
        direct = client.get("/api/v1/sources", headers={"Fly-Client-IP": NOT_CF})
        assert direct.status_code == 403
        assert direct.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get("/api/v1/sources", headers={"Fly-Client-IP": CF_EDGE}).status_code == 200
        # Fly's health check does not come through Cloudflare.
        assert client.get("/api/v1/health", headers={"Fly-Client-IP": NOT_CF}).status_code == 200

    def test_extra_ranges_from_env(self, monkeypatch):
        assert not netsec.is_cloudflare(netsec._parse_ip("192.0.2.10"))
        monkeypatch.setenv(netsec.CLOUDFLARE_CIDRS_ENV, "192.0.2.0/24")
        assert netsec.is_cloudflare(netsec._parse_ip("192.0.2.10"))


# ---------------------------------------------------------------------------
# Headers, CORS, docs, security.txt
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_security_headers_on_page_and_api(self, client):
        for path in ("/", "/api/v1/health", "/api/v1/download/nope"):
            h = client.get(path).headers
            assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
            assert "'unsafe-inline'" not in h["Content-Security-Policy"].split("script-src")[1].split(";")[0]
            assert h["X-Content-Type-Options"] == "nosniff"
            assert h["Strict-Transport-Security"].startswith("max-age=")
            assert h["X-Frame-Options"] == "DENY"

    def test_page_has_no_inline_script(self):
        html = (Path(api_mod.__file__).parent / "static" / "index.html").read_text()
        import re
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
        assert not re.search(r"\son[a-z]+\s*=", html)

    def test_no_wildcard_cors(self, client):
        resp = client.get("/api/v1/sources", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in resp.headers

    def test_cors_only_for_listed_origins(self, monkeypatch):
        monkeypatch.setenv("PBR2RAD_CORS_ORIGINS", "http://localhost:5173")
        with TestClient(create_app()) as c:
            ok = c.get("/api/v1/sources", headers={"Origin": "http://localhost:5173"})
            no = c.get("/api/v1/sources", headers={"Origin": "https://evil.example"})
        assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "access-control-allow-origin" not in no.headers

    def test_docs_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("PBR2RAD_DISABLE_DOCS", "1")
        with TestClient(create_app()) as c:
            assert {c.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")} == {404}

    def test_security_txt_and_favicon(self, client):
        txt = client.get("/.well-known/security.txt")
        assert txt.status_code == 200 and "Contact: mailto:" in txt.text and "Expires: " in txt.text
        ico = client.get("/favicon.ico")
        assert ico.status_code == 200 and ico.headers["content-type"].startswith("image/svg+xml")


# ---------------------------------------------------------------------------
# Jobs: ids, labels, budgets
# ---------------------------------------------------------------------------

class TestJobs:
    @pytest.mark.parametrize("job_id", ["..", ".", "zzzzzzzzzzzz", "ABCDEF012345", "abc", "0" * 13])
    def test_malformed_job_ids_are_not_jobs(self, job_id):
        assert tempdir.get_job_dir(job_id) is None
        assert tempdir.get_output_dir(job_id) is None

    def test_dotdot_job_id_routes_404(self, client):
        assert client.get("/api/v1/download/%2e%2e").status_code == 404
        assert client.post("/api/v1/jobs/%2e%2e/refine").status_code == 404
        assert client.post("/api/v1/jobs/%2e%2e/rerender", json={}).status_code == 404

    def test_channel_label_cannot_point_outside_the_upload(self, client, tmp_path):
        secret = tmp_path / "secret_diff.png"
        secret.write_bytes(_png())
        for label in (str(secret), "../../x.png", "sub/wood_diff.png"):
            resp = _convert(client, channels=f'[{{"filename": "{label}", "channel": "albedo"}}]')
            assert resp.status_code == 400, label
            assert "No albedo" in resp.json()["detail"]

    def test_discover_keeps_nothing_on_disk(self, client):
        before = {p.name for p in tempdir.get_root().iterdir()}
        resp = client.post("/api/v1/discover", files=[_upload("wood_diff.png")])
        assert resp.status_code == 200
        assert resp.json()["channels"][0]["channel"] == "albedo"
        assert {p.name for p in tempdir.get_root().iterdir()} == before

    def test_jobs_budget_refuses_new_work(self, client, monkeypatch):
        monkeypatch.setattr(tempdir, "jobs_usage_bytes", lambda **k: 10 << 20)
        monkeypatch.setenv("PBR2RAD_JOBS_MAX_MB", "5")
        resp = _convert(client)
        assert resp.status_code == 503 and "Retry-After" in resp.headers
        monkeypatch.setenv("PBR2RAD_JOBS_MAX_MB", "0")      # 0 = no ceiling
        assert _convert(client).status_code == 200

    def test_jobs_usage_counts_files(self, client):
        assert _convert(client).status_code == 200
        assert tempdir.jobs_usage_bytes(refresh=True) > 0

    def test_image_declaring_too_many_pixels_is_413(self, client):
        # 9000 x 9000 one-bit PNG: a few KB on the wire, 81 MP to decode.
        huge = _png(size=(9000, 9000), mode="1")
        assert len(huge) < 200_000
        resp = client.post("/api/v1/convert/upload", files=[_upload("big_diff.png", huge)],
                           data={"name": "big", "options": "{}", "channels": ""})
        assert resp.status_code == 413 and "too large" in resp.json()["detail"]
        assert client.post("/api/v1/discover", files=[_upload("big_diff.png", huge)]).status_code == 413

    def test_conversion_error_does_not_echo_internals(self, client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("/home/app/secret/path exploded")
        monkeypatch.setattr(api_mod, "convert_set", boom)
        resp = _convert(client)
        assert resp.status_code == 500
        assert "secret" not in resp.text and "exploded" not in resp.text

    def test_zip_is_named_after_the_material(self, client):
        job = _convert(client, name="oak floor").json()
        resp = client.get(job["download_url"])
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="oak_floor.zip"'


# ---------------------------------------------------------------------------
# Search paging
# ---------------------------------------------------------------------------

class TestSearchPaging:
    @pytest.fixture()
    def catalog(self, monkeypatch):
        data = [{"id": f"Wood{i:03d}", "name": f"Wood {i}", "tags": ["wood"]} for i in range(70)]
        monkeypatch.setattr(api_mod, "_load_catalog", lambda source: data)

    def test_pages_do_not_overlap_and_end(self, client, catalog):
        def page(offset):
            return [r["id"] for r in client.get(
                f"/api/v1/sources/ambientcg/search?q=wood&offset={offset}").json()]
        first, second, third = page(0), page(30), page(60)
        assert (len(first), len(second), len(third)) == (30, 30, 10)
        assert len(set(first + second + third)) == 70
        assert page(70) == []

    def test_legacy_alias_still_works(self, client, catalog):
        rows = client.get("/api/v1/polyhaven/search?q=wood").json()
        assert len(rows) == 30 and rows[0]["slug"] == rows[0]["id"]

    def test_bad_offset_and_long_query_rejected(self, client, catalog):
        assert client.get("/api/v1/sources/ambientcg/search?offset=-1").status_code == 422
        assert client.get("/api/v1/sources/ambientcg/search?q=" + "x" * 201).status_code == 422

    def test_upstream_failure_message_is_generic(self, client, monkeypatch):
        def boom(source):
            raise RuntimeError("https://internal.example/secret failed")
        monkeypatch.setattr(api_mod, "_load_catalog", boom)
        resp = client.get("/api/v1/sources/ambientcg/search?q=x")
        assert resp.status_code == 502 and "secret" not in resp.text


# ---------------------------------------------------------------------------
# Metrics and the stats page
# ---------------------------------------------------------------------------

def _basic(password: str, user: str = "admin") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


class TestStats:
    TOKEN = "t" * 32

    def test_page_does_not_exist_without_a_token(self, client, monkeypatch):
        monkeypatch.delenv(metrics.ADMIN_TOKEN_ENV, raising=False)
        assert client.get("/admin/stats").status_code == 404
        assert client.get("/admin/stats", headers=_basic("anything")).status_code == 404
        monkeypatch.setenv(metrics.ADMIN_TOKEN_ENV, "short")
        assert client.get("/admin/stats", headers=_basic("short")).status_code == 404

    def test_needs_the_right_password(self, client, monkeypatch):
        monkeypatch.setenv(metrics.ADMIN_TOKEN_ENV, self.TOKEN)
        anon = client.get("/admin/stats")
        assert anon.status_code == 401 and "Basic" in anon.headers["WWW-Authenticate"]
        assert client.get("/admin/stats", headers=_basic("wrong" * 8)).status_code == 401
        assert client.get("/admin/stats", headers={"Authorization": "Basic !!!"}).status_code == 401
        ok = client.get("/admin/stats", headers=_basic(self.TOKEN))
        assert ok.status_code == 200 and ok.headers["Cache-Control"] == "no-store"

    def test_wrong_passwords_run_out_of_tries(self, client, monkeypatch):
        monkeypatch.setenv(metrics.ADMIN_TOKEN_ENV, self.TOKEN)
        from pbr2rad.web import admin
        admin._admin_limit.reset()
        codes = [client.get("/admin/stats", headers=_basic("wrong" * 8)).status_code for _ in range(12)]
        admin._admin_limit.reset()
        assert codes[:10] == [401] * 10 and codes[-1] == 429

    def test_counts_conversions_and_downloads_without_visitor_data(self, client, monkeypatch):
        monkeypatch.setenv(metrics.ADMIN_TOKEN_ENV, self.TOKEN)
        from pbr2rad.web import admin
        admin._admin_limit.reset()
        job = _convert(client, name="private project name").json()
        assert client.get(job["download_url"]).status_code == 200
        assert _convert(client, name="bad;name").status_code == 400

        data = metrics.summary(7)
        assert data["totals"] == {"convert": 2, "download": 1}
        assert data["by_source"][0][:3] == ("upload", 2, 1)
        assert data["top_assets"] == []                     # uploads have no asset
        raw = metrics.db_path().read_bytes()
        for private in (b"private", b"wood_diff", b"testclient", b"127.0.0.1"):
            assert private not in raw

        page = client.get("/admin/stats?days=7", headers=_basic(self.TOKEN)).text
        assert "conversions" in page and "upload" in page

    def test_a_broken_database_never_breaks_a_request(self, client, monkeypatch, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        monkeypatch.setenv(metrics.DATA_DIR_ENV, str(blocker / "sub"))
        assert _convert(client).status_code == 200
        assert "error" in metrics.summary(7)
