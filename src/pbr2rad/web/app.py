"""FastAPI application factory for pbr2rad."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import tempdir
from .api import router as api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage temp directories and background cleanup."""
    cleanup_task = asyncio.create_task(tempdir.cleanup_loop())
    yield
    cleanup_task.cancel()
    tempdir.teardown()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="pbr2rad",
        description="Convert PBR texture sets to Radiance material libraries",
        version=__version__,
        lifespan=lifespan,
    )

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
