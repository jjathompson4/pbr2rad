"""Pydantic request/response models for the pbr2rad web API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ConvertOptionsRequest(BaseModel):
    projection: Literal["uv", "planar", "box", "cylindrical", "spherical"] = "uv"
    planar_axis: Literal["xy", "xz", "yz"] = "xy"
    u_scale: float = 1.0
    v_scale: float = 1.0
    u_offset: float = 0.0
    v_offset: float = 0.0
    roughness_override: float | None = None
    metalness_override: float | None = None
    normal: bool = True
    bump_scale: float = 1.0
    varying_roughness: bool = True
    rough_modulation: float = 0.8
    estimate_maps: bool = True


class ChannelMap(BaseModel):
    """User-labeled channel assignment for one uploaded file."""
    filename: str
    channel: Literal[
        "albedo", "roughness", "metalness",
        "normal_gl", "normal_dx", "normal",
        "displacement", "ao",
    ]


class PolyHavenConvertRequest(BaseModel):
    slug: str
    resolution: Literal["1k", "2k", "4k", "8k"] = "2k"
    fmt: Literal["png", "jpg", "exr"] = "png"
    options: ConvertOptionsRequest = Field(default_factory=ConvertOptionsRequest)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DiscoverChannelResponse(BaseModel):
    filename: str
    channel: str | None  # None = unrecognized


class DiscoverResponse(BaseModel):
    name: str
    channels: list[DiscoverChannelResponse]
    extras: list[str]


class ConvertResponse(BaseModel):
    job_id: str
    name: str
    primitive: str
    roughness: float
    metalness: float
    avg_rgb: list[float]
    resolution: list[int]
    download_url: str
    preview_url: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    radiance_available: bool
