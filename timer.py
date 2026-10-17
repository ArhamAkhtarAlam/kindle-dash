"""Countdown timer with on-screen controls. When it reaches zero the Kindle flashes and
shows "Time's up!" like a reminder (from whatever screen is up).

State is in out/timer.json: "ends" (epoch) while running, otherwise "left" (seconds).
touch.py ticks the big digits every second and watches for the end; push.sh's minute check
is the backup if the tap listener isn't running.
"""

import json
import os
import re
import time
from datetime import datetime

from PIL import Image, ImageDraw

import render

STATE_FILE = os.path.join(render.OUT_DIR, "timer.json")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "timer_layout.json")
ADD_BUTTONS = (1, 5, 10, 25)          # minutes
M = 60
DIGITS_Y = 190                         # top of the big countdown (canvas)


def load():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("ends", None)
    st.setdefault("left", 0)
    st.setdefault("set", 0)
    st.setdefault("fired", True)
    return st


def save(st):
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def remaining(st):
    if st["ends"]:
        return max(0.0, st["ends"] - time.time())
    return float(st["left"])


def running(st):
    return bool(st["ends"])


def parse_duration(text):
    """'25m', '90s', '1h30m', '1:30' (min:sec), '10' (minutes) -> seconds, or None."""
    t = text.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+", t):
        return int(t) * 60
    m = re.fullmatch(r"(\d+):(\d{1,2})", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?:in)?)?(?:(\d+)s(?:ec)?)?", t)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    return None


# ----------------------------- actions ------------------------------

def add(seconds):
    st = load()
    if running(st):
        st["ends"] += seconds
    else:
        st["left"] = max(0, st["left"] + seconds)
    st["set"] = max(0, st["set"] + seconds)
    st["fired"] = False
    save(st)


def start():
    st = load()
    if not running(st) and st["left"] > 0:
        st["ends"], st["fired"] = time.time() + st["left"], False
        save(st)


def pause():
    st = load()
    if running(st):
        st["left"], st["ends"] = round(remaining(st)), None
        save(st)


def reset():
    save({"ends": None, "left": 0, "set": 0, "fired": True})


def set_and_start(seconds):
    save({"ends": time.time() + seconds, "left": seconds, "set": seconds, "fired": False})


def check_done():
    """If the timer just reached zero: mark it done and return the alert text, else None."""
    st = load()
    if running(st) and not st["fired"] and time.time() >= st["ends"]:
        total = st["set"]
        st.update(ends=None, left=0, fired=True)
        save(st)
        return "Time's up!\n%s timer" % _fmt(total)
    return None


# ------------------------------ drawing -----------------------------

def _fmt(sec):
    sec = int(round(sec))
    h, rest = divmod(sec, 3600)
    return "%d:%02d:%02d" % (h, rest // 60, rest % 60) if h else "%d:%02d" % (rest // 60, rest % 60)


def digits_box(w):
    return (M, DIGITS_Y, w - M, DIGITS_Y + 420)


def digits_strip(w, st):
    """The countdown + status line as their own image (the per-second update)."""
    x0, y0, x1, y1 = digits_box(w)
    img = Image.new("L", (x1 - x0, y1 - y0), 255)
    d = ImageDraw.Draw(img)
    big = render.ui_font(300, "medium")
    text = _fmt(remaining(st)) if not running(st) else _fmt(int(remaining(st) + 0.999))
    d.text(((x1 - x0 - d.textlength(text, font=big)) / 2, 0), text, font=big, fill=0)
    f = render.ui_font(40)
    if running(st):
        status = "running · done at %s" % datetime.fromtimestamp(st["ends"]).strftime("%-I:%M %p").lower()
    elif st["left"]:
        status = "paused"
    else:
        status = "add some time, then Start"
    d.text(((x1 - x0 - d.textlength(status, font=f)) / 2, 350), status, font=f, fill=0)
    return img


def _button(d, box, text, font, filled=False):
    d.rounded_rectangle(box, radius=22, outline=0, width=4, fill=0 if filled else 255)
    d.text(((box[0] + box[2] - d.textlength(text, font=font)) / 2,
            (box[1] + box[3]) / 2 - font.size * 0.62), text, font=font, fill=255 if filled else 0)


def draw(w, h):
    """Whole timer screen. Returns (image, targets)."""
    st = load()
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.text((M, 40), "Timer", font=render.ui_font(56, "bold"), fill=0)
    x0, y0, x1, y1 = digits_box(w)
    img.paste(digits_strip(w, st), (x0, y0))
    targets = []
    btn = render.ui_font(40, "semibold")
    gap = 24
    row1 = h - 330
    bw = (w - 2 * M - gap * (len(ADD_BUTTONS) - 1)) / len(ADD_BUTTONS)
    for i, mins in enumerate(ADD_BUTTONS):
        box = (M + i * (bw + gap), row1, M + i * (bw + gap) + bw, row1 + 110)
        _button(d, box, "+%d min" % mins, btn)
        targets.append({"action": "add:%d" % (mins * 60), "box": box})
    row2 = h - 190
    main_w = (w - 2 * M - gap) * 0.62
    main = (M, row2, M + main_w, row2 + 130)
    label = "Pause" if running(st) else "Start"
    _button(d, main, label, render.ui_font(52, "bold"), filled=True)
    targets.append({"action": "pause" if running(st) else "start", "box": main})
    rbox = (M + main_w + gap, row2, w - M, row2 + 130)
    _button(d, rbox, "Reset", render.ui_font(48, "semibold"))
    targets.append({"action": "reset", "box": rbox})
    return img, targets


def save_layout(targets):
    with open(LAYOUT_FILE, "w") as f:
        json.dump(targets, f)


def hit(cx, cy):
    try:
        with open(LAYOUT_FILE) as f:
            targets = json.load(f)
    except (OSError, ValueError):
        return None
    for t in targets:
        x0, y0, x1, y1 = t["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return t["action"]
    return None


def tap(action):
    if action.startswith("add:"):
        add(int(action[4:]))
    elif action == "start":
        start()
    elif action == "pause":
        pause()
    elif action == "reset":
        reset()
    else:
        return None
    return action
