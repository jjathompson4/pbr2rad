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
    rotate_per_map: dict[str, int] = Field(default_factory=dict)
    flip_h: bool = False
    flip_v: bool = False
    # Web default caps .dat emission at 512px — visually indistinguishable
    # for normal/roughness perturbation, 16x smaller output, much faster.
    dat_resolution: int | None = Field(default=512, ge=64, le=2048)


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
    # Policy: 1k default, 2k ceiling. Nothing above 2k is ever needed for
    # Radiance materials, and larger fetches would swamp the small host.
    resolution: Literal["1k", "2k"] = "1k"
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
    # Source PBR channels actually consumed during conversion (subset of:
    # "albedo", "normal", "roughness", "metalness"). The UI uses this
    # to indicate which input maps fed the Radiance material.
    channels_used: list[str] = []
    # Channels synthesized from the albedo ("normal", "roughness") because
    # the source set didn't include them.
    channels_estimated: list[str] = []


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    radiance_available: bool
