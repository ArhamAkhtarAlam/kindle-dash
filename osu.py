#!/usr/bin/env python3
"""osu! on the Kindle (a simplified version for e-ink). Only started with `kindle osu` on the Mac,
which plays the song (the Kindle has no speaker) and starts this in step with it.

The Pi runs the game and draws it on the Kindle like DOOM (one ssh connection, FBInk's fast
black/white refresh); touches are read straight from the Kindle with their own timestamps,
so a tap is judged by when you tapped, not when it arrived.

Simplified for e-ink: circles appear earlier than in osu! (MIN_PREEMPT), the timing windows
are LENIENCY times wider, a slider is a tap on its start (its path is drawn), and a spinner
counts any tap while it's up.

  osu.py play FILE.osu [--offset MS] [--mania KEYS] [--no-kindle]   play (waits for "GO" on stdin);
                                      mania maps play as mania; --mania converts a standard map
  osu.py demo                                         make the built-in metronome map (osu/demo.*)
  osu.py pad [KEYS]                                   the Kindle as osu!mania keys for the Mac (prints D/U lane)

Talking to the Mac (stdin/stdout lines): we print "READY <lead ms>" when the Kindle is set up;
the Mac answers "GO" and starts the music <lead ms> later. "QUIT" (or the Mac going away)
ends it. At the end we print "RESULT ..." and put the dashboard back.
"""

import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw

import kindle
import render

OSU_DIR = os.path.join(render.BASE_DIR, "osu")
STATE_FILE = os.path.join(render.OUT_DIR, ".osu")            # pid while playing (touch.py steps aside)
PAUSE_FILE = os.path.join(render.OUT_DIR, ".pause")
MIN_PREEMPT = 1200          # ms: circles show at least this long before you hit them
LENIENCY = 1.5              # timing windows x this (touch + e-ink delay)
AUDIO_LATENCY = 60          # ms the Mac's player takes to start (plus `kindle osu offset`)
DISPLAY_AHEAD = 0.07        # s: draw where things will be when the frame is actually on screen
MIN_LEAD = 2000             # ms before the first circle
HUD_H = 70
QUIT_BOX = (1448 - 190, 8, 1448 - 12, 62)


# ------------------------------ beatmaps ----------------------------

def parse(text):
    """An .osu file -> {"meta", "cs", "od", "ar", "lead", "objects"}. Objects are dicts with
    kind circle/slider/spinner, time (ms), x, y (osu! pixels, 512x384), combo number, and for
    sliders the path points, for spinners the end time."""
    section, meta, diff, objs = None, {}, {}, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            section = m.group(1)
            continue
        if section in ("General", "Metadata", "Difficulty") and ":" in line:
            k, v = line.split(":", 1)
            (diff if section == "Difficulty" else meta)[k.strip()] = v.strip()
        elif section == "HitObjects":
            p = line.split(",")
            if len(p) < 5:
                continue
            x, y, t, typ = float(p[0]), float(p[1]), int(float(p[2])), int(p[3])
            o = {"x": x, "y": y, "time": t, "new_combo": bool(typ & 4)}
            if typ & 128 and len(p) > 5:                  # mania hold note: end time before ":"
                o.update(kind="hold", end=int(float(p[5].split(":")[0])))
            elif typ & 2 and len(p) > 5:
                parts = p[5].split("|")
                pts = [(x, y)]
                for q in parts[1:]:
                    if ":" in q:
                        a, b = q.split(":")
                        pts.append((float(a), float(b)))
                length = float(p[7]) if len(p) > 7 and p[7] else None
                o.update(kind="slider", path=slider_path(parts[0], pts, length))
            elif typ & 8:
                o.update(kind="spinner", end=int(float(p[5])) if len(p) > 5 else t + 1000, x=256, y=192)
            else:
                o.update(kind="circle")
            objs.append(o)
    objs.sort(key=lambda o: o["time"])
    n = 0
    for i, o in enumerate(objs):
        n = 1 if i == 0 or o["new_combo"] else n + 1
        o["number"] = n
    od = float(diff.get("OverallDifficulty", 5))
    return {"meta": meta, "mode": int(float(meta.get("Mode", 0) or 0)), "cs": float(diff.get("CircleSize", 4)), "od": od,
            "ar": float(diff.get("ApproachRate", od)), "lead": int(float(meta.get("AudioLeadIn", 0))),
            "objects": objs}


def _bezier(ctrl, steps=24):
    out = []
    for i in range(steps + 1):
        t = i / steps
        pts = list(ctrl)
        while len(pts) > 1:                                # de Casteljau
            pts = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(pts, pts[1:])]
        out.append(pts[0])
    return out


def _arc(a, b, c):
    """Points on the circle through a, b, c (from a, via b, to c); None if they're in a line."""
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-6:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    t0, t1, t2 = (math.atan2(py - uy, px - ux) for px, py in (a, b, c))
    span = (t2 - t0) % (2 * math.pi)
    if (t1 - t0) % (2 * math.pi) > span:                   # b isn't between a and c going this way
        span -= 2 * math.pi
    n = max(8, int(abs(span) * r / 8))
    return [(ux + r * math.cos(t0 + span * i / n), uy + r * math.sin(t0 + span * i / n)) for i in range(n + 1)]


def slider_path(kind, pts, length=None):
    """The slider's actual curve (osu! pixels): B = Bezier (split at repeated points), P = circular
    arc, L/C = through the points. Cut to the slider's length when the map gives it."""
    if kind == "P" and len(pts) == 3:
        out = _arc(*pts) or pts
    elif kind == "B":
        out, seg = [], [pts[0]]
        for q in pts[1:]:
            if q == seg[-1]:                               # a repeated point starts a new curve
                out += _bezier(seg)
                seg = [q]
            else:
                seg.append(q)
        out += _bezier(seg)
    else:
        out = pts
    if length:
        cut, total = [out[0]], 0.0
        for a, b in zip(out, out[1:]):
            step = math.hypot(b[0] - a[0], b[1] - a[1])
            if total + step >= length:
                f = (length - total) / step if step else 0
                cut.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
                break
            total += step
            cut.append(b)
        out = cut
    return out


def _dense(pts, step):
    """Points every `step` pixels along a polyline."""
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
    return out


def preempt_ms(ar):
    p = 1200 + 600 * (5 - ar) / 5 if ar < 5 else 1200 - 750 * (ar - 5) / 5
    return max(p, MIN_PREEMPT)


def windows(od):
    """(300, 100, 50) hit windows in ms, widened for the Kindle."""
    return tuple(w * LENIENCY for w in (80 - 6 * od, 140 - 8 * od, 200 - 10 * od))


# ------------------------------ the game ----------------------------

class Game:
    def __init__(self, beatmap, w, h):
        self.map = beatmap
        self.w, self.h = w, h
        r_osu = 54.4 - 4.48 * beatmap["cs"]
        avail_h = h - HUD_H - 10
        self.s = min(w / (512 + 2 * r_osu), avail_h / (384 + 2 * r_osu))
        self.r = r_osu * self.s
        self.ox = (w - 512 * self.s) / 2
        self.oy = HUD_H + 5 + (avail_h - 384 * self.s) / 2
        self.preempt = preempt_ms(beatmap["ar"])
        self.w300, self.w100, self.w50 = windows(beatmap["od"])
        self.objs = beatmap["objects"]
        for o in self.objs:
            o.update(result=None, spins=0)
        self.score = self.combo = self.max_combo = 0
        self.counts = {300: 0, 100: 0, 50: 0, 0: 0}
        self.popups = []                                 # (x, y, text, until ms)
        self.lock = threading.Lock()
        self.end_time = max((o.get("end", o["time"]) for o in self.objs), default=0) + 1500

    def xy(self, x, y):
        return self.ox + x * self.s, self.oy + y * self.s

    def _judge(self, o, value, t):
        o["result"] = value
        self.counts[value] += 1
        if value:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            self.score += int(value * (1 + self.combo / 25))
        else:
            self.combo = 0
        x, y = self.xy(o["x"], o["y"])
        self.popups.append((x, y, {300: "300", 100: "100", 50: "50", 0: "✕"}[value], t + 450))

    def press(self, slot, cx, cy, t):
        return self.tap(cx, cy, t)

    def release(self, slot, t):
        pass

    def tap(self, cx, cy, t):
        """A finger came down at canvas (cx, cy), song time t (ms)."""
        with self.lock:
            for o in self.objs:
                if o["kind"] == "spinner" and o["result"] is None and o["time"] <= t <= o["end"]:
                    o["spins"] += 1
                    return "spin"
            for o in self.objs:                          # the earliest circle you could mean
                if o["result"] is not None or o["kind"] == "spinner":
                    continue
                if t < o["time"] - self.w50:
                    break
                if abs(t - o["time"]) > self.w50:
                    continue
                x, y = self.xy(o["x"], o["y"])
                if math.hypot(cx - x, cy - y) <= self.r * 1.2:
                    err = abs(t - o["time"])
                    self._judge(o, 300 if err <= self.w300 else 100 if err <= self.w100 else 50, t)
                    return o["result"]
            return None

    def update(self, t):
        """Misses, finished spinners. True once the song's over."""
        with self.lock:
            for o in self.objs:
                if o["result"] is not None:
                    continue
                if o["kind"] == "spinner":
                    if t > o["end"]:
                        self._judge(o, 300 if o["spins"] >= 2 else 100 if o["spins"] else 0, t)
                elif t > o["time"] + self.w50:
                    self._judge(o, 0, t)
                elif o["time"] - self.preempt > t:
                    break
            self.popups = [p for p in self.popups if p[3] > t]
            return t > self.end_time

    def accuracy(self):
        n = sum(self.counts.values())
        return 100.0 if not n else 100.0 * (300 * self.counts[300] + 100 * self.counts[100]
                                           + 50 * self.counts[50]) / (300 * n)

    # -------------------------------------------------------------- drawing

    def frame(self, t):
        img = Image.new("1", (self.w, self.h), 1)
        d = ImageDraw.Draw(img)
        r = self.r
        num_font = render.ui_font(int(r * 0.85), "bold")
        with self.lock:
            visible = [o for o in self.objs if o["result"] is None
                       and o["time"] - self.preempt <= t <= o.get("end", o["time"]) + self.w50]
            for o in reversed(visible):                  # the next one to hit ends up on top
                if o["kind"] == "spinner":
                    cx, cy = self.xy(256, 192)
                    rr = min(self.w, self.h - HUD_H) * 0.36
                    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=0, width=10)
                    left = max(0, (o["end"] - t) / 1000)
                    d.text((cx, cy - 30), "SPIN!", font=render.ui_font(90, "bold"), fill=0, anchor="mm")
                    d.text((cx, cy + 60), "tap anywhere  ·  %.1fs" % left, font=render.ui_font(40), fill=0, anchor="mm")
                    continue
                x, y = self.xy(o["x"], o["y"])
                if o["kind"] == "slider" and len(o["path"]) > 1:
                    pts = _dense([self.xy(px, py) for px, py in o["path"]], 5)
                    for fill, rr in ((0, r), (1, r - 7)):     # discs along the path: a clean outline
                        for px, py in pts:
                            d.ellipse((px - rr, py - rr, px + rr, py + rr), fill=fill)
                d.ellipse((x - r, y - r, x + r, y + r), fill=1, outline=0, width=9)
                d.text((x, y), str(o["number"]), font=num_font, fill=0, anchor="mm")
                ahead = (o["time"] - t) / self.preempt
                if ahead > 0:
                    ar = r * (1 + 2.6 * ahead)
                    d.ellipse((x - ar, y - ar, x + ar, y + ar), outline=0, width=5)
            pf = render.ui_font(54, "bold")
            for x, y, text, _ in self.popups:
                d.text((x, y - r - 20), text, font=pf, fill=0, anchor="mm", stroke_width=6, stroke_fill=1)
            # HUD
            d.rectangle((0, 0, self.w, HUD_H), fill=1)
            d.line((0, HUD_H, self.w, HUD_H), fill=0, width=3)
            hf = render.ui_font(44, "bold")
            d.text((24, HUD_H / 2), "{:,}".format(self.score), font=hf, fill=0, anchor="lm")
            d.text((self.w / 2 - 140, HUD_H / 2), "%dx" % self.combo, font=hf, fill=0, anchor="mm")
            d.text((self.w / 2 + 120, HUD_H / 2), "%.1f%%" % self.accuracy(), font=hf, fill=0, anchor="mm")
            frac = min(1, max(0, t / max(self.end_time, 1)))
            d.rectangle((0, HUD_H - 8, self.w * frac, HUD_H), fill=0)
            d.rounded_rectangle(QUIT_BOX, radius=12, outline=0, width=3, fill=1)
            d.text(((QUIT_BOX[0] + QUIT_BOX[2]) / 2, (QUIT_BOX[1] + QUIT_BOX[3]) / 2), "Quit",
                   font=render.ui_font(32, "bold"), fill=0, anchor="mm")
        return img

    def results(self):
        img = Image.new("L", (self.w, self.h), 255)
        d = ImageDraw.Draw(img)
        meta = self.map["meta"]
        title = "%s - %s" % (meta.get("Artist", "?"), meta.get("Title", "?"))
        d.text((self.w / 2, 120), title, font=render.ui_font(54, "bold"), fill=0, anchor="mm")
        d.text((self.w / 2, 190), "[%s]" % meta.get("Version", ""), font=render.ui_font(40), fill=0, anchor="mm")
        acc = self.accuracy()
        n = sum(self.counts.values())
        grade = "SS" if acc == 100 else "S" if acc > 95 and not self.counts[0] else "A" if acc > 90 else \
            "B" if acc > 80 else "C" if acc > 70 else "D"
        d.text((self.w / 2, 400), grade, font=render.ui_font(260, "bold"), fill=0, anchor="mm")
        lines = ["Score  {:,}".format(self.score), "Accuracy  %.2f%%   ·   Max combo  %dx / %d" % (acc, self.max_combo, n),
                 "300: %d    100: %d    50: %d    miss: %d" % (self.counts[300], self.counts[100], self.counts[50], self.counts[0])]
        for k, line in enumerate(lines):
            d.text((self.w / 2, 640 + k * 80), line, font=render.ui_font(48 if k == 0 else 40, "bold" if k == 0 else "regular"),
                   fill=0, anchor="mm")
        d.text((self.w / 2, self.h - 70), "Back to the dashboard in a few seconds", font=render.ui_font(30), fill=0, anchor="mm")
        return img

    def result_line(self):
        return "RESULT score=%d acc=%.2f max_combo=%d 300=%d 100=%d 50=%d miss=%d" % (
            self.score, self.accuracy(), self.max_combo, self.counts[300], self.counts[100],
            self.counts[50], self.counts[0])


# ------------------------------ osu!mania ---------------------------

MANIA_SCROLL_MS = 1300      # how long a note takes to fall to the line (longer = slower, easier to read)
MANIA_HIT_Y = 930           # the hit line (canvas y)


class Mania(Game):
    """Falling notes in lanes: tap a lane as its note reaches the line; hold notes are held to
    the end. keys=None: a mania map (lanes from CircleSize); a number: convert a standard map,
    each note's lane from its x position (like osu!'s own conversions)."""

    def __init__(self, beatmap, w, h, keys=None):
        self.map = beatmap
        self.w, self.h = w, h
        self.keys = int(keys or round(beatmap["cs"]) or 4)
        self.lane_w = min(170, (w - 80) // self.keys)
        self.x0 = (w - self.keys * self.lane_w) // 2
        od = beatmap["od"]
        self.w300, self.w100, self.w50 = (x * LENIENCY for x in (64 - 3 * od, 127 - 3 * od, 151 - 3 * od))
        self.objs = []
        last = {}
        for o in beatmap["objects"]:
            if o["kind"] == "spinner":
                continue
            lane = min(self.keys - 1, max(0, int(o["x"] * self.keys / 512)))
            if keys and last.get(lane, -1e9) > o["time"] - 80:
                lane = min(range(self.keys), key=lambda l: last.get(l, -1e9))   # (converted: avoid jacks)
            n = {"time": o["time"], "lane": lane, "x": 0, "y": 0, "kind": "hold" if o["kind"] == "hold" else "note",
                 "end": o.get("end", o["time"]), "result": None, "head": None}
            last[lane] = n["end"]
            self.objs.append(n)
        self.objs.sort(key=lambda o: o["time"])
        self.preempt = MANIA_SCROLL_MS
        self.score = self.combo = self.max_combo = 0
        self.counts = {300: 0, 100: 0, 50: 0, 0: 0}
        self.popups = []
        self.down = {}                                     # slot -> lane being held
        self.lock = threading.Lock()
        self.end_time = max((o["end"] for o in self.objs), default=0) + 1500

    def lane_of(self, cx):
        k = int((cx - self.x0) // self.lane_w)
        return k if 0 <= k < self.keys else None

    def _judge(self, o, value, t):
        o["result"] = value
        self.counts[value] += 1
        if value:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            self.score += int(value * (1 + self.combo / 25))
        else:
            self.combo = 0
        self.popups = [(self.w / 2, MANIA_HIT_Y - 330, {300: "300", 100: "100", 50: "50", 0: "MISS"}[value], t + 400)]

    def press(self, slot, cx, cy, t):
        lane = self.lane_of(cx)
        if lane is None:
            return None
        with self.lock:
            self.down[slot] = lane
            for o in self.objs:
                if o["lane"] != lane or o["result"] is not None or o["head"] is not None:
                    continue
                if t < o["time"] - self.w50:
                    return None                            # too early: nothing yet
                err = abs(t - o["time"])
                if err > self.w50:
                    continue
                v = 300 if err <= self.w300 else 100 if err <= self.w100 else 50
                if o["kind"] == "hold":
                    o["head"] = v                           # judged when you let go
                    o["slot"] = slot
                else:
                    self._judge(o, v, t)
                return v
        return None

    def release(self, slot, t):
        with self.lock:
            self.down.pop(slot, None)
            for o in self.objs:
                if o["kind"] == "hold" and o["result"] is None and o["head"] is not None and o.get("slot") == slot:
                    self._judge(o, o["head"] if t >= o["end"] - self.w100 else 0, t)   # let go too soon = miss

    def update(self, t):
        with self.lock:
            for o in self.objs:
                if o["result"] is not None:
                    continue
                if o["kind"] == "hold" and o["head"] is not None:
                    if t > o["end"] + self.w50:             # held all the way: done
                        self._judge(o, o["head"], t)
                elif t > o["time"] + self.w50:
                    self._judge(o, 0, t)
                elif o["time"] - self.preempt > t:
                    break
            self.popups = [p for p in self.popups if p[3] > t]
            return t > self.end_time

    def frame(self, t):
        img = Image.new("1", (self.w, self.h), 1)
        d = ImageDraw.Draw(img)
        lw, x0, hy = self.lane_w, self.x0, MANIA_HIT_Y
        speed = (hy - HUD_H) / self.preempt
        with self.lock:
            for k in range(self.keys + 1):
                d.line((x0 + k * lw, HUD_H, x0 + k * lw, self.h), fill=0, width=3)
            held = set(self.down.values())
            for k in range(self.keys):                     # the keys under the line
                box = (x0 + k * lw + 8, hy + 22, x0 + (k + 1) * lw - 8, self.h - 14)
                d.rectangle(box, fill=0 if k in held else 1, outline=0, width=3)
            for o in self.objs:
                if o["result"] is not None:
                    continue
                if o["time"] - self.preempt > t:
                    break
                y_head = hy - (o["time"] - t) * speed
                y_tail = hy - (o["end"] - t) * speed
                lx = x0 + o["lane"] * lw
                if o["kind"] == "hold":
                    top = max(HUD_H, y_tail)
                    bottom = min(hy, y_head) if o["head"] is not None else y_head
                    if bottom > top:
                        d.rectangle((lx + 26, top, lx + lw - 26, bottom), fill=1, outline=0, width=6)
                if o["head"] is None and HUD_H < y_head < hy + 60:
                    d.rectangle((lx + 10, y_head - 30, lx + lw - 10, y_head), fill=0)
            d.line((x0, hy, x0 + self.keys * lw, hy), fill=0, width=8)
            pf = render.ui_font(64, "bold")
            for x, y, text, _ in self.popups:
                d.text((x, y), text, font=pf, fill=0, anchor="mm", stroke_width=6, stroke_fill=1)
            d.rectangle((0, 0, self.w, HUD_H), fill=1)
            d.line((0, HUD_H, self.w, HUD_H), fill=0, width=3)
            hf = render.ui_font(44, "bold")
            d.text((24, HUD_H / 2), "{:,}".format(self.score), font=hf, fill=0, anchor="lm")
            d.text((self.w / 2 - 140, HUD_H / 2), "%dx" % self.combo, font=hf, fill=0, anchor="mm")
            d.text((self.w / 2 + 120, HUD_H / 2), "%.1f%%" % self.accuracy(), font=hf, fill=0, anchor="mm")
            frac = min(1, max(0, t / max(self.end_time, 1)))
            d.rectangle((0, HUD_H - 8, self.w * frac, HUD_H), fill=0)
            d.rounded_rectangle(QUIT_BOX, radius=12, outline=0, width=3, fill=1)
            d.text(((QUIT_BOX[0] + QUIT_BOX[2]) / 2, (QUIT_BOX[1] + QUIT_BOX[3]) / 2), "Quit",
                   font=render.ui_font(32, "bold"), fill=0, anchor="mm")
        return img


# ------------------------------ touches -----------------------------

class Touches:
    """Reads the Kindle's touch device over its own ssh connection; calls on_tap(cx, cy, t_pi)
    for each finger that comes down, with t_pi the moment it touched, on the Pi's clock."""

    def __init__(self, ip, on_tap, on_up=None):
        import todo
        import touch
        self.touch, self.on_tap, self.on_up = touch, on_tap, on_up
        self.layout = {"canvas": [render.HEIGHT, render.WIDTH] if render.ROTATE else [render.WIDTH, render.HEIGHT],
                       "rotate": render.ROTATE, "degrees": render.ROTATE_DEGREES}
        self.to_canvas = lambda x, y: todo.panel_to_canvas(*touch.raw_to_panel(x, y), self.layout)
        device, _ = touch.find_device(ip)
        self.proc = subprocess.Popen(touch.ssh_cmd("exec cat " + device, ip), stdout=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.offset = None                                # Pi monotonic - Kindle clock (smallest seen)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        t = self.touch
        buf, slot, fingers, pending = b"", 0, {}, []
        while True:
            chunk = self.proc.stdout.read1(4096)
            if not chunk:
                return
            now = time.monotonic()
            buf += chunk
            while len(buf) >= t.EVENT.size:
                sec, usec, etype, code, value = t.EVENT.unpack_from(buf)
                buf = buf[t.EVENT.size:]
                ts = sec + usec / 1e6
                gap = now - ts
                self.offset = gap if self.offset is None else min(self.offset, gap)
                f = fingers.setdefault(slot, {"x": None, "y": None, "down": False})
                if etype == t.EV_ABS:
                    if code == 0x2F:                      # ABS_MT_SLOT
                        slot = value
                    elif code in (t.ABS_MT_POSITION_X, t.ABS_X):
                        f["x"] = value
                    elif code in (t.ABS_MT_POSITION_Y, t.ABS_Y):
                        f["y"] = value
                    elif code == t.ABS_MT_TRACKING_ID:
                        if value >= 0 and not f["down"]:
                            pending.append(slot)
                        elif value < 0 and f["down"] and self.on_up:
                            self.on_up(slot, ts + self.offset)
                        f["down"] = value >= 0
                elif etype == t.EV_KEY and code == t.BTN_TOUCH and value and not pending:
                    # single-touch drivers only: on multi-touch ones this "screen touched" often comes a
                    # frame after the finger's own event, and must not count as a second press
                    if not any(g.get("down") for g in fingers.values()):
                        f["down"] = True
                        pending.append(slot)
                elif etype == t.EV_KEY and code == t.BTN_TOUCH and not value and self.on_up:
                    for s_, g in fingers.items():         # last finger lifted: release any still down
                        if g.get("down"):
                            g["down"] = False
                            self.on_up(s_, ts + self.offset)
                elif etype == t.EV_SYN:
                    for s in pending:
                        g = fingers.get(s, {})
                        if g.get("x") is not None and g.get("y") is not None:
                            cx, cy = self.to_canvas(g["x"], g["y"])
                            self.on_tap(cx, cy, ts + self.offset, s)
                    pending = []

    def close(self):
        self.proc.kill()


# ------------------------------ playing -----------------------------

def active():
    try:
        with open(STATE_FILE) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def play(path, offset=0, use_kindle=True, mania_keys=None):
    import doom
    import show
    with open(path, encoding="utf-8", errors="replace") as f:
        bm = parse(f.read())
    if not bm["objects"]:
        print("ERROR no hit objects in that map", flush=True)
        return 1
    w, h = show.canvas_size()
    if bm["mode"] == 3 or mania_keys:
        game = Mania(bm, w, h, keys=None if bm["mode"] == 3 else mania_keys)
    else:
        game = Game(bm, w, h)
    lead = max(bm["lead"], MIN_LEAD - bm["objects"][0]["time"], 0)
    screen = touches = None
    if use_kindle:
        ip, _ = kindle.address()
        if not ip:
            print("ERROR Kindle not reachable", flush=True)
            return 1
        for p in (STATE_FILE, PAUSE_FILE):
            with open(p, "w") as f:
                f.write(str(os.getpid()))
        screen = doom.Kindle(ip)
    started = {"t0": None}
    quit_flag = threading.Event()

    def song_time(t_pi):
        return (t_pi - started["t0"]) * 1000 if started["t0"] else -1e9

    def on_tap(cx, cy, t_pi, slot=0):
        if QUIT_BOX[0] <= cx <= QUIT_BOX[2] and QUIT_BOX[1] <= cy <= QUIT_BOX[3]:
            quit_flag.set()
            return
        if started["t0"]:
            game.press(slot, cx, cy, song_time(t_pi))

    def on_up(slot, t_pi):
        if started["t0"]:
            game.release(slot, song_time(t_pi))

    def on_signal(signum, frame):
        quit_flag.set()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)

    def read_stdin():
        for line in sys.stdin:
            cmd = line.strip()
            if cmd == "GO" and not started["t0"]:
                started["t0"] = time.monotonic() + (lead + AUDIO_LATENCY + offset) / 1000
            elif cmd == "QUIT":
                break
            elif cmd.startswith("TAP "):                  # (tests: "TAP x y songms")
                _, x, y, ms = cmd.split()
                if started["t0"]:
                    game.press(0, float(x), float(y), float(ms))
                    game.release(0, float(ms) + 1)
        quit_flag.set()

    shown = 0
    try:
        if use_kindle:
            touches = Touches(ip, on_tap, on_up)
            ready = game.frame(-1e9)
            d = ImageDraw.Draw(ready)
            d.text((w / 2, h / 2), "Get ready…", font=render.ui_font(80, "bold"), fill=0, anchor="mm")
            _push(screen, ready, "FULL")
        threading.Thread(target=read_stdin, daemon=True).start()
        print("READY %d" % lead, flush=True)
        while not quit_flag.is_set():
            if not started["t0"]:
                time.sleep(0.01)
                continue
            t = song_time(time.monotonic())
            if game.update(t):
                break
            img = game.frame(song_time(time.monotonic() + DISPLAY_AHEAD))
            if screen:
                if not _push(screen, img, "A2"):
                    raise RuntimeError("lost the connection to the Kindle")
                shown += 1
                if shown % 20 == 0:
                    os.utime(PAUSE_FILE)
            else:
                time.sleep(0.12)                          # (no Kindle: about the same pace)
                shown += 1
        print(game.result_line() + " frames=%d" % shown, flush=True)
        if screen and not quit_flag.is_set():
            _push(screen, game.results(), "FULL")
            time.sleep(6)
    except Exception as e:
        print("ERROR %s" % e, flush=True)
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        if touches:
            touches.close()
        if screen:
            screen.close()
            for p in (STATE_FILE, PAUSE_FILE):
                try:
                    os.remove(p)
                except OSError:
                    pass
            show.refresh_screen(force=True)
            show.push_now()
    return 0


def _push(screen, img, wave):
    if render.ROTATE:
        img = img.rotate(render.ROTATE_DEGREES, expand=True)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return screen.show(buf.getvalue(), 0, 0, wave)


# ------------------------------ controller --------------------------
# `kindle osu pad`: official osu!mania on the Mac, the Kindle as its keys. We print "D <lane>" when
# a finger lands on a lane and "U <lane>" when it lifts; the Mac presses and releases the key.

PAD_LABELS = {1: "Space", 2: "F J", 3: "F Space J", 4: "D F J K", 5: "D F Space J K", 6: "S D F J K L",
              7: "S D F Space J K L", 8: "A S D F J K L ;"}


def _pad_image(keys, w, h):
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.text((24, 36), "osu!mania pad  ·  play on your Mac", font=render.ui_font(38, "bold"), fill=0, anchor="lm")
    d.rounded_rectangle(QUIT_BOX, radius=12, outline=0, width=3)
    d.text(((QUIT_BOX[0] + QUIT_BOX[2]) / 2, (QUIT_BOX[1] + QUIT_BOX[3]) / 2), "Stop",
           font=render.ui_font(32, "bold"), fill=0, anchor="mm")
    names = PAD_LABELS.get(keys, "").replace("Space", "␣").split()
    lw = w / keys
    for k in range(keys):
        box = (k * lw + 10, HUD_H + 10, (k + 1) * lw - 10, h - 10)
        d.rounded_rectangle(box, radius=24, outline=0, width=6, fill=235 if k % 2 else 255)
        if k < len(names):
            d.text(((box[0] + box[2]) / 2, h - 90), names[k], font=render.ui_font(64, "bold"), fill=0, anchor="mm")
    return img


def pad(keys):
    import doom
    import show
    ip, _ = kindle.address()
    if not ip:
        print("ERROR Kindle not reachable", flush=True)
        return 1
    for p in (STATE_FILE, PAUSE_FILE):
        with open(p, "w") as f:
            f.write(str(os.getpid()))
    w, h = show.canvas_size()
    quit_flag = threading.Event()
    lanes = {}                                          # finger slot -> lane it pressed
    out_lock = threading.Lock()

    def say(line):
        with out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def on_tap(cx, cy, t_pi, slot=0):
        if QUIT_BOX[0] <= cx <= QUIT_BOX[2] and QUIT_BOX[1] <= cy <= QUIT_BOX[3]:
            quit_flag.set()
            return
        if cy < HUD_H:
            return
        lane = min(keys - 1, max(0, int(cx * keys / w)))
        if lanes.get(slot) == lane:
            return                                       # (that finger is already holding this lane)
        old = lanes.get(slot)
        if old is not None:
            say("U %d" % old)
        lanes[slot] = lane
        say("D %d" % lane)

    def on_up(slot, t_pi):
        lane = lanes.pop(slot, None)
        if lane is not None:
            say("U %d" % lane)

    def read_stdin():
        for line in sys.stdin:
            if line.strip() == "QUIT":
                break
        quit_flag.set()

    def on_signal(signum, frame):
        quit_flag.set()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    screen = touches = None
    try:
        screen = doom.Kindle(ip)
        _push(screen, _pad_image(keys, w, h), "FULL")
        touches = Touches(ip, on_tap, on_up)
        threading.Thread(target=read_stdin, daemon=True).start()
        say("READY %d" % keys)
        n = 0
        while not quit_flag.wait(1):
            n += 1
            if n % 20 == 0:
                os.utime(PAUSE_FILE)                     # keep the dashboard paused
        for lane in set(lanes.values()):
            say("U %d" % lane)                           # let go of anything still held
        say("QUIT")
    except Exception as e:
        print("ERROR %s" % e, flush=True)
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        if touches:
            touches.close()
        if screen:
            screen.close()
        for p in (STATE_FILE, PAUSE_FILE):
            try:
                os.remove(p)
            except OSError:
                pass
        time.sleep(0.6)                                  # (let the screen feed see that and stop)
        show.refresh_screen(force=True)
        show.push_now()
    return 0


def frames(keys):
    """The pad's picture: osu!'s columns from the Mac (10-digit length + PNG on stdin), newest
    first, drawn under the pad's top bar with the lane lines and keys over them."""
    import doom
    import show
    ip, _ = kindle.address()
    if not ip:
        return 1
    w, h = show.canvas_size()
    box = (0, HUD_H, w, h)
    latest = {"png": None}
    cond = threading.Condition()
    done = threading.Event()

    def reader():
        stream = sys.stdin.buffer
        while True:
            head = stream.read(10)
            if len(head) < 10:
                break
            data = stream.read(int(head))
            with cond:
                latest["png"] = data
                cond.notify()
        done.set()
        with cond:
            cond.notify()
    threading.Thread(target=reader, daemon=True).start()
    names = PAD_LABELS.get(keys, "").replace("Space", "␣").split()
    label_font = render.ui_font(40, "bold")
    screen = doom.Kindle(ip)
    x, y = doom.panel_xy(box, (w, h))
    shown, started, last_clean = 0, time.time(), time.time()
    try:
        while active():                                  # only while the pad is running
            with cond:
                while latest["png"] is None and not done.is_set():
                    cond.wait(0.5)
                    if not active():
                        break
                data, latest["png"] = latest["png"], None
            if data is None:
                if done.is_set():
                    break
                continue
            img = Image.open(io.BytesIO(data)).convert("L").resize((w, h - HUD_H))
            d = ImageDraw.Draw(img)
            for k in range(1, keys):
                lx = k * w / keys
                d.line((lx, 0, lx, h), fill=0, width=3)
            for k, name in enumerate(names):
                d.text(((k + 0.5) * w / keys, h - HUD_H - 40), name, font=label_font, fill=0, anchor="mm",
                       stroke_width=5, stroke_fill=255)
            img = img.convert("1")
            if render.ROTATE:
                img = img.rotate(render.ROTATE_DEGREES, expand=True)
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=1)
            clean = time.time() - last_clean > 20
            if not active():
                break
            if not screen.show(buf.getvalue(), x, y, "GC16" if clean else "A2"):
                break
            if clean:
                last_clean = time.time()
            shown += 1
    finally:
        screen.close()
        sys.stderr.write("screen feed: %d frames, %.1f a second\n" % (shown, shown / max(1, time.time() - started)))
    return 0


# ------------------------------ demo map ----------------------------

def demo():
    """A metronome map (120 BPM) and its click track: osu/demo.osu, osu/demo.wav."""
    import numpy as np
    import wave
    os.makedirs(OSU_DIR, exist_ok=True)
    bpm, beats, rate = 120, 72, 44100
    beat = 60000 / bpm
    start = 2000
    notes, objs = [], []
    for k in range(8, beats):
        t = int(start + k * beat)
        ang = k * 0.55
        x, y = 256 + 170 * math.cos(ang), 192 + 120 * math.sin(ang * 1.3)
        if k >= 40 and k % 4 == 3:                         # some quicker doubles later on
            objs.append("%d,%d,%d,%d,0,0:0:0:0:" % (x, y, t, 1))
            t2 = int(t + beat / 2)
            objs.append("%d,%d,%d,%d,0,0:0:0:0:" % (512 - x, 384 - y, t2, 1))
            notes += [t, t2]
        else:
            objs.append("%d,%d,%d,%d,0,0:0:0:0:" % (x, y, t, 5 if k % 4 == 0 else 1))
            notes.append(t)
    total = int((start + beats * beat + 1500) / 1000 * rate)
    audio = np.zeros(total, dtype=np.float32)

    def add(at_ms, sig):
        i = int(at_ms / 1000 * rate)
        audio[i:i + len(sig)] += sig[:max(0, total - i)]
    tt = np.arange(int(0.25 * rate)) / rate
    kick = np.sin(2 * np.pi * (55 + 90 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 12) * 0.8
    blip = (np.sin(2 * np.pi * 1760 * tt[:3000]) * np.exp(-tt[:3000] * 60) * 0.5)
    for k in range(beats):
        add(start + k * beat, kick * (1.0 if k % 4 == 0 else 0.6))
    for t in notes:
        add(t, blip)
    pcm = (np.clip(audio, -1, 1) * 30000).astype("<i2").tobytes()
    with wave.open(os.path.join(OSU_DIR, "demo.wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm)
    text = ("osu file format v14\n\n[General]\nAudioFilename: demo.wav\nAudioLeadIn: 0\n\n[Metadata]\n"
            "Title:Metronome\nArtist:kindle-dash\nVersion:Demo\n\n[Difficulty]\nCircleSize:3\n"
            "OverallDifficulty:4\nApproachRate:4\n\n[HitObjects]\n" + "\n".join(objs) + "\n")
    with open(os.path.join(OSU_DIR, "demo.osu"), "w") as f:
        f.write(text)
    return os.path.join(OSU_DIR, "demo.osu")


def main():
    args = sys.argv[1:]
    if args[:1] == ["demo"]:
        print(demo())
        return 0
    if args[:1] == ["frames"]:
        code = frames(int(args[1]) if len(args) > 1 else 4)
        sys.stderr.flush()
        os._exit(code)                                   # (its input reader may still be waiting)
    if args[:1] == ["pad"]:
        return pad(int(args[1]) if len(args) > 1 else 4)
    if args[:1] == ["play"] and len(args) > 1:
        offset = 0
        if "--offset" in args:
            offset = int(float(args[args.index("--offset") + 1]))
        keys = int(args[args.index("--mania") + 1]) if "--mania" in args else None
        return play(args[1], offset, use_kindle="--no-kindle" not in args, mania_keys=keys)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
