"""Render the application icon, as a Windows ``.ico`` and a PNG preview.

Run from the repository root:

    uv run python scripts/make_icon.py

The output is committed, so this only has to be run when the mark changes. It is
kept as a script rather than a checked-in binary blob nobody can edit, and it
draws at 8x and downsamples, because a 16 px icon is the size that decides
whether the mark is legible and nothing drawn directly at 16 px ever is.

The mark is five bars of a voice waveform on the brand blue: the product's
subject is speech, and a waveform survives being 16 px wide in a way that
lettering does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - developer convenience
    sys.exit("Pillow is required: uv run --with pillow python scripts/make_icon.py")

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "src" / "surtitle" / "web"

# Sizes Windows asks for: 16 in the notification area and the title bar, up to
# 256 in the large-icon views of Explorer.
SIZES = (16, 24, 32, 48, 64, 128, 256)

# Rendered large and reduced, so the rounded corners and the bar ends are
# antialiased at every size rather than only the biggest one.
SUPERSAMPLE = 8

TOP = (0x67, 0x9E, 0xFE)
BOTTOM = (0x41, 0x76, 0xE6)
GLYPH = (0xFF, 0xFF, 0xFF)

# Bar heights as a fraction of the drawing area, centred vertically: a spoken
# syllable, widest in the middle.
BAR_HEIGHTS = (0.34, 0.62, 1.0, 0.62, 0.34)
BAR_GAP = 0.42  # gap width as a fraction of a bar's width

# At 16 px, five bars collapse into a white smear: the gaps are under two pixels
# and the antialiasing fills them in. Below 32 px the mark drops to three
# thicker bars with wider gaps and squarer ends, which is the same idea at a
# size that can actually show it.
SMALL_BAR_HEIGHTS = (0.55, 1.0, 0.55)
SMALL_BAR_GAP = 0.65
SMALL_THRESHOLD = 32
SMALL_AREA = 0.68
SMALL_MAX_HEIGHT = 0.66
SMALL_RADIUS_RATIO = 0.25


def _gradient(size: int) -> Image.Image:
    """Vertical brand-blue gradient, drawn one row at a time."""
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        draw.line(
            [(0, y), (size, y)],
            fill=tuple(
                round(top + (bottom - top) * ratio) for top, bottom in zip(TOP, BOTTOM, strict=True)
            ),
        )
    return image


def _rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    """A rounded square, as an 8-bit mask the gradient is pasted through."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=round(size * radius_ratio), fill=255
    )
    return mask


def _bars(size: int) -> Image.Image:
    """The waveform, as a white-on-transparent layer."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    if size < SMALL_THRESHOLD:
        heights = SMALL_BAR_HEIGHTS
        gap_ratio = SMALL_BAR_GAP
        area_ratio = SMALL_AREA
        max_height_ratio = SMALL_MAX_HEIGHT
        # Squarer ends below 32 px: a full-round cap on a bar this short is
        # nearly a circle, and three circles do not read as a waveform.
        radius_ratio = SMALL_RADIUS_RATIO
    else:
        heights = BAR_HEIGHTS
        gap_ratio = BAR_GAP
        area_ratio = 0.60
        max_height_ratio = 0.62
        radius_ratio = 0.5

    area = size * area_ratio  # how much of the width the waveform occupies
    count = len(heights)
    bar_width = area / (count + (count - 1) * gap_ratio)
    gap = bar_width * gap_ratio
    total = bar_width * count + gap * (count - 1)
    left = (size - total) / 2
    max_height = size * max_height_ratio

    for index, fraction in enumerate(heights):
        height = max_height * fraction
        x0 = left + index * (bar_width + gap)
        y0 = (size - height) / 2
        draw.rounded_rectangle(
            [x0, y0, x0 + bar_width, y0 + height],
            radius=bar_width * radius_ratio,
            fill=(*GLYPH, 255),
        )
    return layer


def render(size: int) -> Image.Image:
    """One icon at ``size`` pixels, RGBA."""
    big = size * SUPERSAMPLE
    plate = _gradient(big).convert("RGBA")
    plate.putalpha(_rounded_mask(big))
    plate.alpha_composite(_bars(big))
    return plate.resize((size, size), Image.LANCZOS)


def main() -> int:
    images = [render(size) for size in SIZES]
    WEB.mkdir(parents=True, exist_ok=True)

    ico = WEB / "surtitle.ico"
    # Pillow writes every size it is given into one .ico, which is what makes
    # the same file serve the 16 px notification area and the 256 px preview.
    images[-1].save(ico, format="ICO", sizes=[(size, size) for size in SIZES])

    preview = WEB / "surtitle-icon.png"
    images[-1].save(preview, format="PNG")

    print(f"wrote {ico.relative_to(ROOT)} ({ico.stat().st_size} bytes)")
    print(f"wrote {preview.relative_to(ROOT)} ({preview.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
