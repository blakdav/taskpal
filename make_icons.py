#!/usr/bin/env python3
"""Generate app icons.

Run this after changing the accent colour so the home screen icon matches the
app. Needs Pillow:

    pip install pillow
    python make_icons.py

iOS masks apple-touch-icon with its own rounded rect, so the source art is a
full-bleed square with no corner rounding of its own.
"""

from pathlib import Path

from PIL import Image, ImageDraw

# Match the CSS variables in templates/index.html
PAPER = "#171A17"
ACCENT = "#7DA882"

OUT = Path(__file__).parent / "static"
SIZES = {
    "icon-180.png": 180,   # apple-touch-icon
    "icon-192.png": 192,   # android / manifest
    "icon-512.png": 512,   # manifest, splash
    "favicon-32.png": 32,
}

MASTER = 1024


def draw_master() -> Image.Image:
    img = Image.new("RGBA", (MASTER, MASTER), PAPER)
    d = ImageDraw.Draw(img)

    # An empty checkbox, drawn at the weight the UI uses.
    pad, radius, stroke = 232, 56, 40
    d.rounded_rectangle(
        [pad, pad, MASTER - pad, MASTER - pad],
        radius=radius, outline=ACCENT, width=stroke,
    )

    # The tick, breaking out past the box's top-right corner so the mark
    # reads as an action rather than a static glyph.
    d.line(
        [(360, 520), (480, 645), (735, 330)],
        fill=ACCENT, width=stroke + 14, joint="curve",
    )
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    master = draw_master()

    for name, size in SIZES.items():
        master.resize((size, size), Image.LANCZOS).save(OUT / name)
        print(f"wrote {OUT / name}")

    # SVG favicon for browsers that prefer it -- stays crisp at any size.
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" fill="{PAPER}"/>
  <rect x="252" y="252" width="520" height="520" rx="56"
        fill="none" stroke="{ACCENT}" stroke-width="40"/>
  <path d="M360 520 L480 645 L735 330" fill="none" stroke="{ACCENT}"
        stroke-width="54" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
'''
    (OUT / "icon.svg").write_text(svg)
    print(f"wrote {OUT / 'icon.svg'}")


if __name__ == "__main__":
    main()
