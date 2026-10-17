"""Draws the Kindle Sim icon (a little e-reader showing a clock face) as a 1024px PNG."""

import math
import sys

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((150, 70, 874, 954), radius=110, fill=(30, 31, 35))           # body
d.rounded_rectangle((210, 140, 814, 830), radius=24, fill=(236, 234, 226))          # screen
cx, cy, r = 512, 470, 220
d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(28, 28, 30), width=22)
for i in range(12):
    a = math.radians(i * 30)
    r0 = r - (60 if i % 3 == 0 else 42)
    d.line((cx + r0 * math.sin(a), cy - r0 * math.cos(a), cx + (r - 26) * math.sin(a),
            cy - (r - 26) * math.cos(a)), fill=(28, 28, 30), width=16 if i % 3 == 0 else 9)
for ang, length, width in ((300, 0.5, 30), (60, 0.78, 20)):          # 10:10
    a = math.radians(ang)
    d.line((cx, cy, cx + r * length * math.sin(a), cy - r * length * math.cos(a)),
           fill=(28, 28, 30), width=width)
d.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(28, 28, 30))
d.rounded_rectangle((440, 868, 584, 900), radius=16, fill=(70, 72, 78))             # Kindle logo bar
img.save(sys.argv[1] if len(sys.argv) > 1 else "icon.png")
