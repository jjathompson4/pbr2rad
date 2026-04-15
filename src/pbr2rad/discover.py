"""PBR texture discovery.

Scans an input folder and identifies texture maps by filename convention.
Supports Poly Haven (``*_diff_*``, ``*_rough_*``, ``*_nor_gl_*``, ``*_disp_*``,
``*_arm_*``) and ambientCG (``*_Color*``, ``*_Roughness*``, ``*_NormalGL*``,
``*_Displacement*``, ``*_Metalness*``) naming conventions, plus a generic
keyword fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Ordered list of (channel, keyword tokens). Tokens are matched against
# underscore/dash/dot-separated FILENAME SEGMENTS, not as bare substrings —
# otherwise an asset whose base name contains a channel word (e.g.
# ``box_profile_metal_sheet_diff_1k``) has every file wrongly classified
# as that channel. First match wins; more specific tokens listed first.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("normal_gl", ("norgl", "normalgl", "nor-gl", "normal-gl", "normal-ogl", "normalopengl")),
    ("normal_dx", ("nordx", "normaldx", "nor-dx", "normal-dx")),
    ("normal", ("normal", "nor", "nrm", "norm")),
    ("displacement", ("disp", "displacement", "height", "bump")),
    ("roughness", ("rough", "roughness")),
    ("metalness", ("metal", "metallic", "metalness")),
    ("ao", ("ao", "ambientocclusion", "occlusion")),
    ("arm", ("arm",)),  # packed AO/Roughness/Metalness
    ("albedo", ("diff", "albedo", "basecolor", "col", "color", "diffuse")),
]

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".hdr", ".bmp"}


@dataclass
class PBRSet:
    """A discovered PBR texture set."""

    name: str
    root: Path
    maps: dict[str, Path] = field(default_factory=dict)
    extras: list[Path] = field(default_factory=list)

    @property
    def albedo(self) -> Path | None:
        return self.maps.get("albedo")

    @property
    def roughness(self) -> Path | None:
        return self.maps.get("roughness")

    @property
    def metalness(self) -> Path | None:
        return self.maps.get("metalness")

    @property
    def normal(self) -> Path | None:
        return self.maps.get("normal_gl") or self.maps.get("normal") or self.maps.get("normal_dx")


_RES_TAGS = ("1k", "2k", "4k", "8k", "16k")
_EXT_TAGS = ("png", "jpg", "jpeg", "tif", "tiff", "exr", "hdr", "bmp")


def _suffix_segments(filename: str) -> list[str]:
    """Return the 1-3 filename segments that encode the channel.

    Standard PBR naming is ``{basename}_{channel}_{resolution}.{ext}`` or
    ``{basename}_{channel}.{ext}``. We trim the resolution/extension, then
    take up to the last 2 segments of what remains — that's the specifier
    (``diff``, ``nor_gl``, ``nor_dx``, ``ao``, …). Earlier segments are the
    asset base name and must NOT be consulted for classification, because
    they often contain channel-like words (e.g. ``box_profile_metal_sheet``
    has ``metal`` in its name but is not a metal texture).
    """
    import re
    parts = [s for s in re.split(r"[_\-. ]+", filename.lower()) if s]
    # Drop trailing extension
    while parts and parts[-1] in _EXT_TAGS:
        parts.pop()
    # Drop trailing resolution tag (may come with or without format suffix)
    while parts and parts[-1] in _RES_TAGS:
        parts.pop()
    # Keep only the LAST up-to-2 segments (the channel specifier)
    suffix = parts[-2:] if len(parts) >= 2 else parts[-1:] if parts else []
    return suffix


def _classify(filename: str) -> str | None:
    suffix = _suffix_segments(filename)
    # Build matchable tokens: each segment individually, plus the joined pair
    # (so "nor_gl" → ["nor", "gl", "norgl"] all match).
    matchable = set(suffix)
    if len(suffix) == 2:
        matchable.add(suffix[0] + suffix[1])
    for channel, tokens in _PATTERNS:
        for tok in tokens:
            if tok in matchable:
                return channel
    return None


def discover(folder: Path, name: str | None = None) -> PBRSet:
    """Discover a single PBR set in ``folder``.

    The set name defaults to the folder's basename.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    pbr = PBRSet(name=name or folder.name, root=folder)

    candidates = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )

    # Prefer the highest-resolution variant when duplicates exist: sort by
    # filename (stable) but give longer filenames (usually contain resolution
    # suffix like ``_2k``/``_4k``) priority via tie-break on resolution token.
    def _res_rank(p: Path) -> int:
        lower = p.name.lower()
        for i, tag in enumerate(("_1k", "_2k", "_4k", "_8k", "_16k")):
            if tag in lower:
                return i
        return -1

    # Group by channel, keep the highest-resolution variant.
    best: dict[str, tuple[int, Path]] = {}
    for p in candidates:
        channel = _classify(p.name)
        if channel is None:
            pbr.extras.append(p)
            continue
        rank = _res_rank(p)
        prev = best.get(channel)
        if prev is None or rank > prev[0]:
            best[channel] = (rank, p)

    pbr.maps = {c: v for c, (_r, v) in best.items()}
    return pbr


def discover_many(root: Path) -> list[PBRSet]:
    """Discover PBR sets in ``root``.

    If ``root`` itself contains image files, it is treated as a single set.
    Otherwise each immediate subdirectory is treated as its own set.
    """
    root = Path(root)
    has_images = any(
        p.is_file() and p.suffix.lower() in _IMAGE_EXTS for p in root.iterdir()
    )
    if has_images:
        return [discover(root)]
    return [discover(sub) for sub in sorted(root.iterdir()) if sub.is_dir()]
