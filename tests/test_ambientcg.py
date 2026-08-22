"""Tests for the ambientCG fetcher (mocked HTTP, in-memory zips)."""

from __future__ import annotations

import io
import json
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from pbr2rad import ambientcg
from pbr2rad.ambientcg import (
    FetchError,
    _extract_maps,
    _pick_download,
    _zip_attribute,
    download_texture_set,
    ensure_zip,
    fetch_asset,
    fetch_catalog,
    map_thumbnails,
    normalize_asset,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Cache under tmp_path/cache (conftest isolates too; this pins the path
    the assertions below use) and a fresh thumbnail-download bucket."""
    monkeypatch.setenv("PBR2RAD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(ambientcg, "_THUMB_DOWNLOADS",
                        ambientcg.TokenBucket(ambientcg.THUMB_DL_BURST, ambientcg.THUMB_DL_PER_MIN))
    yield


def _png(size: int = 8, mode: str = "RGB", color=(120, 100, 80)) -> bytes:
    buf = io.BytesIO()
    if mode == "L":
        color = 128
    Image.new(mode, (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _std_members(aid: str = "Bricks104", attr: str = "1K-PNG") -> dict[str, bytes]:
    ext = "png" if attr.endswith("PNG") else "jpg"
    return {
        f"{aid}_{attr}_Color.{ext}": _png(),
        f"{aid}_{attr}_NormalGL.{ext}": _png(color=(128, 128, 255)),
        f"{aid}_{attr}_NormalDX.{ext}": _png(color=(128, 128, 255)),
        f"{aid}_{attr}_Roughness.{ext}": _png(mode="L"),
        f"{aid}_{attr}_Displacement.{ext}": _png(mode="L"),
        f"{aid}_{attr}_AmbientOcclusion.{ext}": _png(mode="L"),
        f"{aid}.mtlx": b"<materialx/>",
        f"{aid}_{attr}.usdc": b"PXR-USDC",
        # Hostile / odd members: traversal, absolute path, nested folder.
        "../evil_Color.png": _png(),
        "/abs/x_Roughness.png": _png(),
        f"sub/dir/{aid}_{attr}_Metalness.{ext}": _png(mode="L"),
    }


def _downloads(aid: str, sizes: dict[str, int]) -> list[dict]:
    return [
        {
            "attributes": attr,
            "extension": "zip",
            "url": f"https://ambientcg.com/get?file={aid}_{attr}.zip",
            "size": size,
        }
        for attr, size in sizes.items()
    ]


def _asset(aid: str = "Bricks104", *, sizes: dict[str, int] | None = None,
           dims=(0, 0), thumbs: bool = True) -> dict:
    if sizes is None:
        sizes = {"1K-JPG": 4_252_352, "1K-PNG": 9_190_179, "2K-JPG": 13_008_436,
                 "2K-PNG": 33_458_789, "4K-JPG": 48_696_227, "8K-PNG": 519_866_930}
    return {
        "id": aid,
        "title": "Bricks 104",
        "type": "material",
        "technique": "surface-photogrammetry",
        "tags": ["bricks", "red", "wall", "masonry"],
        "dimensions": {"width": dims[0], "height": dims[1], "depth": 0},
        "maps": ["color", "displacement", "normal", "roughness", "ambient-occlusion"],
        "thumbnails": (
            {"128-PNG": "https://cdn.example/128.png",
             "256-JPG-FFFFFF": "https://cdn.example/256.jpg",
             "256-JPG-242424": "https://cdn.example/256-dark.jpg",
             "512-JPG-FFFFFF": "https://cdn.example/512.jpg",
             "512-JPG-242424": "https://cdn.example/512-dark.jpg",
             "256-WEBP": "https://cdn.example/256.webp"}
            if thumbs else {}
        ),
        "downloads": _downloads(aid, sizes),
    }


def _api(assets: list[dict], next_url: str | None = None) -> bytes:
    return json.dumps({
        "totalResults": len(assets),
        "nextPageHttp": next_url,
        "assets": assets,
    }).encode()


class _FakeResp:
    """Context-manager response supporting both read() and chunked read(n)."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Seq:
    """Serve canned bodies in order; record every requested URL."""

    def __init__(self, bodies: list[bytes]):
        self.bodies = list(bodies)
        self.urls: list[str] = []

    def __call__(self, req, timeout=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.urls.append(url)
        if not self.bodies:
            raise AssertionError(f"unexpected request: {url}")
        return _FakeResp(self.bodies.pop(0))


def _patch(seq: _Seq):
    # Both modules import the same urllib.request; patching one patches all.
    return mock.patch("pbr2rad.ambientcg.urllib.request.urlopen", side_effect=seq)


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# ---------------------------------------------------------------------------
# Attribute / id validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_attribute_mapping(self):
        assert _zip_attribute("1k", "png") == "1K-PNG"
        assert _zip_attribute("1k", "jpg") == "1K-JPG"
        assert _zip_attribute("2k", "png") == "2K-PNG"
        assert _zip_attribute("2K", "JPG") == "2K-JPG"
        with pytest.raises(FetchError, match="png/jpg"):
            _zip_attribute("1k", "exr")
        with pytest.raises(FetchError, match="png/jpg"):
            _zip_attribute("4k", "png")

    @pytest.mark.parametrize("bad", ["", "../x", "a b", "Bricks-104", "x/y", "a" * 65])
    def test_invalid_id_rejected_before_network(self, bad, tmp_path):
        seq = _Seq([])  # any request would raise AssertionError
        with _patch(seq):
            with pytest.raises(FetchError, match="invalid ambientCG asset id"):
                fetch_asset(bad)
            with pytest.raises(FetchError, match="invalid ambientCG asset id"):
                download_texture_set(bad, tmp_path)
        assert seq.urls == []

    def test_exr_rejected_before_network(self, tmp_path):
        seq = _Seq([])
        with _patch(seq):
            with pytest.raises(FetchError, match="png/jpg"):
                download_texture_set("Bricks104", tmp_path, fmt="exr")
        assert seq.urls == []

    def test_unknown_asset_raises(self):
        seq = _Seq([_api([])])
        with _patch(seq):
            with pytest.raises(FetchError, match="asset not found"):
                fetch_asset("Nope999")
        q = _query(seq.urls[0])
        assert q["id"] == "Nope999"
        assert q["type"] == "material"

    def test_missing_attribute_raises(self):
        asset = _asset(sizes={"1K-JPG": 100, "4K-JPG": 900})
        with pytest.raises(FetchError, match=r"no 2K-PNG download \(available: 1K-JPG\)"):
            _pick_download(asset, "2K-PNG")
        assert _pick_download(asset, "1K-JPG") == (
            "https://ambientcg.com/get?file=Bricks104_1K-JPG.zip", 100,
        )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class TestExtract:
    def test_extracts_only_consumed_channels(self, tmp_path):
        zip_bytes = _make_zip(_std_members())
        zip_path = tmp_path / "Bricks104_1K-PNG.zip"
        zip_path.write_bytes(zip_bytes)
        dest = tmp_path / "out" / "Bricks104"

        extracted = _extract_maps(zip_path, dest)

        names = sorted(p.name for p in extracted)
        assert names == [
            "Bricks104_1K-PNG_Color.png",
            "Bricks104_1K-PNG_Metalness.png",   # nested member, flattened
            "Bricks104_1K-PNG_NormalDX.png",
            "Bricks104_1K-PNG_NormalGL.png",
            "Bricks104_1K-PNG_Roughness.png",
        ]
        on_disk = sorted(p.name for p in dest.iterdir())
        assert on_disk == names
        # Nothing escaped the destination folder.
        assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["Bricks104"]
        assert not (tmp_path / "evil_Color.png").exists()
        assert not (tmp_path / "out" / "evil_Color.png").exists()
        # Each extracted file is a real, intact PNG.
        for p in extracted:
            with Image.open(p) as im:
                assert im.size == (8, 8)

    def test_member_size_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ambientcg, "MAX_MEMBER_BYTES", 10)
        zip_path = tmp_path / "z.zip"
        zip_path.write_bytes(_make_zip({"Bricks104_1K-PNG_Color.png": _png()}))
        with pytest.raises(FetchError, match="per-file limit"):
            _extract_maps(zip_path, tmp_path / "out")
        assert not (tmp_path / "out" / "Bricks104_1K-PNG_Color.png").exists()

    def test_total_extract_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ambientcg, "MAX_EXTRACT_BYTES", 150)
        zip_path = tmp_path / "z.zip"
        zip_path.write_bytes(_make_zip(_std_members()))
        with pytest.raises(FetchError, match="expands past"):
            _extract_maps(zip_path, tmp_path / "out")

    def test_corrupt_member_is_rejected_and_cleaned(self, tmp_path, monkeypatch):
        """A member whose header lies about its size must not be extracted."""
        zip_path = tmp_path / "z.zip"
        zip_path.write_bytes(_make_zip({"Bricks104_1K-PNG_Color.png": _png(size=32)}))

        real_zipfile = ambientcg.zipfile.ZipFile

        class _LyingZip(real_zipfile):
            def infolist(self):
                infos = super().infolist()
                for i in infos:
                    i.file_size = 5  # claims 5 bytes; real payload is far larger
                return infos

        monkeypatch.setattr(ambientcg.zipfile, "ZipFile", _LyingZip)
        with pytest.raises(FetchError):
            _extract_maps(zip_path, tmp_path / "out")
        assert not (tmp_path / "out" / "Bricks104_1K-PNG_Color.png").exists()

    def test_bad_zip_is_evicted(self, tmp_path):
        zip_path = tmp_path / "cache" / "Bricks104_1K-PNG.zip"
        zip_path.parent.mkdir()
        zip_path.write_bytes(b"<html>not a zip</html>")
        with pytest.raises(FetchError, match="not a valid zip"):
            _extract_maps(zip_path, tmp_path / "out")
        assert not zip_path.exists()


# ---------------------------------------------------------------------------
# download_texture_set (full flow, mocked HTTP)
# ---------------------------------------------------------------------------

class TestDownloadTextureSet:
    def _flow(self, tmp_path, *, aid="Bricks104", requested="bricks104",
              resolution="1k", fmt="png", zip_bytes=None, declared=None,
              dims=(0, 0)):
        attr = _zip_attribute(resolution, fmt)
        zip_bytes = zip_bytes if zip_bytes is not None else _make_zip(_std_members(aid, attr))
        declared = len(zip_bytes) if declared is None else declared
        asset = _asset(aid, sizes={attr: declared, "8K-PNG": 519_866_930}, dims=dims)
        seq = _Seq([_api([asset]), zip_bytes])
        with _patch(seq):
            mat_dir = download_texture_set(
                requested, tmp_path / "out", resolution=resolution, fmt=fmt, verbose=True,
            )
        return mat_dir, seq

    def test_full_flow_and_canonical_id(self, tmp_path):
        mat_dir, seq = self._flow(tmp_path, requested="bricks104")
        # Folder (and thus the material name) uses the API's canonical spelling.
        assert mat_dir == tmp_path / "out" / "Bricks104"
        assert _query(seq.urls[0])["id"] == "bricks104"
        assert seq.urls[1] == "https://ambientcg.com/get?file=Bricks104_1K-PNG.zip"
        names = sorted(p.name for p in mat_dir.iterdir())
        assert names == [
            "Bricks104_1K-PNG_Color.png",
            "Bricks104_1K-PNG_Metalness.png",
            "Bricks104_1K-PNG_NormalDX.png",
            "Bricks104_1K-PNG_NormalGL.png",
            "Bricks104_1K-PNG_Roughness.png",
            "pbr2rad_source.json",
        ]
        # Zip landed in the per-source cache.
        cache = Path(tmp_path / "cache" / "ambientcg" / "Bricks104" / "Bricks104_1K-PNG.zip")
        assert cache.is_file()
        assert not cache.with_name(cache.name + ".part").exists()

    def test_sidecar_contents(self, tmp_path):
        mat_dir, _ = self._flow(tmp_path, dims=(180, 180), fmt="jpg")
        side = json.loads((mat_dir / "pbr2rad_source.json").read_text())
        assert side["generator"] == "pbr2rad"
        assert side["source"] == "ambientcg"
        assert side["asset_id"] == "Bricks104"
        assert side["name"] == "Bricks 104"
        assert side["asset_url"] == "https://ambientcg.com/a/Bricks104"
        assert side["license"] == "CC0-1.0"
        assert side["resolution"] == "1k" and side["fmt"] == "jpg"
        assert side["dims_cm"] == [180.0, 180.0]
        assert "color" in side["maps"]
        assert "bricks" in side["tags"]

    def test_discover_and_convert_end_to_end(self, tmp_path):
        from pbr2rad.convert import convert_set, write_manifest
        from pbr2rad.discover import discover

        mat_dir, _ = self._flow(tmp_path)
        pbr = discover(mat_dir)
        assert pbr.name == "Bricks104"
        assert pbr.albedo.name == "Bricks104_1K-PNG_Color.png"
        assert pbr.normal.name == "Bricks104_1K-PNG_NormalGL.png"
        assert pbr.roughness is not None and pbr.metalness is not None
        # The sidecar is not an image, so discover() neither maps nor lists it.
        assert pbr.extras == []
        assert (mat_dir / "pbr2rad_source.json").is_file()

        out = tmp_path / "rad"
        result = convert_set(pbr, out)
        assert (out / "Bricks104" / "Bricks104.rad").is_file()
        assert result.source["source"] == "ambientcg"
        rad_text = (out / "Bricks104" / "Bricks104.rad").read_text()
        assert "# source: ambientCG Bricks104 (CC0-1.0) https://ambientcg.com/a/Bricks104" in rad_text

        manifest = json.loads(write_manifest([result], out).read_text())
        entry = manifest["materials"][0]
        assert entry["name"] == "Bricks104"
        assert entry["source"]["source"] == "ambientcg"
        assert entry["source"]["asset_url"] == "https://ambientcg.com/a/Bricks104"

    def test_size_cap_rejected_before_download(self, tmp_path):
        asset = _asset(sizes={"1K-PNG": ambientcg.MAX_ZIP_BYTES + 1})
        seq = _Seq([_api([asset])])
        with _patch(seq):
            with pytest.raises(FetchError, match="over the 64 MB limit"):
                download_texture_set("Bricks104", tmp_path / "out")
        assert len(seq.urls) == 1  # API only — no transfer attempted

    def test_streaming_cap_aborts_and_cleans_part(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ambientcg, "MAX_ZIP_BYTES", 1000)
        body = _make_zip(_std_members()) + b"\0" * 5000  # > cap, declared small
        asset = _asset(sizes={"1K-PNG": 900})
        seq = _Seq([_api([asset]), body])
        with _patch(seq):
            with pytest.raises(FetchError, match="exceeded"):
                download_texture_set("Bricks104", tmp_path / "out")
        cache_dir = tmp_path / "cache" / "ambientcg" / "Bricks104"
        assert not list(cache_dir.glob("*")) if cache_dir.exists() else True

    def test_size_mismatch_rejected(self, tmp_path):
        zip_bytes = _make_zip(_std_members())
        asset = _asset(sizes={"1K-PNG": len(zip_bytes) + 7})
        seq = _Seq([_api([asset]), zip_bytes])
        with _patch(seq):
            with pytest.raises(FetchError, match="size mismatch"):
                download_texture_set("Bricks104", tmp_path / "out")
        cache = tmp_path / "cache" / "ambientcg" / "Bricks104" / "Bricks104_1K-PNG.zip"
        assert not cache.exists()

    def test_cache_hit_skips_download(self, tmp_path):
        zip_bytes = _make_zip(_std_members())
        asset = _asset(sizes={"1K-PNG": len(zip_bytes)})
        seq = _Seq([_api([asset]), zip_bytes, _api([asset])])
        with _patch(seq):
            download_texture_set("Bricks104", tmp_path / "out1")
            mat_dir = download_texture_set("Bricks104", tmp_path / "out2")
        # 3 requests total: API, zip, API (second run served from cache).
        assert len(seq.urls) == 3
        assert seq.bodies == []
        assert (mat_dir / "Bricks104_1K-PNG_Color.png").is_file()

    def test_bad_zip_body_is_evicted(self, tmp_path):
        body = b"<html>maintenance</html>"
        asset = _asset(sizes={"1K-PNG": len(body)})
        seq = _Seq([_api([asset]), body])
        with _patch(seq):
            with pytest.raises(FetchError, match="not a valid zip"):
                download_texture_set("Bricks104", tmp_path / "out")
        cache = tmp_path / "cache" / "ambientcg" / "Bricks104" / "Bricks104_1K-PNG.zip"
        assert not cache.exists()

    def test_no_usable_maps(self, tmp_path):
        zip_bytes = _make_zip({"Bricks104.mtlx": b"<materialx/>", "README.txt": b"hi"})
        asset = _asset(sizes={"1K-PNG": len(zip_bytes)})
        seq = _Seq([_api([asset]), zip_bytes])
        with _patch(seq):
            with pytest.raises(FetchError, match="no usable texture maps"):
                download_texture_set("Bricks104", tmp_path / "out")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_normalize_asset(self):
        entry = normalize_asset(_asset(dims=(180, 90)))
        assert entry["id"] == "Bricks104"
        assert entry["name"] == "Bricks 104"
        assert entry["preview"] == "https://cdn.example/256.jpg"   # 256-JPG-FFFFFF preferred
        assert entry["preview_dark"] == "https://cdn.example/256-dark.jpg"
        assert entry["preview_large"] == "https://cdn.example/512.jpg"
        assert entry["preview_large_dark"] == "https://cdn.example/512-dark.jpg"
        assert entry["tags"] == ["bricks", "red", "wall", "masonry"]
        assert entry["maps"] == ["color", "displacement", "normal", "roughness", "ambient-occlusion"]
        assert entry["dims_cm"] == [180.0, 90.0]
        # Only the resolutions pbr2rad will ever fetch are kept.
        assert set(entry["downloads"]) == {"1K-JPG", "1K-PNG", "2K-JPG", "2K-PNG"}
        assert entry["downloads"]["1K-JPG"] == 4_252_352

    def test_normalize_asset_sparse(self):
        entry = normalize_asset({"id": "X1", "thumbnails": {}, "downloads": []})
        assert entry == {
            "id": "X1", "name": "X1", "preview": None, "preview_dark": None,
            "preview_large": None, "preview_large_dark": None,
            "tags": [], "maps": [], "dims_cm": None, "downloads": {},
        }

    def test_large_preview_falls_back_to_small(self):
        entry = normalize_asset({
            "id": "X2",
            "thumbnails": {"256-JPG-FFFFFF": "https://cdn.example/s.jpg",
                           "256-JPG-242424": "https://cdn.example/s-dark.jpg"},
        })
        assert entry["preview_large"] == "https://cdn.example/s.jpg"
        assert entry["preview_large_dark"] == "https://cdn.example/s-dark.jpg"

    def test_fetch_catalog_paginates(self):
        page2_url = "https://ambientcg.com/api/v3/assets?type=material&limit=500&offset=500"
        seq = _Seq([
            _api([_asset("Bricks104"), _asset("Wood051")], next_url=page2_url),
            _api([_asset("Wood051"), _asset("Metal032")], next_url=None),
        ])
        with _patch(seq):
            entries = fetch_catalog()
        assert [e["id"] for e in entries] == ["Bricks104", "Wood051", "Metal032"]
        assert len(seq.urls) == 2
        q = _query(seq.urls[0])
        assert q["type"] == "material" and q["limit"] == "500" and q["sort"] == "popular"
        assert seq.urls[1] == page2_url

    def test_fetch_catalog_ignores_offhost_next(self):
        seq = _Seq([_api([_asset("Bricks104")], next_url="https://evil.example/api/v3/assets")])
        with _patch(seq):
            entries = fetch_catalog()
        assert [e["id"] for e in entries] == ["Bricks104"]
        assert len(seq.urls) == 1

    def test_fetch_catalog_page_failure_raises(self):
        import urllib.error

        def boom(req, timeout=None, **kw):
            raise urllib.error.URLError("down")

        with mock.patch("pbr2rad.ambientcg.urllib.request.urlopen", side_effect=boom):
            with pytest.raises(FetchError, match="network error"):
                fetch_catalog()


# ---------------------------------------------------------------------------
# ensure_zip + per-map thumbnails (web UI)
# ---------------------------------------------------------------------------

class TestThumbnails:
    def _seq_for(self, tmp_path, members=None, attr="1K-JPG"):
        zip_bytes = _make_zip(members if members is not None else _std_members("Bricks104", attr))
        asset = _asset("Bricks104", sizes={attr: len(zip_bytes), "8K-PNG": 519_866_930})
        return _Seq([_api([asset]), zip_bytes]), zip_bytes

    def test_ensure_zip_downloads_then_hits_cache(self, tmp_path):
        seq, zip_bytes = self._seq_for(tmp_path)
        seq.bodies.append(_api([_asset("Bricks104", sizes={"1K-JPG": len(zip_bytes)})]))
        with _patch(seq):
            path, asset = ensure_zip("bricks104", resolution="1k", fmt="jpg")
            assert path.is_file() and asset["id"] == "Bricks104"
            assert path == tmp_path / "cache" / "ambientcg" / "Bricks104" / "Bricks104_1K-JPG.zip"
            path2, _ = ensure_zip("Bricks104", resolution="1k", fmt="jpg")
        assert path2 == path
        assert len(seq.urls) == 3          # API, zip, API (cache hit)

    def test_ensure_zip_refuses_download_when_disabled(self, tmp_path):
        asset = _asset("Bricks104", sizes={"1K-JPG": 123})
        seq = _Seq([_api([asset])])
        with _patch(seq):
            with pytest.raises(FetchError, match="not cached"):
                ensure_zip("Bricks104", resolution="1k", fmt="jpg", allow_download=False)
        assert len(seq.urls) == 1

    def test_map_thumbnails_generated_from_pack(self, tmp_path):
        seq, _ = self._seq_for(tmp_path)
        with _patch(seq):
            thumbs = map_thumbnails("Bricks104", size=32)
        # Every pictured channel, GL normal preferred over DX, AO/displacement included.
        assert set(thumbs) == {"albedo", "normal_gl", "roughness", "metalness", "ao", "displacement"}
        for channel, path in thumbs.items():
            assert path.parent == tmp_path / "cache" / "ambientcg" / "Bricks104" / "thumbs_1K-JPG"
            with Image.open(path) as im:
                assert im.format == "JPEG" and max(im.size) <= 32
        # Nothing was extracted next to the zip.
        zdir = tmp_path / "cache" / "ambientcg" / "Bricks104"
        assert sorted(p.name for p in zdir.iterdir()) == ["Bricks104_1K-JPG.zip", "thumbs_1K-JPG"]
        # Second call: served from the index, no network at all.
        with _patch(_Seq([])):
            again = map_thumbnails("Bricks104", size=32)
        assert again == thumbs

    def test_map_thumbnails_respect_cache_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ambientcg, "MAX_THUMB_CACHE_BYTES", 0)
        # Put a byte in the cache so it's "over" the (zero) cap.
        (tmp_path / "cache" / "ambientcg").mkdir(parents=True)
        (tmp_path / "cache" / "ambientcg" / "x").write_bytes(b"1")
        seq = _Seq([])                      # nothing may hit the network
        with _patch(seq):
            assert map_thumbnails("Bricks104") == {}
        assert seq.urls == []

    def test_map_thumbnails_throttled_without_network(self, tmp_path, monkeypatch):
        """Once the download bucket is empty, uncached assets get label tiles
        and we make no upstream calls at all; cached packs still work."""
        monkeypatch.setattr(ambientcg, "_THUMB_DOWNLOADS", ambientcg.TokenBucket(1, 0.0))
        seq1, _ = self._seq_for(tmp_path)
        with _patch(seq1):
            assert set(map_thumbnails("Bricks104", size=16))      # uses the one token
        seq2 = _Seq([])
        with _patch(seq2):
            assert map_thumbnails("Wood051", size=16) == {}         # throttled, no network
            assert set(map_thumbnails("Bricks104", size=16))       # index hit, no network
        assert seq2.urls == []

    def test_map_thumbnails_use_catalog_entry_without_api_call(self, tmp_path):
        """With a catalog entry we know the pack URL/size → skip fetch_asset."""
        zip_bytes = _make_zip(_std_members("Bricks104", "1K-JPG"))
        entry = {"id": "Bricks104", "name": "Bricks 104",
                 "downloads": {"1K-JPG": len(zip_bytes), "2K-PNG": 999}}
        seq = _Seq([zip_bytes])             # only the zip itself
        with _patch(seq):
            thumbs = map_thumbnails("Bricks104", size=16, entry=entry)
        assert "albedo" in thumbs
        assert seq.urls == ["https://ambientcg.com/get?file=Bricks104_1K-JPG.zip"]

    def test_no_index_written_when_nothing_decodes(self, tmp_path):
        """A transient all-fail must not pin the asset to label tiles forever."""
        members = {name: b"not an image" for name in _std_members("Bricks104", "1K-JPG")}
        seq, _ = self._seq_for(tmp_path, members)
        with _patch(seq):
            assert map_thumbnails("Bricks104", size=16) == {}
        tdir = tmp_path / "cache" / "ambientcg" / "Bricks104" / "thumbs_1K-JPG"
        assert not (tdir / "index.json").exists()

    def test_concurrent_ensure_zip_downloads_once(self, tmp_path):
        """Two callers for the same pack share one download (.part race fix)."""
        import threading
        zip_bytes = _make_zip(_std_members("Bricks104", "1K-JPG"))
        asset = _asset("Bricks104", sizes={"1K-JPG": len(zip_bytes)})
        n_downloads = []
        lock = threading.Lock()

        class SlowResp(_FakeResp):
            def read(self, n=-1):
                import time
                time.sleep(0.01)
                return super().read(n)

        def fake(req, timeout=None, **kw):
            url = req.full_url
            if "get?file=" in url:
                with lock:
                    n_downloads.append(url)
                return SlowResp(zip_bytes)
            return _FakeResp(_api([asset]))

        results, errors = [], []

        def worker():
            try:
                results.append(ensure_zip("Bricks104", resolution="1k", fmt="jpg"))
            except Exception as exc:   # pragma: no cover - surfaced by assertion
                errors.append(exc)

        with mock.patch("pbr2rad.ambientcg.urllib.request.urlopen", side_effect=fake):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads: t.start()
            for t in threads: t.join()
        assert not errors
        assert len(n_downloads) == 1
        assert all(p == results[0][0] for p, _a in results)
        with zipfile.ZipFile(results[0][0]) as zf:
            assert zf.testzip() is None

    def test_map_thumbnails_survive_a_bad_member(self, tmp_path):
        members = _std_members("Bricks104", "1K-JPG")
        members["Bricks104_1K-JPG_Roughness.jpg"] = b"not an image"
        seq, _ = self._seq_for(tmp_path, members)
        with _patch(seq):
            thumbs = map_thumbnails("Bricks104", size=32)
        assert "roughness" not in thumbs and "albedo" in thumbs
