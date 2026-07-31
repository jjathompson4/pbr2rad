"""End-to-end conversion of one PBR set into a Radiance material folder."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cal as cal_mod
from . import estimate as estimate_mod
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
    varying_roughness: bool = True   # use roughness map for brightdata if discovered
    rough_modulation: float = 0.8    # how strongly roughness affects specular
    estimate_maps: bool = True       # estimate missing normal/roughness from albedo

    # Per-map rotation overrides (CCW degrees: 0, 90, 180, 270), keyed by
    # discover channel name ("albedo", "normal_gl", "normal_dx", "roughness",
    # "metalness", "ao", "displacement", "arm"). Maps without an entry keep
    # their original orientation. Pixel-level rotation only — heavy rotations
    # on normal maps may misalign lighting direction.
    rotate_per_map: dict[str, int] = field(default_factory=dict)
    # Global flip overrides, applied to all maps (escape hatch).
    flip_h: bool = False
    flip_v: bool = False


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
    # Source-PBR channels that actually fed the Radiance material
    # ("albedo", "normal", "roughness", "metalness"). Anything in the input
    # set but not in this list was ignored (e.g. ao, displacement, or maps
    # disabled via options).
    channels_used: list[str] = field(default_factory=list)


def _apply_orientation(
    pbr: PBRSet,
    out_dir: Path,
    opts: ConvertOptions,
) -> PBRSet:
    """Write rotated/flipped copies of every source map to ``out_dir`` and
    return a new ``PBRSet`` pointing at those copies. Original ``pbr.maps``
    paths are left untouched."""
    from PIL import Image

    xform_dir = out_dir / "_oriented"
    xform_dir.mkdir(exist_ok=True)

    new_maps: dict[str, Path] = {}
    for channel, src in pbr.maps.items():
        rot = opts.rotate_per_map.get(channel, 0)
        # No transforms for this map? Skip the re-encode and reuse the source.
        if rot == 0 and not opts.flip_h and not opts.flip_v:
            new_maps[channel] = src
            continue
        img = Image.open(src)
        if opts.flip_h:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if opts.flip_v:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        if rot:
            img = img.rotate(rot, expand=True)  # PIL rotate is CCW
        dst = xform_dir / src.name
        img.save(dst)
        new_maps[channel] = dst

    # Shallow copy with rewritten maps; keep name/root intact.
    new_pbr = PBRSet(name=pbr.name, root=pbr.root)
    new_pbr.maps = new_maps
    return new_pbr


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

    # The set name becomes a directory component; reject anything that could
    # escape out_root (the web layer passes user-supplied names through here).
    name = pbr.name
    if (
        not name
        or name in (".", "..")
        or any(ch in name for ch in ("/", "\\", ":", "\0"))
        or ".." in name
    ):
        raise ValueError(f"Invalid material name {name!r}")

    out_dir = Path(out_root) / pbr.name
    out_dir.mkdir(parents=True, exist_ok=True)

    # Apply per-map rotation + global flip overrides before any processing.
    # Pixel-level only — does not remap normal-vector channel values.
    if opts.rotate_per_map or opts.flip_h or opts.flip_v:
        pbr = _apply_orientation(pbr, out_dir, opts)

    hdr_file = out_dir / f"{pbr.name}.hdr"
    cal_file = out_dir / f"{pbr.name}.cal"
    rad_file = out_dir / f"{pbr.name}.rad"

    channels_used: list[str] = []

    # 1. Metalness picks plastic vs metal; both drive the specular term we
    #    must reserve texture headroom for, so determine it first.
    if opts.metalness_override is not None:
        metalness = opts.metalness_override
    elif pbr.metalness is not None:
        metalness = hdr_mod.average_gray(pbr.metalness)
        channels_used.append("metalness")
    else:
        metalness = 0.0

    # Reserve headroom for the specular term to guarantee energy
    # conservation at every texel — see pbr2rad-audit/audit_summary.md.
    # Plastic adds a constant spec on top of the diffuse pattern, so we
    # pre-scale the .hdr by (1 - spec) and keep R=G=B=1 in the .rad.
    # Metal uses the pattern colour itself as the specular reflectance
    # (no additive term), so no scaling is needed.
    spec = rad_mod.default_specularity(metalness)
    albedo_scale = 1.0 if metalness >= 0.5 else (1.0 - spec)

    # 2. Albedo → Radiance HDR
    width, height = hdr_mod.convert_ldr_to_hdr(
        pbr.albedo, hdr_file, srgb=True, scale=albedo_scale,
    )
    avg_rgb = hdr_mod.average_rgb(pbr.albedo, srgb=True)
    channels_used.append("albedo")

    # 3. Projection .cal
    cal_text = cal_mod.generate(
        opts.projection,
        axis=opts.planar_axis,  # type: ignore[arg-type]
        u_scale=opts.u_scale,
        v_scale=opts.v_scale,
        u_offset=opts.u_offset,
        v_offset=opts.v_offset,
    )
    cal_file.write_text(cal_text, encoding="ascii")

    # 3b. Estimate missing maps from albedo (if enabled)
    if opts.estimate_maps:
        if pbr.normal is None and opts.normal:
            est_normal = out_dir / f"{pbr.name}_est_nor_gl.png"
            estimate_mod.estimate_normal(pbr.albedo, est_normal, strength=opts.bump_scale)
            pbr.maps["normal_gl"] = est_normal
        if pbr.roughness is None and opts.varying_roughness:
            est_rough = out_dir / f"{pbr.name}_est_rough.png"
            estimate_mod.estimate_roughness(pbr.albedo, est_rough)
            pbr.maps["roughness"] = est_rough

    # 3. Material parameters
    if opts.roughness_override is not None:
        roughness = opts.roughness_override
    elif pbr.roughness is not None:
        roughness = hdr_mod.average_gray(pbr.roughness)
        # Mark roughness as used (for the scalar average). The varying-
        # roughness path below may also use it — channels_used is a set
        # semantically, so we de-duplicate at the end.
        channels_used.append("roughness")
    else:
        roughness = 0.5

    # 4. Normal map (optional)
    normal_kwargs: dict = {}
    if opts.normal and pbr.normal is not None:
        convention = normal_mod.detect_convention(pbr.maps)
        is_dx = convention == "dx"
        channels_used.append("normal")

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

    # 5. Spatially varying roughness (optional)
    rough_kwargs: dict = {}
    if opts.varying_roughness and pbr.roughness is not None:
        # Roughness already in channels_used from the scalar-average step;
        # the varying-roughness path consumes the same source map.
        rough_dat, _rw, _rh = normal_mod.convert_roughness_to_dat(
            pbr.roughness, out_dir, pbr.name,
        )
        rough_cal_name = f"{pbr.name}_rough.cal"
        # Roughness .cal needs projection definitions (u, v) just like normal .cal
        rough_cal_text = cal_text + "\n" + normal_mod.generate_roughness_cal(pbr.name)
        (out_dir / rough_cal_name).write_text(rough_cal_text, encoding="ascii")

        rough_kwargs = dict(
            rough_dat=rough_dat,
            rough_cal_file=rough_cal_name,
            rough_modulation=opts.rough_modulation,
        )

    mat = rad_mod.MaterialParams(
        name=pbr.name,
        hdr_file=hdr_file.name,   # relative — resolved alongside the .rad file
        cal_file=cal_file.name,
        roughness=roughness,
        metalness=metalness,
        **normal_kwargs,
        **rough_kwargs,
    )
    rad_file.write_text(rad_mod.generate(mat), encoding="ascii")

    # De-duplicate channels_used while preserving insertion order.
    seen: set[str] = set()
    channels_used = [c for c in channels_used if not (c in seen or seen.add(c))]

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
        channels_used=channels_used,
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
