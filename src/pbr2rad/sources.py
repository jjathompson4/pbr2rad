"""Registry of the texture sources pbr2rad can fetch from.

Deliberately small: two entries and a handful of lookups. Each source is a
module exposing ``download_texture_set(asset_id, output_dir, *, resolution,
fmt, verbose) -> Path`` and ``fetch_catalog() -> list[dict]``; the modules are
resolved lazily by name so importing this registry never drags in a client,
and so tests can monkeypatch ``pbr2rad.<module>.download_texture_set`` and
still be dispatched to.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

from .fetch import SIDECAR_NAME  # noqa: F401  (re-exported for convenience)


@dataclass(frozen=True)
class TextureSource:
    key: str
    display_name: str
    home_url: str
    license: str
    module: str                  # dotted module path, resolved lazily
    formats: tuple[str, ...]     # image formats the source can deliver
    web_default_fmt: str         # what the web UI requests by default
    asset_url_tmpl: str          # "{id}" → public asset page
    catalog_file: str            # filename under the source's cache dir
    catalog_ttl: int             # seconds before the web layer re-fetches
    id_hint: str                 # example asset id for help text / placeholders

    def asset_url(self, asset_id: str) -> str:
        return self.asset_url_tmpl.format(id=asset_id)


SOURCES: dict[str, TextureSource] = {
    "polyhaven": TextureSource(
        key="polyhaven",
        display_name="Poly Haven",
        home_url="https://polyhaven.com",
        license="CC0-1.0",
        module="pbr2rad.fetch",
        formats=("png", "jpg", "exr"),
        web_default_fmt="png",
        asset_url_tmpl="https://polyhaven.com/a/{id}",
        # v2 made the on-disk catalog the normalized entry list (it used to
        # be Poly Haven's raw slug→dict response); v3 added the large/dark
        # preview fields. Older files are simply ignored.
        catalog_file="catalog.v3.json",
        catalog_ttl=3600,
        id_hint="wood_floor_03",
    ),
    "ambientcg": TextureSource(
        key="ambientcg",
        display_name="ambientCG",
        home_url="https://ambientcg.com",
        license="CC0-1.0",
        module="pbr2rad.ambientcg",
        formats=("png", "jpg"),
        # 1K-JPG packs are ~4 MB vs ~9 MB for PNG; plenty for .hdr/.dat
        # emission and kinder to the small public host.
        web_default_fmt="jpg",
        asset_url_tmpl="https://ambientcg.com/a/{id}",
        catalog_file="catalog.v3.json",
        # The ambientCG API self-describes as hobby-grade and the catalog
        # changes slowly — re-fetch rarely.
        catalog_ttl=6 * 3600,
        id_hint="Bricks104",
    ),
}

DEFAULT_SOURCE = "polyhaven"


def get_source(key: str) -> TextureSource:
    try:
        return SOURCES[key]
    except KeyError:
        valid = ", ".join(sorted(SOURCES))
        raise ValueError(f"unknown texture source {key!r} (valid: {valid})") from None


def get_downloader(key: str) -> Callable[..., object]:
    """Return the source's ``download_texture_set`` (resolved at call time)."""
    module = importlib.import_module(get_source(key).module)
    return getattr(module, "download_texture_set")


def get_catalog_fetcher(key: str) -> Callable[[], list[dict]]:
    """Return the source's ``fetch_catalog`` (resolved at call time)."""
    module = importlib.import_module(get_source(key).module)
    return getattr(module, "fetch_catalog")


def asset_url(key: str, asset_id: str) -> str:
    return get_source(key).asset_url(asset_id)
