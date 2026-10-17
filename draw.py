#!/usr/bin/env python3
"""Drawing pad: draw with your finger on the Kindle; each stroke appears when you lift it.
A toolbar along the bottom has Thin / Thick pen, Eraser, Undo, Clear and Save.

Strokes are kept as point lists in draw/strokes.json (so Undo can redraw without the last
one); Save writes draw/saved/drawing-YYYYmmdd-HHMMSS.png, which "kindle draw save" on the
Mac downloads.

  draw.py save     save the current drawing, print its path
  draw.py clear
"""

import json
import os
import sys
from datetime import datetime

from PIL import Image, ImageDraw

import render

DRAW_DIR = os.path.join(render.BASE_DIR, "draw")
STROKES_FILE = os.path.join(DRAW_DIR, "strokes.json")
SAVED_DIR = os.path.join(DRAW_DIR, "saved")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "draw_layout.json")
TOOLBAR_H = 110
PENS = {"thin": 6, "thick": 18, "eraser": 60}
TOOLS = [("thin", "Thin"), ("thick", "Thick"), ("eraser", "Eraser"),
         ("undo", "Undo"), ("clear", "Clear"), ("save", "Save")]


def load():
    try:
        with open(STROKES_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("strokes", [])
    st.setdefault("tool", "thin")
    st.setdefault("saved", None)
    return st


def store(st):
    os.makedirs(DRAW_DIR, exist_ok=True)
    with open(STROKES_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STROKES_FILE + ".tmp", STROKES_FILE)


def _paint(d, stroke):
    width, fill = stroke["w"], stroke["c"]
    pts = [tuple(p) for p in stroke["pts"]]
    r = width / 2
    if len(pts) > 1:
        d.line(pts, fill=fill, width=int(width), joint="curve")
    for x, y in pts[:1] + pts[-1:] if len(pts) > 1 else pts:     # round ends / a dot
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def picture(w, h, st):
    """Just the drawing (no toolbar), white background."""
    img = Image.new("L", (w, h - TOOLBAR_H), 255)
    d = ImageDraw.Draw(img)
    for s in st["strokes"]:
        _paint(d, s)
    return img


def draw(w, h, st):
    """Drawing + toolbar. Returns (image, toolbar buttons)."""
    img = Image.new("L", (w, h), 255)
    img.paste(picture(w, h, st), (0, 0))
    d = ImageDraw.Draw(img)
    top = h - TOOLBAR_H
    d.rectangle((0, top, w, h), fill=255)
    d.line((0, top, w, top), fill=0, width=3)
    font = render.ui_font(34, "semibold")
    pad, gap = 30, 16
    bw = (w - 2 * pad - gap * (len(TOOLS) - 1)) / len(TOOLS)
    buttons = []
    for i, (key, label) in enumerate(TOOLS):
        x0 = pad + i * (bw + gap)
        box = (x0, top + 18, x0 + bw, h - 18)
        on = key == st["tool"]
        d.rounded_rectangle(box, radius=18, outline=0, width=3, fill=0 if on else 255)
        if key == "save" and st.get("saved"):
            label = "Saved"
        d.text((x0 + (bw - d.textlength(label, font=font)) / 2, top + 34), label, font=font,
               fill=255 if on else 0)
        buttons.append({"action": key, "box": box})
    return img, buttons


def save_layout(buttons, h):
    with open(LAYOUT_FILE, "w") as f:
        json.dump({"buttons": buttons, "top": h - TOOLBAR_H}, f)


def hit(cx, cy):
    """("tool", name) for a toolbar tap, "canvas" for the drawing area."""
    try:
        with open(LAYOUT_FILE) as f:
            lay = json.load(f)
    except (OSError, ValueError):
        return "canvas"
    if cy < lay["top"]:
        return "canvas"
    for b in lay["buttons"]:
        x0, y0, x1, y1 = b["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return ("tool", b["action"])
    return None


def add_stroke(points, h):
    """points: canvas coordinates of one finger stroke."""
    st = load()
    top = h - TOOLBAR_H
    pts = [[round(x), round(min(y, top - 1))] for x, y in points]
    # thin out points closer than 3px, keeps the file small
    slim = pts[:1]
    for p in pts[1:]:
        if abs(p[0] - slim[-1][0]) + abs(p[1] - slim[-1][1]) >= 3:
            slim.append(p)
    tool = st["tool"]
    st["strokes"].append({"w": PENS[tool], "c": 255 if tool == "eraser" else 0, "pts": slim})
    st["saved"] = None
    store(st)


def save_png(w, h):
    st = load()
    os.makedirs(SAVED_DIR, exist_ok=True)
    path = os.path.join(SAVED_DIR, datetime.now().strftime("drawing-%Y%m%d-%H%M%S.png"))
    picture(w, h, st).save(path)          # canvas orientation = how it looks on the stand
    st["saved"] = os.path.basename(path)
    store(st)
    return path


def tool(action, w, h):
    """Apply a toolbar button. Returns what happened (for the log)."""
    st = load()
    if action in PENS:
        st["tool"] = action
        store(st)
        return "tool " + action
    if action == "undo":
        if st["strokes"]:
            st["strokes"].pop()
            st["saved"] = None
            store(st)
        return "undo"
    if action == "clear":
        st["strokes"], st["saved"] = [], None
        store(st)
        return "clear"
    if action == "save":
        return "saved " + save_png(w, h)
    return None


if __name__ == "__main__":
    import show
    w, h = show.canvas_size()
    if sys.argv[1:] == ["save"]:
        print(save_png(w, h))
    elif sys.argv[1:] == ["clear"]:
        tool("clear", w, h)
        print("cleared")
    else:
        print(__doc__)
