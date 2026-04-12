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

# Ordered list of (channel, keyword tokens).  First match wins; more specific
# tokens are listed before more general ones (e.g. ``nor_gl`` before ``nor``).
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("normal_gl", ("nor_gl", "normalgl", "normal_gl", "normal-ogl", "normal_opengl")),
    ("normal_dx", ("nor_dx", "normaldx", "normal_dx", "normal-dx")),
    ("normal", ("normal", "_nor_", "_nrm_", "_norm_")),
    ("displacement", ("disp", "displacement", "height", "_bump_")),
    ("roughness", ("rough", "roughness")),
    ("metalness", ("metal", "metallic", "metalness")),
    ("ao", ("_ao_", "ambientocclusion", "occlusion")),
    ("arm", ("_arm_",)),  # packed AO/Roughness/Metalness
    ("albedo", ("diff", "albedo", "basecolor", "base_color", "_col_", "color", "diffuse")),
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


def _classify(filename: str) -> str | None:
    lower = filename.lower()
    for channel, tokens in _PATTERNS:
        for tok in tokens:
            if tok in lower:
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
