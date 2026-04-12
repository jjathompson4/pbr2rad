"""``pbr2rad`` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .convert import ConvertOptions, convert_set, write_manifest
from .discover import discover_many


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pbr2rad",
        description=(
            "Convert PBR texture sets (Poly Haven / ambientCG style) into a "
            "Radiance material library folder (.rad/.cal/.hdr)."
        ),
    )
    p.add_argument("input", type=Path, help="PBR set folder, or parent folder of sets")
    p.add_argument("-o", "--output", type=Path, required=True, help="Output library folder")
    p.add_argument(
        "--projection",
        choices=("uv", "planar", "box"),
        default="uv",
        help="Projection mode. 'uv' uses mesh Lu/Lv (Radiance 6.0).",
    )
    p.add_argument(
        "--planar-axis",
        choices=("xy", "xz", "yz"),
        default="xy",
        help="Axis pair for --projection planar.",
    )
    p.add_argument("--u-scale", type=float, default=1.0)
    p.add_argument("--v-scale", type=float, default=1.0)
    p.add_argument("--u-offset", type=float, default=0.0)
    p.add_argument("--v-offset", type=float, default=0.0)
    p.add_argument(
        "--roughness",
        type=float,
        default=None,
        help="Override roughness (0..1). Default: mean of roughness map, or 0.5.",
    )
    p.add_argument(
        "--metalness",
        type=float,
        default=None,
        help="Override metalness (0..1). Default: mean of metalness map, or 0.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    opts = ConvertOptions(
        projection=args.projection,
        planar_axis=args.planar_axis,
        u_scale=args.u_scale,
        v_scale=args.v_scale,
        u_offset=args.u_offset,
        v_offset=args.v_offset,
        roughness_override=args.roughness,
        metalness_override=args.metalness,
    )

    sets = discover_many(args.input)
    if not sets:
        print(f"error: no PBR sets found under {args.input}", file=sys.stderr)
        return 1

    results = []
    skipped = 0
    for pbr in sets:
        if pbr.albedo is None:
            print(f"skip: {pbr.name} (no albedo map found)", file=sys.stderr)
            skipped += 1
            continue
        if args.verbose:
            print(f"converting: {pbr.name}")
            for channel, path in pbr.maps.items():
                print(f"  {channel:<12s} {path.name}")
        result = convert_set(pbr, args.output, opts)
        results.append(result)
        print(
            f"  → {result.rad_file.relative_to(args.output)} "
            f"[{result.primitive}, rough={result.roughness:.2f}, "
            f"metal={result.metalness:.2f}]"
        )

    if not results:
        print("error: nothing was converted", file=sys.stderr)
        return 1

    manifest = write_manifest(results, args.output)
    print(f"wrote {len(results)} material(s); manifest: {manifest}")
    if skipped:
        print(f"({skipped} set(s) skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
