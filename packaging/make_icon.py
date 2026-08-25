#!/usr/bin/env python3
"""Draw the app icon and write packaging/icon.icns.

The icon is the day strip, which is the only thing the app ever shows you: a
period's worth of cells, mostly green, with a holiday, some PTO and a day that
hasn't happened yet. Colours are macOS's own system green/orange/purple, the
same ones the strip is drawn with, so the icon and the menu match.

The .icns is committed, so building a release needs neither Pillow nor a Mac.
Re-run this only when the artwork changes:

    pip install Pillow && python packaging/make_icon.py
"""
from __future__ import annotations

import os
import struct

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ICNS = os.path.join(HERE, "icon.icns")
PREVIEW = os.path.join(os.path.dirname(HERE), "docs", "icon.png")

SIZE = 1024
SS = 4                                  # supersample; Pillow has no antialiased draw

# macOS dark-appearance system colours — the strip's own palette, which reads
# brighter than the light variants against a dark background.
GREEN = (48, 209, 88)
ORANGE = (255, 159, 10)
PURPLE = (191, 90, 242)

BG_TOP = (52, 62, 79)
BG_BOTTOM = (24, 29, 39)

# Apple's app-icon grid: an 824pt rounded square centred in a 1024pt canvas,
# with a corner radius of 185pt.
MARGIN, RADIUS = 100, 185

COLS, ROWS = 4, 3
CELL, GAP, CELL_RADIUS = 132, 28, 30

#   G G G G
#   G H G G      H = holiday      P = PTO
#   G P G .      . = a working day still ahead of us
LAYOUT = [
    GREEN, GREEN, GREEN, GREEN,
    GREEN, ORANGE, GREEN, GREEN,
    GREEN, PURPLE, GREEN, None,
]


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        strip.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return strip.resize((size, size), Image.NEAREST)


def draw(px: int) -> Image.Image:
    """Draw the icon at `px`, working `SS` times larger and shrinking to fit."""
    s = px * SS
    k = s / SIZE                                   # canvas units → pixels

    def u(v: float) -> float:
        return v * k

    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # The rounded square, as a gradient behind a mask.
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [u(MARGIN), u(MARGIN), u(SIZE - MARGIN), u(SIZE - MARGIN)],
        radius=u(RADIUS), fill=255)
    canvas.paste(_vertical_gradient(s, BG_TOP, BG_BOTTOM).convert("RGBA"), (0, 0), mask)

    # A hairline along the top edge, so the tile has an edge rather than just
    # stopping. Barely visible, and doing the work at 512pt and up.
    ImageDraw.Draw(canvas).rounded_rectangle(
        [u(MARGIN), u(MARGIN), u(SIZE - MARGIN), u(SIZE - MARGIN)],
        radius=u(RADIUS), outline=(255, 255, 255, 34), width=max(1, round(u(3))))

    grid_w = COLS * CELL + (COLS - 1) * GAP
    grid_h = ROWS * CELL + (ROWS - 1) * GAP
    x0, y0 = (SIZE - grid_w) / 2, (SIZE - grid_h) / 2

    draw_on = ImageDraw.Draw(canvas)
    for i, colour in enumerate(LAYOUT):
        cx = x0 + (i % COLS) * (CELL + GAP)
        cy = y0 + (i // COLS) * (CELL + GAP)
        box = [u(cx), u(cy), u(cx + CELL), u(cy + CELL)]
        if colour is None:                          # planned, not yet entered
            draw_on.rounded_rectangle(box, radius=u(CELL_RADIUS),
                                      outline=(255, 255, 255, 72),
                                      width=max(1, round(u(10))))
        else:
            draw_on.rounded_rectangle(box, radius=u(CELL_RADIUS), fill=colour + (255,))

    return canvas.resize((px, px), Image.LANCZOS)


# ── .icns ─────────────────────────────────────────────────────────────────────
# Written by hand rather than shelled out to iconutil, so the artwork can be
# regenerated anywhere. An icns is a magic word, a total length, and then a
# sequence of (4-byte type, length-including-header, payload) chunks; for every
# type below the payload is simply a PNG. The type/size pairs are the ten
# iconutil emits for a full .iconset.
VARIANTS = [
    ("icp4", 16), ("icp5", 32), ("ic07", 128), ("ic08", 256), ("ic09", 512),
    ("ic10", 1024),                       # 512@2x
    ("ic11", 32), ("ic12", 64), ("ic13", 256), ("ic14", 512),   # the @2x set
]


def build() -> None:
    import io

    rendered: dict[int, Image.Image] = {}
    for _, px in VARIANTS:
        rendered.setdefault(px, draw(px))

    chunks = b""
    for kind, px in VARIANTS:
        buf = io.BytesIO()
        rendered[px].save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        chunks += kind.encode("ascii") + struct.pack(">I", len(data) + 8) + data

    with open(ICNS, "wb") as fh:
        fh.write(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)

    os.makedirs(os.path.dirname(PREVIEW), exist_ok=True)
    rendered[512].save(PREVIEW, format="PNG", optimize=True)
    print(f"wrote {ICNS} ({os.path.getsize(ICNS):,} bytes)")
    print(f"wrote {PREVIEW}")


if __name__ == "__main__":
    build()
