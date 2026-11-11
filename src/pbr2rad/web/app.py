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
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import netsec, tempdir
from .admin import router as admin_router
from .api import router as api_router

CANONICAL_HOST = "pbr2rad.com"
# Already public in the page footer.
SECURITY_CONTACT = "jt@thompsonjeff.com"

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

    # The interactive API docs are a development aid; production sets
    # PBR2RAD_DISABLE_DOCS=1 (fly.toml) so the schema isn't published.
    docs_off = os.environ.get("PBR2RAD_DISABLE_DOCS") == "1"
    app = FastAPI(
        title="pbr2rad",
        description="Convert PBR texture sets to Radiance material libraries",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if docs_off else "/docs",
        redoc_url=None if docs_off else "/redoc",
        openapi_url=None if docs_off else "/openapi.json",
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

    # The page calls its own origin, so no CORS headers are needed. A dev
    # setup serving the frontend elsewhere can list its origins in
    # PBR2RAD_CORS_ORIGINS (comma-separated); "*" let any website drive the
    # converter from its visitors' browsers.
    cors_origins = [o.strip() for o in os.environ.get("PBR2RAD_CORS_ORIGINS", "").split(",") if o.strip()]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    # Registered last so it wraps everything above: refuse traffic that
    # skipped Cloudflare (when required), and put the security headers on
    # every response, error pages included.
    @app.middleware("http")
    async def edge_and_headers(request: Request, call_next):
        path = request.url.path
        if (
            netsec.cloudflare_required()
            and path != HEALTH_PATH
            and not netsec.came_through_cloudflare(request)
        ):
            response = PlainTextResponse("Forbidden", status_code=403)
        else:
            response = await call_next(request)
        netsec.apply_security_headers(path, response.headers)
        return response

    # API routes
    app.include_router(api_router)
    app.include_router(admin_router)

    @app.get("/.well-known/security.txt", include_in_schema=False)
    async def security_txt():
        expires = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 180 * 86400))
        return PlainTextResponse(
            f"Contact: mailto:{SECURITY_CONTACT}\n"
            f"Expires: {expires}\n"
            "Preferred-Languages: en\n"
            f"Canonical: https://{CANONICAL_HOST}/.well-known/security.txt\n"
        )

    # Static files (frontend)
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    # Root serves index.html
    @app.get("/")
    async def index():
        index_file = static_dir / "index.html"
        if index_file.exists():
            # Always revalidate the page (cheap: ETag). Without this, browsers
            # cached it heuristically for days, and after a deploy a stale
            # page could load the new /static/app.js (the server ignores ?v=).
            return FileResponse(index_file, headers={"Cache-Control": "no-cache"})
        return {"message": "pbr2rad API — see /docs for endpoints"}

    return app
