"""Tests for the texture-source registry."""

from __future__ import annotations

import pytest

from pbr2rad import sources


def test_registry_keys_and_default():
    assert set(sources.SOURCES) == {"polyhaven", "ambientcg"}
    assert sources.DEFAULT_SOURCE == "polyhaven"
    for key, src in sources.SOURCES.items():
        assert src.key == key
        assert src.web_default_fmt in src.formats
        assert src.catalog_ttl > 0


def test_unknown_source_raises():
    with pytest.raises(ValueError, match="unknown texture source"):
        sources.get_source("texturehaven")


def test_downloader_resolves_lazily(monkeypatch):
    import pbr2rad.ambientcg as acg
    import pbr2rad.fetch as ph

    assert sources.get_downloader("ambientcg") is acg.download_texture_set
    assert sources.get_downloader("polyhaven") is ph.download_texture_set
    assert sources.get_catalog_fetcher("ambientcg") is acg.fetch_catalog
    assert sources.get_catalog_fetcher("polyhaven") is ph.fetch_catalog

    # Resolved at call time, so monkeypatching the module attribute is honoured.
    sentinel = object()
    monkeypatch.setattr(acg, "download_texture_set", sentinel)
    assert sources.get_downloader("ambientcg") is sentinel


def test_asset_urls():
    assert sources.asset_url("polyhaven", "wood_floor_03") == "https://polyhaven.com/a/wood_floor_03"
    assert sources.asset_url("ambientcg", "Bricks104") == "https://ambientcg.com/a/Bricks104"
