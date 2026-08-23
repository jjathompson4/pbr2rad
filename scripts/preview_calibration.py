#!/usr/bin/env python
"""Preview-sphere calibration loop: render 7 ambientCG reference materials with the
CURRENT ``pbr2rad.web.preview`` constants and compare against ambientCG's own sphere
renders (mean RGB inside the sphere, upper/lower-half luminance ratio), plus a
comparison sheet.

Needs a local Radiance (``rpict`` etc. on PATH, RAYPATH including the Radiance lib,
e.g. ``RAYPATH=.:/usr/local/radiance/lib``) and network access the first time (it
fetches the 1K-JPG packs through pbr2rad's ambientCG cache and the 512 px reference
thumbnails).

    PYTHONPATH=src .venv/bin/python scripts/preview_calibration.py --tag try1
    PYTHONPATH=src .venv/bin/python scripts/preview_calibration.py --tag wrap --mode wrap

``--mode exported`` (default) renders the exported chain exactly as the web app does on
``main``; ``--mode wrap`` renders the reference-wrap variant (needs
``ConvertOptions.write_preview_variant`` — branch ``preview-reference-wrap``).
Rig experiments without editing code: ``RIG_ROT=330 RIG_GAIN=1.2 RIG_SAT=1.3``
override the matching ``preview`` constants for this run.

Acceptance targets used so far: mean |Δlog L| < 0.15 over concrete/tiles/bricks,
up/lo 1.1–1.4, every light grey (rig_neutrality <= 1.01), fixed_exposure() in 1–6.
See docs/preview-scale-notes.md.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from pbr2rad import ambientcg
from pbr2rad.convert import ConvertOptions, convert_set
from pbr2rad.discover import discover
from pbr2rad.web import preview

IDS = ["WoodFloor052", "Concrete034", "Tiles141", "Grass005", "Bricks104", "Fabric030", "Metal049A"]
# Materials whose reference differs from ours for material-shading reasons (wood
# warmth, fabric sheen, grass translucency, chrome) — reported but not scored.
UNSCORED = {"WoodFloor052", "Fabric030", "Grass005", "Metal049A"}
REF_URL = "https://acg-media.struffelproductions.com/file/ambientCG-Web/media/thumbnail/512-JPG-242424/{aid}.jpg"
UA = "pbr2rad/0.1 (+https://pbr2rad.com)"


def lin(c):
    return (np.asarray(c, dtype=float) / 255.0) ** 2.2


def lum(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def disk(arr, frac):
    h, w = arr.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    return (xx - (w - 1) / 2) ** 2 + (yy - (h - 1) / 2) ** 2 <= (frac * w / 2) ** 2


def sphere_stats(arr, mask):
    h = arr.shape[0]
    m = arr[mask].mean(axis=0)
    up = arr[: h // 2][mask[: h // 2]].mean(axis=0)
    lo = arr[h // 2 :][mask[h // 2 :]].mean(axis=0)
    return m, lum(up) / max(1e-6, lum(lo))


def silhouette(arr):
    """Mask of a reference thumbnail's sphere (anything brighter than the dark ground)."""
    L = lum(np.moveaxis(lin(arr), -1, 0))
    bg = np.median(np.concatenate([L[:8, :8].ravel(), L[-8:, -8:].ravel()]))
    return L > bg + 0.004


def limb_band(arr, mask):
    """Edge glow: linear luminance in the thin limb band (r/R 0.93–0.99, ±30° of
    the left / right limb points) divided by the sphere mean — the rig v5c rim
    metric (references: glossy Wood028 ≈ 2/3, matte Concrete034 ≈ 1.2/0.5)."""
    ys, xs = np.nonzero(mask)
    cy, cx = (ys.min() + ys.max()) / 2, (xs.min() + xs.max()) / 2
    R = max(ys.max() - ys.min(), xs.max() - xs.min()) / 2
    L = lum(np.moveaxis(lin(arr), -1, 0))          # H×W linear luminance
    yy, xx = np.mgrid[: arr.shape[0], : arr.shape[1]]
    r = np.hypot(yy - cy, xx - cx) / R
    ang = (np.degrees(np.arctan2(-(yy - cy), xx - cx)) + 360) % 360
    mean = L[(r < 0.97) & mask].mean()
    out = []
    for a0 in (180.0, 0.0):                                   # left, right
        d = ((ang - a0) + 180) % 360 - 180
        out.append(L[(r >= 0.93) & (r < 0.99) & (np.abs(d) < 30) & mask].mean() / mean)
    return tuple(out)


def ensure_ref(refs: Path, aid: str) -> Path:
    p = refs / f"ref_{aid}.png"
    if not p.is_file():
        req = urllib.request.Request(REF_URL.format(aid=aid), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        tmp = refs / f"ref_{aid}.jpg"
        tmp.write_bytes(data)
        Image.open(tmp).convert("RGB").save(p)
        tmp.unlink()
    return p


def apply_rig_overrides() -> None:
    """Env-var overrides for quick rig experiments (no code edits)."""
    if os.environ.get("RIG_ROT"):
        preview.ENV_ROTATION_DEG = float(os.environ["RIG_ROT"])
    if os.environ.get("RIG_GAIN"):
        preview.EXPOSURE_ALBEDO_GAIN = float(os.environ["RIG_GAIN"])
    if os.environ.get("RIG_SAT"):
        preview.PREVIEW_SATURATION = float(os.environ["RIG_SAT"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="calib", help="label for output files")
    ap.add_argument("--mode", choices=("exported", "wrap"), default="exported",
                    help="render the exported chain (as on main) or the reference-wrap variant")
    ap.add_argument("--refs", type=Path, default=Path("calibration/refs"),
                    help="directory for ref_<Id>.png (fetched when missing)")
    ap.add_argument("--out", type=Path, default=Path("calibration/out"))
    ap.add_argument("--ids", nargs="*", default=IDS)
    ap.add_argument("--rig", choices=("auto", "lights", "hdri"), default="auto",
                    help="preview rig: auto = by primitive (metals → hdri, else lights), as the web app does")
    args = ap.parse_args()
    if not preview.radiance_available():
        print("Radiance not found on PATH", file=sys.stderr)
        return 2
    apply_rig_overrides()
    args.refs.mkdir(parents=True, exist_ok=True)
    src_root = args.out / "src"
    rad_root = args.out / f"rad_{args.tag}"
    src_root.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(rad_root, ignore_errors=True)
    rad_root.mkdir()

    print(f"rigs: lights exposure {preview.fixed_exposure(preview.RIG_LIGHTS):.2f} | hdri {preview.ENV_HDR_NAME} rot {preview.ENV_ROTATION_DEG} "
          f"exposure {preview.fixed_exposure(preview.RIG_HDRI):.2f} | cam {preview._CAM_VP} | sat {preview.PREVIEW_SATURATION}→{preview.PREVIEW_SATURATION_HI}")
    n = preview._camera_facing_normal()
    for rig in (preview.RIG_LIGHTS, preview.RIG_HDRI):
        print(f"E facing camera ({rig}):", np.round(preview.rig_irradiance(n, rig), 3), f" neutrality {preview.rig_neutrality(n, rig):.3f}")

    opts = ConvertOptions(projection="box")
    if args.mode == "wrap":
        if not hasattr(opts, "write_preview_variant"):
            print("this checkout has no write_preview_variant (branch preview-reference-wrap)", file=sys.stderr)
            return 2
        opts.write_preview_variant = True  # type: ignore[attr-defined]

    rows, dlog = [], []
    for aid in args.ids:
        dest = src_root / aid
        if not dest.is_dir():
            mat_dir = ambientcg.download_texture_set(aid, dest, resolution="1k", fmt="jpg")
        else:
            mat_dir = dest if any(dest.glob("*.jpg")) else next(p for p in dest.iterdir() if p.is_dir())
        res = convert_set(discover(mat_dir), rad_root, opts)
        rad_file = getattr(res, "preview_rad_file", None) if args.mode == "wrap" else None
        rad_file = rad_file or res.rad_file
        out_png = args.out / f"{args.tag}_{aid}.png"
        rig = preview.rig_for(res.primitive) if args.rig == "auto" else args.rig
        assert preview.render_preview(res.out_dir, rad_file, out_png, rig=rig, roughness=res.roughness), aid
        a = np.asarray(Image.open(out_png).convert("RGBA")).astype(float)
        ours, ours_flat = sphere_stats(a[..., :3], a[..., 3] > 250)
        r = np.asarray(Image.open(ensure_ref(args.refs, aid)).convert("RGB")).astype(float)
        refm, ref_flat = sphere_stats(r, disk(r, 0.62))
        dl = math.log(lum(lin(ours)) / lum(lin(refm)))
        if aid not in UNSCORED:
            dlog.append(abs(dl))
        rows.append(aid)
        rim_o = limb_band(a[..., :3], a[..., 3] > 250)
        rim_r = limb_band(r, silhouette(r))
        print(f"{aid:13s} ours {ours.round(0)} up/lo {ours_flat:.2f} | ref {refm.round(0)} up/lo {ref_flat:.2f} | dlog-lum {dl:+.2f}"
              f" | limb L/R ours {rim_o[0]:.2f}/{rim_o[1]:.2f} ref {rim_r[0]:.2f}/{rim_r[1]:.2f}")
    if dlog:
        print(f"mean |dlog-lum| over scored materials: {np.mean(dlog):.3f}")

    sheet = Image.new("RGB", (len(rows) * 150 + 20, 2 * 150 + 60), (36, 36, 40))
    dr = ImageDraw.Draw(sheet)
    for i, aid in enumerate(rows):
        x = 10 + i * 150
        sheet.paste(Image.open(args.refs / f"ref_{aid}.png").convert("RGB").resize((140, 140)), (x, 10))
        im = Image.open(args.out / f"{args.tag}_{aid}.png").convert("RGBA")
        bg = Image.new("RGBA", im.size, (36, 36, 40, 255))
        sheet.paste(Image.alpha_composite(bg, im).convert("RGB").resize((140, 140)), (x, 160))
        dr.text((x + 2, 305), aid[:12], fill=(200, 200, 205))
    dr.text((10, 318), f"top: ambientCG reference · bottom: pbr2rad {args.tag} ({args.mode})", fill=(160, 160, 165))
    sheet_path = args.out / f"sheet_{args.tag}.png"
    sheet.save(sheet_path)
    print("sheet:", sheet_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
