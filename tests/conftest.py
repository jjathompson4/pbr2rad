"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_download_cache(tmp_path, monkeypatch):
    """Never let a test read or write the developer's real ~/.cache/pbr2rad.

    Every download-cache consumer (Poly Haven per-file cache, ambientCG zips
    and thumbnails, the web catalog files, the cache-budget sweeper) resolves
    its root through $PBR2RAD_CACHE_DIR, so one env var isolates them all.
    """
    monkeypatch.setenv("PBR2RAD_CACHE_DIR", str(tmp_path / "pbr2rad-cache"))
    # Budget eviction walks are memoised; start each test with a clean memo.
    from pbr2rad import fetch

    fetch._USAGE_MEMO.update(root=None, at=0.0, files=[])
    yield
