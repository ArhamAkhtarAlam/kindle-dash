"""Mandelbrot set viewer. Tap the picture to zoom in 3x around that point, swipe to pan
(drag the picture), and use the buttons along the bottom: Zoom out, Back (undo the last
move), Reset, and Detail (more iterations: sharper edges deep down, slower).

Only started with `kindle mandelbrot` (or `kindle fractal`). The escape-time maths is in
fractal/mandel.c, compiled automatically the first time (all 4 cores, a fraction of a
second per screen); numpy is the fallback if that can't be built (e.g. in Kindle Sim).

out/mandel.json  where you are: centre, width of the view, detail, and the history for Back
"""

import ctypes
import json
import math
import os
import subprocess

import numpy as np
from PIL import Image, ImageDraw

import render

STATE_FILE = os.path.join(render.OUT_DIR, "mandel.json")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "mandel_layout.json")
CACHE_FILE = os.path.join(render.OUT_DIR, "mandel_cache.png")
SRC = os.path.join(render.BASE_DIR, "fractal", "mandel.c")
LIB = os.path.join(render.OUT_DIR, "libmandel.so")
HOME = {"cx": -0.65, "cy": 0.0, "width": 3.4}
ZOOM_STEP = 3
MIN_WIDTH = 1e-12          # past this, 64-bit numbers run out of precision (blocky pixels)
BAR = 110                  # height of the button bar at the bottom
DETAIL = {"normal": 1.0, "high": 2.5}
HISTORY = 30


# ------------------------------ state -------------------------------

def load():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for k, v in HOME.items():
        st.setdefault(k, v)
    st.setdefault("detail", "normal")
    st.setdefault("history", [])
    st.setdefault("msg", "")
    return st


def save(st):
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def _move(st, cx, cy, width):
    st["history"] = (st["history"] + [[st["cx"], st["cy"], st["width"]]])[-HISTORY:]
    st.update(cx=cx, cy=cy, width=width, msg="")


def iterations(st):
    zoom = HOME["width"] / st["width"]
    n = (180 + 110 * math.log2(max(zoom, 1))) * DETAIL[st["detail"]]
    return int(min(max(n, 120), 8000))


# ------------------------------ maths -------------------------------

_lib = [None]


def _library():
    """The compiled escape-time function, building it if needed (None if that fails)."""
    if _lib[0] is not None:
        return _lib[0] or None
    try:
        if not os.path.exists(LIB) or os.path.getmtime(LIB) < os.path.getmtime(SRC):
            for flags in (["-fopenmp"], []):              # threads if the compiler has OpenMP
                r = subprocess.run(["cc", "-O3", "-shared", "-fPIC"] + flags + [SRC, "-o", LIB + ".tmp", "-lm"],
                                   capture_output=True)
                if r.returncode == 0:
                    os.replace(LIB + ".tmp", LIB)
                    break
            else:
                raise OSError(r.stderr.decode(errors="replace")[:200])
        lib = ctypes.CDLL(LIB)
        lib.mandel.argtypes = [ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
        _lib[0] = lib
    except OSError as e:
        render.log("mandelbrot: no compiled version (%s), using numpy" % e)
        _lib[0] = False
    return _lib[0] or None


def _numpy(cx, cy, width, w, h, maxit):
    step = width / (w - 1)
    xs = cx + (np.arange(w) - w / 2) * step
    ys = cy + (h / 2 - np.arange(h)) * step
    c = (xs[None, :] + 1j * ys[:, None]).ravel()
    out = np.full(c.size, -1.0, dtype=np.float32)
    q = (c.real - 0.25) ** 2 + c.imag ** 2
    inside = (q * (q + (c.real - 0.25)) <= 0.25 * c.imag ** 2) | ((c.real + 1) ** 2 + c.imag ** 2 <= 0.0625)
    idx = np.nonzero(~inside)[0]
    c = c[idx]
    z = np.zeros_like(c)
    for n in range(maxit):
        z = z * z + c
        a = z.real * z.real + z.imag * z.imag
        esc = a > 256
        if esc.any():
            out[idx[esc]] = n + 1 - np.log2(np.log2(a[esc]) / 2)
            keep = ~esc
            z, c, idx = z[keep], c[keep], idx[keep]
            if not idx.size:
                break
    return out.reshape(h, w)


def escape_times(cx, cy, width, w, h, maxit):
    lib = _library()
    if lib is None:
        return _numpy(cx, cy, width, w, h, maxit)
    out = np.empty(w * h, dtype=np.float32)
    lib.mandel(cx, cy, width, w, h, maxit, out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))
    return out.reshape(h, w)


def shade(v):
    """Smooth iteration counts -> greys: soft repeating bands that stay readable however deep
    you go; the set itself is black."""
    g = np.zeros(v.shape, dtype=np.float32)
    out = v >= 0
    t = np.sqrt(v[out]) * 1.1
    g[out] = 0.5 - 0.5 * np.cos(t)
    g[out] = 0.08 + 0.92 * g[out]                    # (only the set itself is fully black)
    return Image.fromarray((g * 255).astype(np.uint8), "L")


# ------------------------------ drawing -----------------------------

def _buttons(w, h):
    labels = [("out", "Zoom out"), ("back", "Back"), ("reset", "Reset"), ("detail", "Detail")]
    bw, gap, y0 = 200, 14, h - BAR + 16
    x = w - 30 - len(labels) * bw - (len(labels) - 1) * gap
    return [(act, label, (x + i * (bw + gap), y0, x + i * (bw + gap) + bw, h - 16))
            for i, (act, label) in enumerate(labels)]


def draw(w, h):
    """The fractal (cached while you stay put) plus the button bar. Returns (image, targets)."""
    import show
    st = load()
    ph = h - BAR
    key = "%r %r %r %d %dx%d" % (st["cx"], st["cy"], st["width"], iterations(st), w, ph)
    pic = None
    try:
        with Image.open(CACHE_FILE) as im:
            if im.info.get("view") == key:
                pic = im.copy()
    except OSError:
        pass
    if pic is None:
        pic = show.to_16_grays(shade(escape_times(st["cx"], st["cy"], st["width"], w, ph, iterations(st))))
        from PIL import PngImagePlugin
        meta = PngImagePlugin.PngInfo()
        meta.add_text("view", key)
        pic.save(CACHE_FILE, pnginfo=meta, compress_level=1)
    img = Image.new("L", (w, h), 255)
    img.paste(pic, (0, 0))
    d = ImageDraw.Draw(img)
    d.line((0, ph, w, ph), fill=0, width=3)
    zoom = HOME["width"] / st["width"]
    info = st["msg"] or ("×%s zoom  ·  %d iterations%s  ·  tap to zoom, swipe to move"
                         % ("{:,.0f}".format(zoom) if zoom >= 10 else "%.1f" % zoom, iterations(st),
                            "  ·  high detail" if st["detail"] == "high" else ""))
    f = render.ui_font(30)
    buttons = _buttons(w, h)
    room = buttons[0][2][0] - 60
    while d.textlength(info, font=f) > room and f.size > 18:
        f = render.ui_font(f.size - 2)
    d.text((30, ph + BAR / 2), info, font=f, fill=0, anchor="lm")
    bf = render.ui_font(32, "semibold")
    targets = []
    for act, label, box in buttons:
        on = act == "detail" and st["detail"] == "high"
        d.rounded_rectangle(box, radius=16, outline=0, width=3, fill=0 if on else 255)
        d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=bf, fill=255 if on else 0, anchor="mm")
        targets.append({"action": act, "box": list(box)})
    targets.append({"action": "pic", "box": [0, 0, w, ph]})
    with open(LAYOUT_FILE, "w") as fh:
        json.dump({"targets": targets, "size": [w, ph]}, fh)
    return img


# ------------------------------ input -------------------------------

def hit(cx, cy):
    try:
        with open(LAYOUT_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    for t in data["targets"]:
        x0, y0, x1, y1 = t["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return t["action"]
    return None


def _point(st, x, y, w, ph):
    """Canvas pixel -> complex plane."""
    step = st["width"] / (w - 1)
    return st["cx"] + (x - w / 2) * step, st["cy"] + (ph / 2 - y) * step


def tap(action, x=None, y=None, size=None):
    """A button, or "pic" with the tap position. Returns a log line, or None if nothing changed."""
    st = load()
    if action == "pic":
        if st["width"] / ZOOM_STEP < MIN_WIDTH:
            st["msg"] = "That's as deep as it goes (the Pi's numbers run out of precision)"
            save(st)
            return "too deep"
        cx, cy = _point(st, x, y, *size)
        _move(st, cx, cy, st["width"] / ZOOM_STEP)
    elif action == "out":
        _move(st, st["cx"], st["cy"], min(st["width"] * ZOOM_STEP, HOME["width"] * 2))
    elif action == "back":
        if not st["history"]:
            return None
        st["cx"], st["cy"], st["width"] = st["history"].pop()
        st["msg"] = ""
    elif action == "reset":
        if st["history"] or st["width"] != HOME["width"]:
            _move(st, HOME["cx"], HOME["cy"], HOME["width"])
    elif action == "detail":
        st["detail"] = "normal" if st["detail"] == "high" else "high"
    else:
        return None
    save(st)
    return action


def pan(dx, dy, size):
    """A swipe of (dx, dy) canvas pixels drags the picture by that much."""
    st = load()
    step = st["width"] / (size[0] - 1)
    _move(st, st["cx"] - dx * step, st["cy"] + dy * step, st["width"])
    save(st)
    return "pan"


def layout_size():
    try:
        with open(LAYOUT_FILE) as f:
            return json.load(f)["size"]
    except (OSError, ValueError, KeyError):
        return None
