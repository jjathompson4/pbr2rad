#!/usr/bin/env python
"""Does Poly Haven render its reference spheres with the texture rotated?

For every directional texture (structure-tensor anisotropy of the 1k albedo >= 0.3)
compare the dominant line orientation in the albedo with that at the centre of the
512 px reference thumbnail. SAME = the sphere wraps the picture un-rotated (image x
around, image y pole-to-pole — what ``convert.REFERENCE_WRAPS`` assumes); ROT90 =
the picture is transposed on the sphere.

Result 2026-08-23 (see docs/preview-scale-notes.md): 121 assets, 118 SAME, 0 ROT90,
3 unclear; ``brick_floor_003`` (below the anisotropy cut) is rotated on visual
inspection — its thumbnail doesn't match its own albedo.

    PYTHONPATH=src .venv/bin/python scripts/polyhaven_orientation_survey.py --limit 40
    PYTHONPATH=src .venv/bin/python scripts/polyhaven_orientation_survey.py --slugs oak_veneer_01 brick_floor_003
"""
from __future__ import annotations

import argparse
import io
import math
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

from pbr2rad import fetch

UA = {"User-Agent": "pbr2rad/0.1 (+https://pbr2rad.com)"}
KEYWORDS = ("plank", "wood", "brick", "corrugat", "parquet", "laminate", "floor", "deck", "board",
            "siding", "fence", "bamboo", "fabric", "denim", "slat", "rail", "pav", "metal", "tile", "roof")


def get(url: str, cache: Path) -> bytes:
    if cache.is_file():
        return cache.read_bytes()
    data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
    cache.write_bytes(data)
    return data


def line_orientation(gray: np.ndarray) -> tuple[float, float]:
    """Dominant line angle in degrees (0 = horizontal lines, 90 = vertical) and anisotropy 0..1."""
    a = gray.astype(float)
    gy, gx = np.gradient(a)
    jxx, jyy, jxy = (gx * gx).mean(), (gy * gy).mean(), (gx * gy).mean()
    theta = 0.5 * math.degrees(math.atan2(2 * jxy, jxx - jyy))       # gradient direction
    disc = math.sqrt(((jxx - jyy) / 2) ** 2 + jxy ** 2)
    l1, l2 = (jxx + jyy) / 2 + disc, (jxx + jyy) / 2 - disc
    return (theta + 90) % 180, (l1 - l2) / (l1 + l2 + 1e-9)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slugs", nargs="*", help="explicit asset slugs (default: keyword-picked from the catalog)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N scored assets (0 = all)")
    ap.add_argument("--cache", type=Path, default=Path("calibration/polyhaven_survey"))
    ap.add_argument("--min-aniso", type=float, default=0.3)
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)

    slugs = args.slugs
    if not slugs:
        cat = fetch.fetch_catalog()
        slugs = [c["id"] for c in cat if any(k in c["id"] for k in KEYWORDS)]
    counts = {"SAME": 0, "ROT90": 0, "?": 0}
    for slug in slugs:
        try:
            files = fetch.fetch_asset_files(slug)
            diff = files.get("Diffuse", {}).get("1k", {}).get("jpg", {}).get("url")
            if not diff:
                continue
            alb = np.asarray(Image.open(io.BytesIO(get(diff, args.cache / f"{slug}_diff.jpg"))).convert("L").resize((384, 384)))
            la, aa = line_orientation(alb)
            if aa < args.min_aniso:
                continue
            thumb = get(fetch._THUMB_URL.format(slug=slug, width=512), args.cache / f"{slug}_thumb.png")
            ref = np.asarray(Image.open(io.BytesIO(thumb)).convert("L"))
            h, w = ref.shape
            lr, ar = line_orientation(ref[h // 2 - 60:h // 2 + 60, w // 2 - 60:w // 2 + 60])
            if ar < 0.2:
                continue
            delta = abs(((la - lr) + 90) % 180 - 90)
            verdict = "SAME" if delta < 25 else "ROT90" if delta > 65 else "?"
            counts[verdict] += 1
            print(f"{slug:26s} albedo {la:6.1f}° ({aa:.2f})  thumb {lr:6.1f}° ({ar:.2f})  Δ {delta:5.1f}°  {verdict}", flush=True)
        except Exception as exc:  # network hiccups shouldn't kill the survey
            print(f"{slug:26s} ERROR {exc}", flush=True)
        if args.limit and sum(counts.values()) >= args.limit:
            break
    print("TOTAL", sum(counts.values()), counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
