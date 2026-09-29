"""Render the menu bar icons: a clock over a punched timecard, tinted per state.

Run `python3 make_icons.py` to regenerate icons/*.png (needs Pillow). The PNGs
are committed, so Pillow is only needed if you change the artwork.
"""
import math
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "icons"
STATES = {                      # state → clock face colour
    "ok": "#30b94d", "missing": "#ea4339", "pto": "#2f8ff0", "holiday": "#a259e6",
    "unset": "#9a9aa2", "busy": "#f29a1f", "error": "#f2c21f",
}
S = 8                           # supersample factor
W, H = 26, 22                   # points; saved at 2x for retina


def render(color: str) -> Image.Image:
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    p = lambda v: v * S
    card = "#8e8e96"
    # timecard: tall rounded rect behind the clock, with a punched slot row
    d.rounded_rectangle([p(11), p(1), p(24.5), p(21)], p(2), fill=card)
    for y in (5, 8.5, 12, 15.5):                       # punch marks
        d.rounded_rectangle([p(18.5), p(y), p(22.5), p(y + 1.8)], p(0.9), fill=(0, 0, 0, 0))
    # clock: knock a halo out of the card so it reads as sitting in front
    cx, cy, r = p(8.5), p(12.5), p(8)
    halo = Image.new("L", img.size, 0)
    ImageDraw.Draw(halo).ellipse([cx - r - p(1.4), cy - r - p(1.4), cx + r + p(1.4), cy + r + p(1.4)], fill=255)
    img.paste((0, 0, 0, 0), mask=halo)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    for h in range(12):                                # tick marks
        a = h * math.pi / 6
        l = p(1.5) if h % 3 == 0 else p(0.8)
        d.line([cx + math.sin(a) * (r - p(1) - l), cy - math.cos(a) * (r - p(1) - l),
                cx + math.sin(a) * (r - p(1)), cy - math.cos(a) * (r - p(1))],
               fill="white", width=int(p(0.55)))
    d.line([cx, cy, cx, cy - p(4.6)], fill="white", width=int(p(1.3)))                 # minute hand
    d.line([cx, cy, cx + p(3), cy + p(1.4)], fill="white", width=int(p(1.3)))          # hour hand
    d.ellipse([cx - p(0.9), cy - p(0.9), cx + p(0.9), cy + p(0.9)], fill="white")
    return img.resize((W * 2, H * 2), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, color in STATES.items():
        render(color).save(OUT / f"{name}.png", dpi=(144, 144))
    print(f"wrote {len(STATES)} icons to {OUT}")
