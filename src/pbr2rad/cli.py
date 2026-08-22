"""``pbr2rad`` command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .convert import ConvertOptions, convert_set, write_manifest
from .discover import discover_many


def _unit_float(value: str) -> float:
    """argparse type: float clamped-checked to [0, 1]."""
    f = float(value)
    if not 0.0 <= f <= 1.0:
        raise argparse.ArgumentTypeError(f"{value} is not in the range 0..1")
    return f


def _positive_float(value: str) -> float:
    f = float(value)
    if f <= 0:
        raise argparse.ArgumentTypeError(f"{value} must be > 0")
    return f


def _build_convert_parser(
    p: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    """Build (or populate) the convert argument parser."""
    if p is None:
        p = argparse.ArgumentParser(
            prog="pbr2rad",
            description=(
                "Convert PBR texture sets (Poly Haven / ambientCG style) into a "
                "Radiance material library folder (.rad/.cal/.hdr)."
            ),
            epilog=(
                "Subcommands: 'pbr2rad fetch <slug> -o DIR' downloads a Poly "
                "Haven set; 'pbr2rad serve' starts the web UI; 'pbr2rad "
                "convert <input> ...' is the explicit form of the default "
                "command (use it if your input folder is literally named "
                "'fetch' or 'serve')."
            ),
        )
    p.add_argument("--version", action="version", version=f"pbr2rad {__version__}")
    p.add_argument("input", type=Path, help="PBR set folder, or parent folder of sets")
    p.add_argument("-o", "--output", type=Path, required=True, help="Output library folder")
    p.add_argument(
        "--projection",
        choices=("uv", "planar", "box", "cylindrical", "spherical"),
        default="box",
        help=(
            "Projection mode. 'box' (default) is triplanar and works on any "
            "surface. 'uv' uses mesh Lu/Lv (Radiance 6.0), but obj2mesh "
            "currently strips the pbr2rad material chain on import — see "
            "README for details."
        ),
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
        type=_unit_float,
        default=None,
        help="Override roughness (0..1). Default: mean of roughness map, or 0.5.",
    )
    p.add_argument(
        "--metalness",
        type=_unit_float,
        default=None,
        help="Override metalness (0..1). Default: mean of metalness map, or 0.",
    )
    p.add_argument(
        "--no-normal",
        action="store_true",
        help="Skip normal map even if one is discovered.",
    )
    p.add_argument(
        "--bump-scale",
        type=_positive_float,
        default=1.0,
        help="Normal map perturbation strength (default: 1.0).",
    )
    p.add_argument(
        "--varying-roughness",
        action="store_true",
        help="Opt-in: emit a brightdata pattern from the roughness map. NOTE: a "
             "Radiance pattern scales the material colour (diffuse reflectance), not "
             "its roughness, so this darkens rough areas; off by default.",
    )
    p.add_argument(
        "--no-estimate",
        action="store_true",
        help="Don't estimate missing normal/roughness maps from albedo.",
    )
    p.add_argument(
        "--no-pvw",
        action="store_true",
        help="Don't write the ClimateStudio material-preview file.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress per-material progress output (errors still print).",
    )
    return p


def _build_fetch_parser(
    p: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    """Build (or populate) the fetch argument parser."""
    from .sources import DEFAULT_SOURCE, SOURCES

    if p is None:
        p = argparse.ArgumentParser(prog="pbr2rad fetch")
    p.add_argument(
        "slug",
        metavar="asset",
        help="Asset id for the chosen source "
             "(Poly Haven slug e.g. 'wood_floor_03'; ambientCG id e.g. 'Bricks104').",
    )
    p.add_argument("-o", "--output", type=Path, required=True, help="Output folder")
    p.add_argument(
        "--source",
        choices=tuple(sorted(SOURCES)),
        default=DEFAULT_SOURCE,
        help=f"Texture source to fetch from (default: {DEFAULT_SOURCE}).",
    )
    p.add_argument(
        "--resolution",
        choices=("1k", "2k"),
        default="1k",
        help="Texture resolution to download (default: 1k, max: 2k).",
    )
    p.add_argument(
        "--format",
        dest="fmt",
        choices=("png", "jpg", "exr"),
        default="png",
        help="Image format to download (default: png; ambientCG offers png/jpg only).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _run_convert(args: argparse.Namespace) -> int:
    # Exit code convention: 0 = converted something; 1 = runtime failure
    # (missing input, nothing converted); 2 is reserved for argparse usage
    # errors so callers can tell the two apart.
    if not args.input.is_dir():
        kind = "not found" if not args.input.exists() else "not a directory"
        print(f"error: input {kind}: {args.input}", file=sys.stderr)
        return 1

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
        normal=not args.no_normal,
        bump_scale=args.bump_scale,
        varying_roughness=args.varying_roughness,
        estimate_maps=not args.no_estimate,
        write_pvw=not args.no_pvw,
    )

    sets = discover_many(args.input)
    if not sets:
        print(f"error: no PBR sets found under {args.input}", file=sys.stderr)
        return 1

    results = []
    skipped = 0
    failed = 0
    quiet = getattr(args, "quiet", False)
    for pbr in sets:
        if pbr.albedo is None:
            print(f"skip: {pbr.name} (no albedo map found)", file=sys.stderr)
            skipped += 1
            continue
        if args.verbose:
            print(f"converting: {pbr.name}")
            for channel, path in pbr.maps.items():
                print(f"  {channel:<12s} {path.name}")
        # One corrupt set must not abort a whole library run.
        try:
            result = convert_set(pbr, args.output, opts)
        except Exception as exc:
            print(f"error: {pbr.name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        results.append(result)
        if not quiet:
            rel = os.path.relpath(result.rad_file, args.output)
            print(
                f"  → {rel} "
                f"[{result.primitive}, rough={result.roughness:.2f}, "
                f"metal={result.metalness:.2f}]"
            )

    if not results:
        print("error: nothing was converted", file=sys.stderr)
        return 1

    manifest = write_manifest(results, args.output)
    if not quiet:
        print(f"wrote {len(results)} material(s); manifest: {manifest}")
        if skipped:
            print(f"({skipped} set(s) skipped)")
        if failed:
            print(f"({failed} set(s) failed)")
    # Partial success is still success (converted materials are usable),
    # but a run where every attempted set failed already returned 1 above.
    return 0


def _run_fetch(args: argparse.Namespace) -> int:
    from .fetch import FetchError
    from .sources import DEFAULT_SOURCE, get_downloader

    download = get_downloader(getattr(args, "source", DEFAULT_SOURCE))
    try:
        mat_dir = download(
            args.slug,
            args.output,
            resolution=args.resolution,
            fmt=args.fmt,
            verbose=args.verbose,
        )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"downloaded: {mat_dir}")
    return 0


def _run_serve(argv: list[str]) -> int:
    """Start the web server."""
    p = argparse.ArgumentParser(prog="pbr2rad serve")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    p.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = p.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "error: web dependencies not installed.\n"
            "Run: pip install pbr2rad[web]",
            file=sys.stderr,
        )
        return 1

    print(f"Starting pbr2rad web server at http://{args.host}:{args.port}")
    uvicorn.run(
        "pbr2rad.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point.  Dispatches to ``convert`` (default), ``fetch``, or ``serve``."""
    if argv is None:
        argv = sys.argv[1:]

    # Detect subcommand
    if argv and argv[0] == "fetch":
        parser = _build_fetch_parser()
        args = parser.parse_args(argv[1:])
        return _run_fetch(args)

    if argv and argv[0] == "serve":
        return _run_serve(argv[1:])

    # Strip optional "convert" prefix for explicit subcommand usage.
    if argv and argv[0] == "convert":
        argv = argv[1:]

    parser = _build_convert_parser()
    args = parser.parse_args(argv)
    return _run_convert(args)


if __name__ == "__main__":
    raise SystemExit(main())
