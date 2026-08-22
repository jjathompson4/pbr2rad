"""FastAPI application factory for pbr2rad."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import tempdir
from .api import router as api_router

CANONICAL_HOST = "pbr2rad.com"

# Fly's health checks must keep passing regardless of the Host header they send.
HEALTH_PATH = "/api/v1/health"


# Opt-in catalog pre-warm (set PBR2RAD_PREFETCH_CATALOGS=1 in deployment):
# the ambientCG catalog is ~5 paged API calls (~12 s cold), and it's the
# landing-page source, so warm it while the machine boots rather than on the
# first visitor's search. Off by default so tests/dev never touch the network
# at startup.
PREFETCH_ENV = "PBR2RAD_PREFETCH_CATALOGS"
_PREFETCH_SOURCES = ("ambientcg", "polyhaven")


def _prefetch_catalogs() -> None:
    from .api import _load_catalog

    for source in _PREFETCH_SOURCES:
        try:
            _load_catalog(source)
        except Exception:
            logging.getLogger("pbr2rad.web").warning(
                "catalog prefetch failed for %s", source, exc_info=True,
            )


# The download cache (Poly Haven maps, ambientCG packs + thumbnails) lives
# on the rootfs and is bounded by $PBR2RAD_CACHE_MAX_MB (fetch.py). Enforce
# it at boot — the rootfs may have persisted across stop/start — and then
# periodically; downloads also enforce it inline.
CACHE_SWEEP_SECONDS = 10 * 60


def _cache_budget_loop() -> None:
    from ..fetch import enforce_cache_budget

    log = logging.getLogger("pbr2rad.web")
    while True:
        try:
            enforce_cache_budget()
        except Exception:
            log.warning("cache budget sweep failed", exc_info=True)
        time.sleep(CACHE_SWEEP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage temp directories and background cleanup."""
    cleanup_task = asyncio.create_task(tempdir.cleanup_loop())
    if os.environ.get(PREFETCH_ENV) == "1":
        threading.Thread(target=_prefetch_catalogs, name="catalog-prefetch", daemon=True).start()
    threading.Thread(target=_cache_budget_loop, name="cache-budget", daemon=True).start()
    yield
    cleanup_task.cancel()
    tempdir.teardown()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Surface pbr2rad.web INFO+ messages (preview failures, temp cleanup)
    # under uvicorn, which only configures its own loggers.
    logging.basicConfig(level=logging.INFO)

    app = FastAPI(
        title="pbr2rad",
        description="Convert PBR texture sets to Radiance material libraries",
        version=__version__,
        lifespan=lifespan,
    )

    # The *.fly.dev hostname can't be removed and bypasses Cloudflare's WAF —
    # send anything arriving on it to the canonical domain instead.
    @app.middleware("http")
    async def redirect_fly_dev(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0].lower()
        if host.endswith(".fly.dev") and request.url.path != HEALTH_PATH:
            target = request.url.replace(scheme="https", netloc=CANONICAL_HOST)
            return RedirectResponse(str(target), status_code=301)
        return await call_next(request)

    # CORS — allow all origins for local dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(api_router)

    # Static files (frontend)
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Root serves index.html
    @app.get("/")
    async def index():
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"message": "pbr2rad API — see /docs for endpoints"}

    return app
