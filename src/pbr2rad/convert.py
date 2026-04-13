"""End-to-end conversion of one PBR set into a Radiance material folder."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import cal as cal_mod
from . import hdr as hdr_mod
from . import normal as normal_mod
from . import rad as rad_mod
from .discover import PBRSet


@dataclass
class ConvertOptions:
    projection: str = "uv"           # "uv" | "planar" | "box"
    planar_axis: str = "xy"          # for projection="planar"
    u_scale: float = 1.0
    v_scale: float = 1.0
    u_offset: float = 0.0
    v_offset: float = 0.0
    roughness_override: float | None = None
    metalness_override: float | None = None
    normal: bool = True              # use normal map if discovered
    bump_scale: float = 1.0          # normal map perturbation strength


@dataclass
class ConvertResult:
    name: str
    out_dir: Path
    hdr_file: Path
    cal_file: Path
    rad_file: Path
    width: int
    height: int
    avg_rgb: tuple[float, float, float]
    roughness: float
    metalness: float
    primitive: str


def convert_set(
    pbr: PBRSet,
    out_root: Path,
    opts: ConvertOptions | None = None,
) -> ConvertResult:
    """Convert one ``PBRSet`` into a Radiance material folder under ``out_root``.

    Produces ``<out_root>/<name>/<name>.{hdr,cal,rad}``.
    """
    opts = opts or ConvertOptions()
    if pbr.albedo is None:
        raise ValueError(f"PBR set {pbr.name!r} has no albedo map — cannot convert")

    out_dir = Path(out_root) / pbr.name
    out_dir.mkdir(parents=True, exist_ok=True)

    hdr_file = out_dir / f"{pbr.name}.hdr"
    cal_file = out_dir / f"{pbr.name}.cal"
    rad_file = out_dir / f"{pbr.name}.rad"

    # 1. Albedo → Radiance HDR
    width, height = hdr_mod.convert_ldr_to_hdr(pbr.albedo, hdr_file, srgb=True)
    avg_rgb = hdr_mod.average_rgb(pbr.albedo, srgb=True)

    # 2. Projection .cal
    cal_text = cal_mod.generate(
        opts.projection,
        axis=opts.planar_axis,  # type: ignore[arg-type]
        u_scale=opts.u_scale,
        v_scale=opts.v_scale,
        u_offset=opts.u_offset,
        v_offset=opts.v_offset,
    )
    cal_file.write_text(cal_text, encoding="ascii")

    # 3. Material parameters
    if opts.roughness_override is not None:
        roughness = opts.roughness_override
    elif pbr.roughness is not None:
        roughness = hdr_mod.average_gray(pbr.roughness)
    else:
        roughness = 0.5

    if opts.metalness_override is not None:
        metalness = opts.metalness_override
    elif pbr.metalness is not None:
        metalness = hdr_mod.average_gray(pbr.metalness)
    else:
        metalness = 0.0

    # 4. Normal map (optional)
    normal_kwargs: dict = {}
    if opts.normal and pbr.normal is not None:
        convention = normal_mod.detect_convention(pbr.maps)
        is_dx = convention == "dx"

        dat_r, dat_g, dat_b, _nw, _nh = normal_mod.convert_normal_to_dat(
            pbr.normal, out_dir, pbr.name,
        )
        normal_cal_name = f"{pbr.name}_normal.cal"
        # The normal .cal must include projection definitions (u, v)
        # because texdata's funcfile is the only .cal file it loads.
        normal_cal_text = cal_text + "\n" + normal_mod.generate_normal_cal(
            pbr.name, is_dx=is_dx,
        )
        (out_dir / normal_cal_name).write_text(normal_cal_text, encoding="ascii")

        normal_kwargs = dict(
            normal_dat_r=dat_r,
            normal_dat_g=dat_g,
            normal_dat_b=dat_b,
            normal_cal_file=normal_cal_name,
            bump_scale=opts.bump_scale,
            normal_is_dx=is_dx,
        )

    mat = rad_mod.MaterialParams(
        name=pbr.name,
        hdr_file=hdr_file.name,   # relative — resolved alongside the .rad file
        cal_file=cal_file.name,
        roughness=roughness,
        metalness=metalness,
        **normal_kwargs,
    )
    rad_file.write_text(rad_mod.generate(mat), encoding="ascii")

    return ConvertResult(
        name=pbr.name,
        out_dir=out_dir,
        hdr_file=hdr_file,
        cal_file=cal_file,
        rad_file=rad_file,
        width=width,
        height=height,
        avg_rgb=avg_rgb,
        roughness=roughness,
        metalness=metalness,
        primitive=mat.as_primitive(),
    )


def write_manifest(results: list[ConvertResult], out_root: Path) -> Path:
    """Write a ``manifest.json`` summarising a converted library."""
    manifest_path = Path(out_root) / "manifest.json"
    entries = []
    for r in results:
        entries.append({
            "name": r.name,
            "primitive": r.primitive,
            "files": {
                "rad": r.rad_file.relative_to(out_root).as_posix(),
                "cal": r.cal_file.relative_to(out_root).as_posix(),
                "hdr": r.hdr_file.relative_to(out_root).as_posix(),
            },
            "resolution": [r.width, r.height],
            "avg_linear_rgb": [round(c, 4) for c in r.avg_rgb],
            "roughness": round(r.roughness, 4),
            "metalness": round(r.metalness, 4),
        })
    manifest = {
        "generator": "pbr2rad",
        "version": 1,
        "materials": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


# Silence unused-import warning for asdict; kept for future callers.
_ = asdict
