"""Normal map conversion for Radiance ``texdata`` perturbation.

Converts a PBR normal map (PNG, OpenGL or DirectX convention) into:

* Three Radiance ``.dat`` data files (one per R/G/B channel, normalised 0..1).
* A ``.cal`` file that samples the ``.dat`` files and computes surface-normal
  perturbation deltas (dx, dy, dz) for use with the ``texdata`` primitive.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def _read_channel_f(img: Image.Image, channel: int) -> list[float]:
    """Extract one channel from an image as a list of 0..1 floats.

    Handles 8-bit (L/RGB) and 16/32-bit integer (I-family) images.
    """
    mode = img.mode

    if mode.startswith("I"):
        # Integer single-channel modes: "I;16"/"I;16L"/"I;16B" (16-bit) or
        # plain "I" (32-bit — how Pillow loads many 16-bit PNGs). getdata()
        # decodes each variant's width/endianness correctly; struct-unpacking
        # raw bytes as shorts would double the pixel count for "I" images.
        # Values are normalised as 16-bit and clamped.
        return [min(1.0, p / 65535.0) for p in img.getdata()]

    # For multi-channel images, split and take the requested channel
    if mode == "RGB":
        bands = img.split()
        band = bands[channel]
    elif mode == "L":
        band = img
    else:
        # Force to RGB and extract
        bands = img.convert("RGB").split()
        band = bands[channel]

    data = band.tobytes()
    return [b / 255.0 for b in data]


def write_dat_2d(path: Path, data: list[float], width: int, height: int) -> None:
    """Write a 2D Radiance ``.dat`` data file.

    Radiance's convention for multi-dim data files is that the LAST declared
    dimension varies fastest in the file. We declare dims as
    ``(width, height)`` so that Radiance's first sample coordinate ``u``
    indexes the X axis and the second ``v`` indexes the Y axis — matching
    how ``colorpict`` samples the accompanying HDR (u=horizontal, v=vertical).

    To make the file layout match that declaration, we write COLUMN-major:
    one file line per X column, each line containing ``height`` values (one
    per Y row). If you accidentally write row-major with the same
    declaration, Radiance will sample the .dat transposed 90° relative to
    the HDR — visible as cross-hatching artifacts when a normal map pattern
    overlays the matching albedo pattern.

    ``data`` is the source image in PIL scanline order (row-major, top-left
    origin): ``data[y * width + x]``.
    """
    if len(data) != width * height:
        raise ValueError(
            f"data length {len(data)} does not match {width}x{height}"
        )
    path = Path(path)

    lines = [
        "2",
        f"0\t1\t{width}",
        f"0\t1\t{height}",
    ]

    # Write column-by-column: one line per X value, each line has H values.
    # That makes the fast-varying axis in the file = Y (which matches the
    # last-declared dim = height).
    for x in range(width):
        col_vals = (data[y * width + x] for y in range(height))
        lines.append("\t".join(f"{v:.3f}" for v in col_vals))

    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def convert_normal_to_dat(
    src: Path,
    out_dir: Path,
    name: str,
) -> tuple[str, str, str, int, int]:
    """Convert a normal map PNG into three Radiance ``.dat`` files.

    Returns ``(r_dat_name, g_dat_name, b_dat_name, width, height)``
    where ``*_dat_name`` is the bare filename (no directory).
    """
    img = Image.open(src)
    # Flip vertically so DAT sample v=0 corresponds to the BOTTOM of the
    # source image — matching Radiance's bottom-origin picture coordinates
    # used by colorpict on the albedo HDR. Without this the normal-map
    # bumps land vertically inverted from the albedo pattern they describe.
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    width, height = img.size

    # Extract R, G, B channels as 0..1 floats
    if img.mode.startswith("I"):
        # Single-channel integer mode — treat as grayscale (unusual for normals)
        ch_r = _read_channel_f(img, 0)
        ch_g = ch_r
        ch_b = ch_r
    else:
        img_rgb = img.convert("RGB")
        ch_r = _read_channel_f(img_rgb, 0)
        ch_g = _read_channel_f(img_rgb, 1)
        ch_b = _read_channel_f(img_rgb, 2)

    r_name = f"{name}_nor_r.dat"
    g_name = f"{name}_nor_g.dat"
    b_name = f"{name}_nor_b.dat"

    out_dir = Path(out_dir)
    write_dat_2d(out_dir / r_name, ch_r, width, height)
    write_dat_2d(out_dir / g_name, ch_g, width, height)
    write_dat_2d(out_dir / b_name, ch_b, width, height)

    return r_name, g_name, b_name, width, height


def generate_normal_cal(
    name: str,
    *,
    is_dx: bool = False,
) -> str:
    """Generate a ``.cal`` file for normal-map perturbation via ``texdata``.

    The ``texdata`` primitive passes interpolated values from the three
    ``.dat`` files as arguments to the perturbation functions.  Each
    function receives ``(dx, dy, dz)`` — the sampled values from the
    R, G, B data files respectively.

    The ``.dat`` files store channel values in [0, 1].  We remap to
    [-1, +1] normal-vector range, then output perturbation deltas.
    ``A1`` (the first real argument from the texdata definition) controls
    bump intensity.

    Parameters
    ----------
    name:
        Material name (for comments).
    is_dx:
        If True, flip the Y component (DirectX convention).
    """
    flip_y = -1 if is_dx else 1

    return (
        f"{{ Normal perturbation for {name} - generated by pbr2rad }}\n"
        f"{{ Convention: {'DirectX (Y flipped)' if is_dx else 'OpenGL'} }}\n"
        "{ A1 = bump intensity (from texdata real args) }\n"
        "\n"
        "{ Each function receives (dx,dy,dz) = sampled R,G,B from .dat files }\n"
        "{ Convert from [0,1] data range to [-1,+1] normal range }\n"
        f"dx_func(r,g,b) = A1 * (2*r - 1);\n"
        f"dy_func(r,g,b) = A1 * {flip_y} * (2*g - 1);\n"
        f"dz_func(r,g,b) = A1 * (2*b - 1 - 1);\n"
    )


def detect_convention(maps: dict[str, Path]) -> str:
    """Detect normal map convention from discovered channel keys.

    Returns ``"gl"`` (OpenGL, Y+) or ``"dx"`` (DirectX, Y-).
    Defaults to ``"gl"`` when ambiguous.
    """
    if "normal_dx" in maps and "normal_gl" not in maps:
        return "dx"
    return "gl"


# ---------------------------------------------------------------------------
# Spatially varying roughness (brightdata)
# ---------------------------------------------------------------------------

def convert_roughness_to_dat(
    src: Path,
    out_dir: Path,
    name: str,
) -> tuple[str, int, int]:
    """Convert a roughness map PNG into a single Radiance ``.dat`` file.

    The roughness map is single-channel (grayscale).  Values are stored
    as 0..1 floats in the ``.dat`` file.

    Returns ``(dat_name, width, height)``.
    """
    img = Image.open(src)
    # Vertical flip — see convert_normal_to_dat for rationale (DAT bottom-origin
    # alignment with the albedo HDR via colorpict).
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    width, height = img.size

    # Read as grayscale 0..1
    if img.mode.startswith("I"):
        # Integer modes decoded via getdata() — see _read_channel_f for why
        # struct-unpacking raw bytes is wrong for 32-bit "I" images.
        channel = _read_channel_f(img, 0)
    else:
        gray = img.convert("L")
        raw = gray.tobytes()
        channel = [b / 255.0 for b in raw]

    dat_name = f"{name}_rough.dat"
    write_dat_2d(Path(out_dir) / dat_name, channel, width, height)
    return dat_name, width, height


def generate_roughness_cal(name: str) -> str:
    """Generate a ``.cal`` file for roughness-driven specular modulation.

    Used with ``brightdata``.  The function receives the sampled roughness
    value and converts it to a specular attenuation factor:

    * roughness=0 (smooth) → factor=1 (full specular)
    * roughness=1 (rough)  → factor=(1 - A1) (reduced specular)

    ``A1`` (from the brightdata real args) controls the modulation depth.
    """
    return (
        f"{{ Roughness-driven specular modulation for {name} - generated by pbr2rad }}\n"
        "{ A1 = modulation depth (from brightdata real args) }\n"
        "\n"
        "{ Invert roughness to specular brightness: smooth=bright, rough=dim }\n"
        "rough_func(v) = 1 - A1 * v;\n"
    )
