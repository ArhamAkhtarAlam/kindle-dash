"""To-do list: stored in out/todos.json, drawn as a tappable screen.

Used by show.py (commands) and touch.py (taps). Each row on screen is recorded in
out/todo_layout.json so a tap can be matched to the row under it.
"""

import difflib
import fcntl
import json
import os
import shlex
from contextlib import contextmanager
from datetime import datetime

from PIL import Image, ImageDraw

import render

TODO_FILE = os.path.join(render.OUT_DIR, "todos.json")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "todo_layout.json")
LOCK_FILE = os.path.join(render.OUT_DIR, ".todo.lock")
M = 50                  # screen margin, px
MAX_ROW_H = 120         # rows shrink (down to MIN_ROW_H) when the list is long
MIN_ROW_H = 72


# ------------------------------ storage -----------------------------

@contextmanager
def locked():
    """Serialise changes between the Mac commands and the tap listener."""
    os.makedirs(render.OUT_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load():
    try:
        with open(TODO_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(items):
    tmp = TODO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TODO_FILE)


def shell_quote(text):
    """Double quotes when that's safe to paste into a shell, so apostrophes read naturally."""
    return '"%s"' % text if not any(c in text for c in '"$`\\!') else shlex.quote(text)


def find(items, query, verb="remove", noun="todo"):
    """Index of the item whose text is exactly query (any case), or whose number it is.
    Otherwise raises ValueError saying it doesn't exist, with the closest matches."""
    q = query.strip()
    if q.isdigit() and 1 <= int(q) <= len(items):
        return int(q) - 1
    for i, it in enumerate(items):
        if it["text"].casefold() == q.casefold():
            return i
    texts = [it["text"] for it in items]
    folded = {t.casefold(): t for t in texts}
    near = [t for t in texts if q and q.casefold() in t.casefold()]
    near += [folded[t] for t in difflib.get_close_matches(q.casefold(), list(folded), n=3, cutoff=0.4)]
    near = list(dict.fromkeys(near))[:3]
    msg = 'the %s "%s" does not exist' % ({"todo": "task", "slides": "slide", "feeds": "feed", "calendar": "calendar"}.get(noun, "reminder"), q)
    if near:
        msg += "\ndid you mean:\n" + "\n".join("  kindle %s %s %s" % (noun, verb, shell_quote(t))
                                              for t in near)
    elif items:
        msg += "\nyou have:\n" + "\n".join("  %d. %s" % (i + 1, it["text"])
                                          for i, it in enumerate(items))
    raise ValueError(msg)


def format_list(items):
    if not items:
        return "  (empty)"
    return "\n".join("  %d. [%s] %s" % (i + 1, "x" if it["done"] else " ", it["text"])
                     for i, it in enumerate(items))


# ------------------------------ drawing -----------------------------

def fit(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def draw_screen(items, w, h):
    """Draw the list on a w x h canvas. Returns (image, rows) where rows are
    tap targets: {"index", "text", "y0", "y1"} in canvas coordinates."""
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    left = sum(1 for it in items if not it["done"])

    title = render.ui_font(68, "bold")
    small = render.ui_font(32)
    d.text((M, M), "To-do", font=title, fill=0)
    count = "%d left" % left if items else ""
    d.text((w - M - d.textlength(count, font=small), M + 28), count, font=small, fill=0)
    top = M + 100
    d.line((M, top, w - M, top), fill=0, width=3)

    footer_y = h - M - 30
    d.text((M, footer_y), "tap a row to tick it off", font=small, fill=0)
    import kindle                                      # (kindle imports render, not us)
    right = kindle.draw_battery(d, w - M, footer_y, height=32)
    stamp = datetime.now().strftime("%H:%M")
    right = right - 30 if right < w - M else right
    d.text((right - d.textlength(stamp, font=small), footer_y), stamp, font=small, fill=0)

    if not items:
        big = render.ui_font(64, "medium")
        msg, hint = "Nothing to do", 'kindle todo add "..."'
        d.text(((w - d.textlength(msg, font=big)) / 2, h / 2 - 70), msg, font=big, fill=0)
        d.text(((w - d.textlength(hint, font=small)) / 2, h / 2 + 20), hint, font=small, fill=0)
        return img, []

    area_top, area_bot = top + 10, footer_y - 20
    area = area_bot - area_top
    row_h = max(MIN_ROW_H, min(MAX_ROW_H, area // len(items)))
    fits = area // row_h
    shown = items if len(items) <= fits else items[:fits - 1]
    box = int(row_h * 0.5)
    font = render.ui_font(row_h * 0.44)
    text_x = M + box + 30
    rows = []
    y = area_top
    for i, it in enumerate(shown):
        bx, by = M, y + (row_h - box) // 2
        d.rectangle((bx, by, bx + box, by + box), outline=0, width=4)
        text = fit(d, it["text"], font, w - M - text_x)
        ty = y + (row_h - font.size) // 2 - font.size // 8
        if it["done"]:
            d.line((bx + box * 0.2, by + box * 0.55, bx + box * 0.42, by + box * 0.78,
                    bx + box * 0.82, by + box * 0.22), fill=0, width=max(4, box // 8))
            d.text((text_x, ty), text, font=font, fill=110)
            mid = y + row_h // 2
            d.line((text_x, mid, text_x + d.textlength(text, font=font), mid), fill=0, width=3)
        else:
            d.text((text_x, ty), text, font=font, fill=0)
        if i < len(shown) - 1:
            d.line((text_x, y + row_h, w - M, y + row_h), fill=190, width=1)
        rows.append({"index": i, "text": it["text"], "y0": y, "y1": y + row_h})
        y += row_h
    if len(shown) < len(items):
        more = "+ %d more (see kindle todo list)" % (len(items) - len(shown))
        d.text((text_x, y + (row_h - small.size) // 2), more, font=small, fill=0)
    return img, rows


def save_layout(w, h, rows):
    with open(LAYOUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"canvas": [w, h], "rotate": render.ROTATE,
                   "degrees": render.ROTATE_DEGREES, "rows": rows}, f, ensure_ascii=False)


def load_layout():
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def panel_to_canvas(px, py, layout):
    """Undo the 90/270 rotation applied to the canvas (PIL rotates counter-clockwise),
    turning a point on the portrait panel into canvas coordinates."""
    w, h = layout["canvas"]
    if not layout["rotate"]:
        return px, py
    if layout["degrees"] == 90:          # canvas (x, y) -> panel (y, w-1-x)
        return w - 1 - py, px
    return py, h - 1 - px                # 270: canvas (x, y) -> panel (h-1-y, x)


def row_at(layout, cx, cy):
    w, _ = layout["canvas"]
    if not (0 <= cx < w):
        return None
    for r in layout["rows"]:
        if r["y0"] <= cy < r["y1"]:
            return r
    return None
