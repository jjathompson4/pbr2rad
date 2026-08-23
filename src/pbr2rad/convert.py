"""End-to-end conversion of one PBR set into a Radiance material folder."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import cal as cal_mod
from . import estimate as estimate_mod
from . import hdr as hdr_mod
from . import normal as normal_mod
from . import pvw as pvw_mod
from . import rad as rad_mod
from .discover import PBRSet
from .fetch import SIDECAR_NAME

log = logging.getLogger("pbr2rad.convert")


@dataclass
class ConvertOptions:
    projection: str = "uv"           # "uv" | "planar" | "box" | "cylindrical" | "spherical"
    planar_axis: str = "xy"          # for projection="planar"
    u_scale: float = 1.0
    v_scale: float = 1.0
    u_offset: float = 0.0
    v_offset: float = 0.0
    roughness_override: float | None = None
    metalness_override: float | None = None
    normal: bool = True              # use normal map if discovered
    bump_scale: float = 1.0          # normal map perturbation strength
    # Roughness map → brightdata pattern. OFF by default: a Radiance pattern
    # scales the material COLOUR (the diffuse reflectance of a plastic, all of
    # a metal), not its roughness — so this darkens the material by up to
    # ``rough_modulation`` where the map is rough. Kept as an explicit opt-in
    # for back-compat; true spatially varying roughness needs a mixdata of two
    # plastics (roadmap).
    varying_roughness: bool = False
    rough_modulation: float = 0.8    # how strongly roughness affects specular
    estimate_maps: bool = True       # estimate missing normal/roughness from albedo
    # Cap the longest edge of normal/roughness .dat emission (None = native).
    # .dat files are ASCII and Radiance parses them at render time; detail
    # above ~512px is visually indistinguishable for perturbation data while
    # costing 16x the text per doubling.
    dat_resolution: int | None = None

    # Per-map rotation overrides (CCW degrees: 0, 90, 180, 270), keyed by
    # discover channel name ("albedo", "normal_gl", "normal_dx", "roughness",
    # "metalness", "ao", "displacement", "arm"). Maps without an entry keep
    # their original orientation. Pixel-level rotation only — heavy rotations
    # on normal maps may misalign lighting direction.
    rotate_per_map: dict[str, int] = field(default_factory=dict)
    # Global flip overrides, applied to all maps (escape hatch).
    flip_h: bool = False
    flip_v: bool = False

    # Ship a ClimateStudio preview file alongside the .rad (see pvw.py).
    write_pvw: bool = True

    # Material tuning. ``specularity_override`` replaces the primitive's
    # default specular reflectance (plastic 0.05, metal 1.0);
    # ``diffuse_scale`` multiplies the albedo (clamped so the shipped pattern
    # never exceeds 100 % reflectance). Both are what the web Output-panel
    # sliders drive via re-render.
    specularity_override: float | None = None
    diffuse_scale: float = 1.0

    # Also write a preview-only material chain ``preview_<name>.{rad,cal,…}``
    # next to the exported one: same .hdr/.dat files, but wrapped on a sphere
    # the way the texture sites render their reference spheres
    # (``REFERENCE_WRAPS``). Never shipped (the web zip filter drops the
    # ``preview_`` prefix) and off by default, so CLI output and the golden
    # hashes are untouched. The web preview renders it instead of the
    # exported projection so the side-by-side compares like with like.
    write_preview_variant: bool = False
    # Which site's wrap convention the preview variant uses ("ambientcg",
    # "polyhaven"); None → the default (ambientCG's). Uploads use the default.
    preview_wrap_source: str | None = None


@dataclass(frozen=True)
class ProjectionSpec:
    """How a material chain maps texture space onto geometry (one .cal)."""
    projection: str
    axis: str = "xy"
    u_scale: float = 1.0
    v_scale: float = 1.0
    u_offset: float = 0.0
    v_offset: float = 0.0
    swap_uv: bool = False      # spherical only: transposed wrap (see cal.spherical)


PREVIEW_VARIANT_PREFIX = "preview_"

# How the texture sites wrap a texture on their reference spheres:
# equirectangular on a UV sphere, poles on Z, a fixed **3 repeats around and
# 1.5 pole-to-pole** (square texels at the equator; not physical-size based —
# Bricks058 at 105 cm and Tiles141 at 200 cm show the same density).
# Measured 2026-08-22 by unwrapping ambientCG's renders: grout/mortar lines
# every ~20° of longitude and latitude (Tiles141: 6 tiles per repeat → 18
# around / 9 pole-to-pole; Bricks058: ~17 courses per repeat → 25 per 180°).
# Poly Haven renders the same way (same density, picture un-rotated: a 2026-08-23
# survey of 121 directional assets — planks, veneers, corrugated iron, brick
# walls, tiles — found the texture's orientation preserved on 118, none rotated,
# 3 unclear; only brick_floor_003's thumbnail disagrees with its own albedo,
# an outlier; scripts/polyhaven_orientation_survey.py). The
# web preview sphere is a unit sphere at the world origin — exactly what the
# spherical .cal is centred on. Per-source entries stay so a source with a
# different convention can be added without touching the callers.
REFERENCE_WRAPS: dict[str | None, ProjectionSpec] = {
    "ambientcg": ProjectionSpec(projection="spherical", axis="xy", u_scale=3.0, v_scale=1.5),
    "polyhaven": ProjectionSpec(projection="spherical", axis="xy", u_scale=3.0, v_scale=1.5),
}
REFERENCE_WRAP = REFERENCE_WRAPS["ambientcg"]      # default (uploads, unknown sources)


def reference_wrap(source: str | None) -> ProjectionSpec:
    """The preview-sphere wrap for a texture source (default: ambientCG's)."""
    return REFERENCE_WRAPS.get((source or "").lower(), REFERENCE_WRAP)


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
    # ClimateStudio preview file, when one was written.
    pvw_file: Path | None = None
    # ``preview_<name>.rad`` when ``ConvertOptions.write_preview_variant`` is
    # set — the reference-wrap chain for the web preview sphere.
    preview_rad_file: Path | None = None
    # Source-PBR channels that actually fed the Radiance material
    # ("albedo", "normal", "roughness", "metalness"). Anything in the input
    # set but not in this list was ignored (e.g. ao, displacement, or maps
    # disabled via options).
    channels_used: list[str] = field(default_factory=list)
    # Channels synthesized from the albedo because the source set lacked
    # them ("normal", "roughness"). Never overlaps channels_used.
    channels_estimated: list[str] = field(default_factory=list)
    # Provenance read from the fetcher's ``pbr2rad_source.json`` sidecar
    # (source, asset_id, asset_url, license, …), or None for local sets.
    source: dict | None = None
    # Effective material characteristics as emitted (after overrides):
    # specular reflectance, Radiance roughness (alpha = perceptual²), the
    # diffuse multiplier in force, and the photopic reflectance split.
    specularity: float = 0.05
    roughness_radiance: float = 0.0
    diffuse_scale: float = 1.0
    reflectance: dict = field(default_factory=dict)
    avg_srgb_hex: str = "#000000"


def material_reflectance(
    avg_rgb: tuple[float, float, float],
    *,
    primitive: str,
    specularity: float,
    diffuse_scale: float = 1.0,
) -> dict:
    """Reflectance split of the emitted material, Radiance semantics.

    With the pattern colour ``C = avg_rgb × diffuse_scale`` (what the ``.hdr``
    carries) and the ``.rad`` colour at ``1 1 1``:

    * plastic: diffuse = C × (1 − spec), specular = spec (uncoloured);
    * metal:   diffuse = C × (1 − spec), specular = C × spec.

    Visible (photopic) values use Radiance's 0.265/0.670/0.065 weights — the
    "VLR" numbers ClimateStudio would show for an untextured material.
    """
    spec = max(0.0, min(1.0, float(specularity)))
    c = [min(1.0, max(0.0, float(v) * diffuse_scale)) for v in avg_rgb]
    diffuse = [v * (1.0 - spec) for v in c]
    specular = [v * spec for v in c] if primitive == "metal" else [spec] * 3
    dv = hdr_mod.visible(diffuse)
    sv = hdr_mod.visible(specular)
    return {
        "diffuse_rgb": [round(v, 4) for v in diffuse],
        "specular_rgb": [round(v, 4) for v in specular],
        "diffuse_vis": round(dv, 4),
        "specular_vis": round(sv, 4),
        "total_vis": round(dv + sv, 4),
    }


def _read_source_sidecar(root: Path) -> dict | None:
    """Return the provenance sidecar written by ``pbr2rad fetch``, if any.

    Best-effort: a missing or malformed sidecar never fails a conversion.
    """
    try:
        path = Path(root) / SIDECAR_NAME
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("generator") != "pbr2rad":
        return None
    if not data.get("source"):
        return None
    return data


def _source_note(source: dict | None) -> str | None:
    """One-line provenance for the .rad header, e.g.
    ``ambientCG Bricks104 (CC0-1.0) https://ambientcg.com/a/Bricks104``."""
    if not source:
        return None
    parts = [str(source.get("source_name") or source.get("source"))]
    if source.get("asset_id"):
        parts.append(str(source["asset_id"]))
    if source.get("license"):
        parts.append(f"({source['license']})")
    if source.get("asset_url"):
        parts.append(str(source["asset_url"]))
    return " ".join(parts)


def _apply_orientation(
    pbr: PBRSet,
    work_dir: Path,
    opts: ConvertOptions,
) -> PBRSet:
    """Write rotated/flipped copies of every source map to ``work_dir`` and
    return a new ``PBRSet`` pointing at those copies. Original ``pbr.maps``
    paths are left untouched."""
    from PIL import Image

    xform_dir = work_dir / "oriented"
    xform_dir.mkdir(parents=True, exist_ok=True)

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
            # Exact 90-degree steps are pure memory shuffles (CCW, matching
            # PIL rotate); anything else falls back to the affine rotate.
            transpose = {
                90: Image.ROTATE_90, 180: Image.ROTATE_180, 270: Image.ROTATE_270,
            }.get(rot % 360)
            if transpose is not None:
                img = img.transpose(transpose)
            else:
                img = img.rotate(rot, expand=True)  # PIL rotate is CCW
        # Always re-encode as PNG: saving through the source suffix would
        # lossily re-encode JPEG data maps on every conversion.
        dst = xform_dir / f"{src.stem}.png"
        img.save(dst)
        new_maps[channel] = dst

    # Shallow copy with rewritten maps; keep name/root/extras intact.
    return PBRSet(
        name=pbr.name, root=pbr.root, maps=new_maps, extras=list(pbr.extras)
    )


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

    # Work on a copy: estimation and orientation rewrite ``maps``, and
    # mutating the caller's PBRSet makes retries and batch reuse silently
    # consume a previous run's intermediates.
    pbr = PBRSet(
        name=pbr.name, root=pbr.root, maps=dict(pbr.maps), extras=list(pbr.extras)
    )

    # Intermediates (oriented copies, estimated maps) live in a scratch dir
    # that never ships: leaving them in the output pollutes the material
    # folder and — worse — re-running discover() on an output folder picks
    # up ``*_est_nor_gl.png`` as a source normal map.
    work_dir = out_dir / ".pbr2rad_work"
    try:
        return _convert_set_inner(pbr, out_dir, work_dir, opts)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@dataclass(frozen=True)
class _ChainInputs:
    """Everything the exported chain and the preview variant share."""
    hdr_name: str
    pic_u_scale: float
    pic_v_scale: float
    roughness: float
    metalness: float
    specularity: float
    source_note: str | None
    normal_dats: tuple[str, str, str] | None   # (r, g, b) bare .dat names
    normal_is_dx: bool
    bump_scale: float
    rough_dat: str | None
    rough_modulation: float


def _write_material_chain(
    out_dir: Path, stem: str, proj: ProjectionSpec, shared: _ChainInputs,
) -> tuple[Path, Path, rad_mod.MaterialParams]:
    """Write ``<stem>.cal`` [, ``<stem>_normal.cal``, ``<stem>_rough.cal``] and
    ``<stem>.rad`` for one projection. Primitive ids derive from ``stem``;
    the .hdr/.dat files are referenced by name and shared between chains.
    Returns ``(cal_file, rad_file, params)``."""
    cal_file = out_dir / f"{stem}.cal"
    rad_file = out_dir / f"{stem}.rad"
    cal_text = cal_mod.generate(
        proj.projection,
        axis=proj.axis,  # type: ignore[arg-type]
        u_scale=proj.u_scale,
        v_scale=proj.v_scale,
        u_offset=proj.u_offset,
        v_offset=proj.v_offset,
        pic_u_scale=shared.pic_u_scale,
        pic_v_scale=shared.pic_v_scale,
        swap_uv=proj.swap_uv,
    )
    cal_file.write_text(cal_text, encoding="ascii")

    kwargs: dict = {}
    if shared.normal_dats is not None:
        normal_cal_name = f"{stem}_normal.cal"
        # The normal .cal must include projection definitions (u, v)
        # because texdata's funcfile is the only .cal file it loads.
        normal_cal_text = cal_text + "\n" + normal_mod.generate_normal_cal(
            stem, is_dx=shared.normal_is_dx,
        )
        (out_dir / normal_cal_name).write_text(normal_cal_text, encoding="ascii")
        dat_r, dat_g, dat_b = shared.normal_dats
        kwargs.update(
            normal_dat_r=dat_r,
            normal_dat_g=dat_g,
            normal_dat_b=dat_b,
            normal_cal_file=normal_cal_name,
            bump_scale=shared.bump_scale,
            normal_is_dx=shared.normal_is_dx,
        )
    if shared.rough_dat is not None:
        rough_cal_name = f"{stem}_rough.cal"
        # Roughness .cal needs projection definitions (u, v) just like normal .cal
        rough_cal_text = cal_text + "\n" + normal_mod.generate_roughness_cal(stem)
        (out_dir / rough_cal_name).write_text(rough_cal_text, encoding="ascii")
        kwargs.update(
            rough_dat=shared.rough_dat,
            rough_cal_file=rough_cal_name,
            rough_modulation=shared.rough_modulation,
        )

    mat = rad_mod.MaterialParams(
        name=stem,
        hdr_file=shared.hdr_name,   # relative — resolved alongside the .rad file
        cal_file=cal_file.name,
        roughness=shared.roughness,
        metalness=shared.metalness,
        specularity=shared.specularity,
        source_note=shared.source_note,
        **kwargs,
    )
    rad_file.write_text(rad_mod.generate(mat), encoding="ascii")
    return cal_file, rad_file, mat


def _convert_set_inner(
    pbr: PBRSet,
    out_dir: Path,
    work_dir: Path,
    opts: ConvertOptions,
) -> ConvertResult:
    # Apply per-map rotation + global flip overrides before any processing.
    # Pixel-level only — does not remap normal-vector channel values.
    if opts.rotate_per_map or opts.flip_h or opts.flip_v:
        pbr = _apply_orientation(pbr, work_dir, opts)

    hdr_file = out_dir / f"{pbr.name}.hdr"

    channels_used: list[str] = []
    channels_estimated: list[str] = []
    had_normal = pbr.normal is not None
    had_rough = pbr.roughness is not None

    # 1. Metalness picks plastic vs metal; both drive the specular term we
    #    must reserve texture headroom for, so determine it first.
    if opts.metalness_override is not None:
        metalness = opts.metalness_override
    elif pbr.metalness is not None:
        metalness = hdr_mod.average_gray(pbr.metalness)
        channels_used.append("metalness")
    else:
        metalness = 0.0

    # Specular reflectance: the primitive default unless overridden. The
    # .rad keeps R=G=B=1 and the .hdr carries the colour pattern C; Radiance
    # itself (normal.c: rdiff = 1 - rspec) makes the diffuse term C×(1-spec)
    # for both plastic and metal and the specular term spec (plastic,
    # uncoloured) or C×spec (metal), so C ≤ 1 already conserves energy at
    # every texel — no extra headroom scaling is needed (an earlier version
    # pre-scaled by 1-spec on top, darkening plastics by 5 %). The optional
    # diffuse multiplier is applied here and clipped at 1.0.
    if opts.specularity_override is not None:
        spec = max(0.0, min(1.0, float(opts.specularity_override)))
    else:
        spec = rad_mod.default_specularity(metalness)
    diffuse_scale = max(0.0, float(opts.diffuse_scale))
    albedo_scale = diffuse_scale

    # 2. Albedo → Radiance HDR
    width, height = hdr_mod.convert_ldr_to_hdr(
        pbr.albedo, hdr_file, srgb=True, scale=albedo_scale, clip=1.0,
    )
    avg_rgb = hdr_mod.average_rgb(pbr.albedo, srgb=True)
    channels_used.append("albedo")

    # 3. Projection: the albedo's aspect feeds the colorpict lookup so
    #    non-square textures (common on ambientCG, e.g. 1024x512) don't get
    #    their picture sampled over half the tile while the .dat maps span it.
    #    The .cal/.rad files themselves are written by _write_material_chain
    #    once the .dat maps are known (their .cal files embed the projection).
    pic_u_scale, pic_v_scale = cal_mod.picture_scales(width, height)

    # 3b. Estimate missing maps from albedo (if enabled). Estimated PNGs are
    # scratch files — consumed by the .dat converters below, never shipped.
    if opts.estimate_maps:
        work_dir.mkdir(parents=True, exist_ok=True)
        if pbr.normal is None and opts.normal:
            est_normal = work_dir / f"{pbr.name}_est_nor_gl.png"
            estimate_mod.estimate_normal(pbr.albedo, est_normal, strength=opts.bump_scale)
            pbr.maps["normal_gl"] = est_normal
        if pbr.roughness is None and opts.roughness_override is None:
            # The estimate feeds the scalar roughness (and the legacy
            # brightdata path when varying_roughness is on).
            est_rough = work_dir / f"{pbr.name}_est_rough.png"
            estimate_mod.estimate_roughness(pbr.albedo, est_rough)
            pbr.maps["roughness"] = est_rough

    # 3. Material parameters
    if opts.roughness_override is not None:
        roughness = opts.roughness_override
    elif pbr.roughness is not None:
        roughness = hdr_mod.average_gray(pbr.roughness)
        # Mark roughness as source-used only if the set actually shipped a
        # roughness map; a synthesized one is reported as estimated instead.
        # The varying-roughness path below consumes the same map —
        # channels_used is a set semantically, so we de-duplicate at the end.
        if had_rough:
            channels_used.append("roughness")
        else:
            channels_estimated.append("roughness")
    else:
        roughness = 0.5

    # 4. Normal map (optional) → .dat files (the expensive part; shared by
    #    every chain written below)
    normal_dats: tuple[str, str, str] | None = None
    normal_is_dx = False
    if opts.normal and pbr.normal is not None:
        convention = normal_mod.detect_convention(pbr.maps)
        normal_is_dx = convention == "dx"
        if had_normal:
            channels_used.append("normal")
        else:
            channels_estimated.append("normal")

        dat_r, dat_g, dat_b, _nw, _nh = normal_mod.convert_normal_to_dat(
            pbr.normal, out_dir, pbr.name, max_size=opts.dat_resolution,
        )
        normal_dats = (dat_r, dat_g, dat_b)

    # 5. Spatially varying roughness (optional) → .dat
    rough_dat: str | None = None
    if opts.varying_roughness and pbr.roughness is not None:
        # Roughness already in channels_used from the scalar-average step;
        # the varying-roughness path consumes the same source map.
        rough_dat, _rw, _rh = normal_mod.convert_roughness_to_dat(
            pbr.roughness, out_dir, pbr.name, max_size=opts.dat_resolution,
        )

    source = _read_source_sidecar(pbr.root)

    # 6. The material chains: the exported one (the user's projection) and,
    #    when asked, the reference-wrap preview variant.
    shared = _ChainInputs(
        hdr_name=hdr_file.name,
        pic_u_scale=pic_u_scale,
        pic_v_scale=pic_v_scale,
        roughness=roughness,
        metalness=metalness,
        specularity=spec,
        source_note=_source_note(source),
        normal_dats=normal_dats,
        normal_is_dx=normal_is_dx,
        bump_scale=opts.bump_scale,
        rough_dat=rough_dat,
        rough_modulation=opts.rough_modulation,
    )
    exported = ProjectionSpec(
        projection=opts.projection,
        axis=opts.planar_axis,
        u_scale=opts.u_scale,
        v_scale=opts.v_scale,
        u_offset=opts.u_offset,
        v_offset=opts.v_offset,
    )
    cal_file, rad_file, mat = _write_material_chain(out_dir, pbr.name, exported, shared)
    preview_rad_file: Path | None = None
    if opts.write_preview_variant:
        wrap = reference_wrap(opts.preview_wrap_source or (source or {}).get("source"))
        _, preview_rad_file, _ = _write_material_chain(
            out_dir, f"{PREVIEW_VARIANT_PREFIX}{pbr.name}", wrap, shared,
        )
    reflectance = material_reflectance(
        avg_rgb, primitive=mat.as_primitive(), specularity=spec,
        diffuse_scale=diffuse_scale,
    )

    # 6. ClimateStudio preview file. The albedo swatch is the always-available
    #    source; callers with a renderer (the web app) overwrite the .pvw with
    #    a rendered preview afterwards. A failure here must not lose the
    #    material — the .rad is already complete and usable without it.
    pvw_file: Path | None = None
    if opts.write_pvw:
        candidate = out_dir / f"{pbr.name}.pvw"
        try:
            pvw_mod.write_pvw(
                candidate, pbr.name, pvw_mod.make_preview_png(pbr.albedo)
            )
            pvw_file = candidate
        except Exception:
            log.warning(
                "preview (.pvw) generation failed for %s", pbr.name, exc_info=True,
            )

    # De-duplicate channels_used while preserving insertion order.
    seen: set[str] = set()
    channels_used = [c for c in channels_used if not (c in seen or seen.add(c))]
    seen.clear()
    channels_estimated = [
        c for c in channels_estimated if not (c in seen or seen.add(c))
    ]

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
        pvw_file=pvw_file,
        preview_rad_file=preview_rad_file,
        channels_used=channels_used,
        channels_estimated=channels_estimated,
        source=source,
        specularity=spec,
        roughness_radiance=rad_mod._rad_roughness(roughness),
        diffuse_scale=diffuse_scale,
        reflectance=reflectance,
        avg_srgb_hex=hdr_mod.srgb_hex(
            [min(1.0, c * diffuse_scale) for c in avg_rgb]
        ),
    )


def write_manifest(results: list[ConvertResult], out_root: Path) -> Path:
    """Write a ``manifest.json`` summarising a converted library."""
    manifest_path = Path(out_root) / "manifest.json"
    entries = []
    for r in results:
        # os.path.relpath instead of Path.relative_to: tolerant of mixed
        # relative/absolute inputs and macOS /tmp vs /private/tmp aliasing.
        def _rel(p: Path) -> str:
            return Path(os.path.relpath(p, out_root)).as_posix()

        files = {
            "rad": _rel(r.rad_file),
            "cal": _rel(r.cal_file),
            "hdr": _rel(r.hdr_file),
        }
        if r.pvw_file is not None:
            files["pvw"] = _rel(r.pvw_file)

        entries.append({
            "name": r.name,
            "primitive": r.primitive,
            "files": files,
            "resolution": [r.width, r.height],
            # Mean linear RGB of the SOURCE albedo (before any diffuse_scale).
            "avg_linear_rgb": [round(c, 4) for c in r.avg_rgb],
            "roughness": round(r.roughness, 4),
            "metalness": round(r.metalness, 4),
            # Emitted material characteristics (Radiance semantics; the
            # "VLR" numbers a material browser can't read off a textured chain).
            "specularity": round(r.specularity, 4),
            "roughness_radiance": round(r.roughness_radiance, 4),
            "diffuse_scale": round(r.diffuse_scale, 4),
            "reflectance": r.reflectance,
            "avg_srgb_hex": r.avg_srgb_hex,
        })
        # Provenance (source, asset_id, asset_url, license, …) when the set
        # came from ``pbr2rad fetch``. Additive — omitted for local sets.
        if getattr(r, "source", None):
            entries[-1]["source"] = r.source
    manifest = {
        "generator": "pbr2rad",
        "version": 1,
        "materials": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path
