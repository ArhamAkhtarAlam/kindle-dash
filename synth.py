#!/usr/bin/env python3
"""Synth: the Kindle is the instrument, the Mac makes the sound (`kindle synth` on the Mac).

Four pages (tabs along the top, with Play / Rec / tempo always there):
  Keys    two octaves of keys (multi-touch), waveform, octave, ring, echo
  Drums   8 big pads: kick, snare, clap, hats, toms, crash
  Beat    the drum pattern: 8 sounds x 16 or 32 steps, tap squares on and off
  Notes   the note pattern as a piano roll: tap a note to pick it, then move it up/down/
          sideways, make it longer/shorter or delete it; tap an empty square to add one. Undo.

Play loops the pattern (on the Mac's audio clock, so the timing is tight). ● Rec while playing
puts what you play on Keys and Drums into the pattern, snapped to the nearest step, so a wrong
note can be fixed on the Notes page. The pattern is kept in out/synth.json.

Printed for the Mac:
  N <finger> <midi> / O <finger>      a key down / up (heard right away)
  H <pad>                             a drum pad hit
  C wave|release|echo <value>         a setting
  T play 0|1 · T bpm N · T len 16|32  the transport
  G <json>  P <json>                  the drum grid / the notes [{step, len, midi}] (whole thing)
"""

import io
import json
import os
import signal
import sys
import threading
import time

from PIL import Image, ImageDraw

import kindle
import render

STATE_FILE = os.path.join(render.OUT_DIR, ".synth")           # pid while it runs (touch.py looks)
PAUSE_FILE = os.path.join(render.OUT_DIR, ".pause")
SAVE_FILE = os.path.join(render.OUT_DIR, "synth.json")
BAR_H, ROW_Y0, ROW_Y1, BODY_Y = 70, 80, 168, 182
PAGES = [("keys", "Keys"), ("drums", "Drums"), ("beat", "Beat"), ("notes", "Notes")]
DRUMS = ["Kick", "Snare", "Clap", "Hat", "Open hat", "Low tom", "High tom", "Crash"]
WAVES = [("sine", "Sine"), ("square", "Square"), ("saw", "Saw"), ("tri", "Triangle")]
WHITE = [0, 2, 4, 5, 7, 9, 11]
BLACK = {0: 1, 1: 3, 3: 6, 4: 8, 5: 10}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
ROLL_ROWS = 24                                                 # two octaves visible on Notes
MAX_STEPS = 32


def active():
    try:
        with open(STATE_FILE) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def _load():
    try:
        with open(SAVE_FILE) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("settings", {"wave": "saw", "octave": 4, "release": "short", "echo": "off"})
    s.setdefault("bpm", 110)
    s.setdefault("length", 16)
    s.setdefault("drums", [[0] * MAX_STEPS for _ in DRUMS])
    s.setdefault("notes", [])
    s.setdefault("roll_base", 48)                              # lowest pitch shown on Notes (C3)
    s.setdefault("page", "keys")
    return s


class App:
    def __init__(self, w, h, say):
        self.w, self.h, self.say = w, h, say
        self.s = _load()
        self.playing = False
        self.play_start = 0.0
        self.recording = False
        self.sel = None                                        # index into notes (Notes page)
        self.undo = []
        self.fingers = {}                                      # slot -> midi (Keys)
        self.pads_down = {}                                    # slot -> pad (Drums)
        self.pending = {}                                      # slot -> (midi, t) while recording
        self.dirty = {"full"}                                  # (start with one clean full refresh)
        self.lock = threading.RLock()

    # ------------------------------------------------------------ state
    def save(self):
        with open(SAVE_FILE + ".tmp", "w") as f:
            json.dump(self.s, f)
        os.replace(SAVE_FILE + ".tmp", SAVE_FILE)

    def send_pattern(self):
        self.say("G " + json.dumps(self.s["drums"], separators=(",", ":")))
        self.say("P " + json.dumps(self.s["notes"], separators=(",", ":")))

    def hello(self):
        for k, v in self.s["settings"].items():
            if k != "octave":
                self.say("C %s %s" % (k, v))
        self.say("T bpm %d" % self.s["bpm"])
        self.say("T len %d" % self.s["length"])
        self.send_pattern()

    def remember(self):
        self.undo = (self.undo + [json.dumps({"notes": self.s["notes"], "drums": self.s["drums"]})])[-50:]

    def step_now(self, t):
        """The pattern step (float) at Pi time t, while playing."""
        step_s = 60.0 / self.s["bpm"] / 4
        return ((t - self.play_start) / step_s) % self.s["length"]

    # ------------------------------------------------------------ layout
    def bar_buttons(self):
        b = []
        x = 16
        for key, label in PAGES:
            b.append(("page", key, label, (x, 8, x + 128, 62)))
            x += 136
        x += 24
        b.append(("play", "", "", (x, 8, x + 130, 62)))
        b.append(("rec", "", "", (x + 138, 8, x + 258, 62)))
        x += 286
        b.append(("bpm", "-5", "−", (x, 8, x + 56, 62)))
        self.bpm_box = (x + 60, 8, x + 196, 62)
        b.append(("bpm", "+5", "+", (x + 200, 8, x + 256, 62)))
        b.append(("exit", "", "Exit", (self.w - 136, 8, self.w - 16, 62)))
        return b

    def row_buttons(self):
        page, b = self.s["page"], []
        if page == "keys":
            x = 16
            for key, label in WAVES:
                b.append(("wave", key, label, (x, ROW_Y0, x + 150, ROW_Y1)))
                x += 160
            x += 20
            b.append(("octave", "-1", "Oct −", (x, ROW_Y0, x + 120, ROW_Y1)))
            self.oct_box = (x + 128, ROW_Y0, x + 228, ROW_Y1)
            b.append(("octave", "+1", "Oct +", (x + 236, ROW_Y0, x + 356, ROW_Y1)))
            x += 376
            b.append(("release", "", "", (x, ROW_Y0, x + 170, ROW_Y1)))
            b.append(("echo", "", "", (x + 180, ROW_Y0, x + 330, ROW_Y1)))
        elif page in ("beat", "notes"):
            items = [("length", "", "%d steps" % self.s["length"])]
            if page == "notes":
                items += [("edit", "up", "▲"), ("edit", "down", "▼"), ("edit", "left", "◀"), ("edit", "right", "▶"),
                          ("edit", "longer", "Longer"), ("edit", "shorter", "Shorter"), ("edit", "delete", "Delete"),
                          ("view", "+12", "View ↑"), ("view", "-12", "View ↓")]
            items += [("undo", "", "Undo"), ("clear", "", "Clear")]
            gap = 8
            bw = (self.w - 32 - gap * (len(items) - 1)) / len(items)
            for i, (name, value, label) in enumerate(items):
                x = 16 + i * (bw + gap)
                b.append((name, value, label, (x, ROW_Y0, x + bw, ROW_Y1)))
        return b

    def keys(self):
        base = 12 * (self.s["settings"]["octave"] + 1)
        nw = 15
        kw = (self.w - 32) / nw
        y0, y1 = BODY_Y + 10, self.h - 14
        bh = y0 + (y1 - y0) * 0.6
        whites, blacks = [], []
        for i in range(nw):
            octave, k = divmod(i, 7)
            x = 16 + i * kw
            whites.append((base + 12 * octave + WHITE[k], False, (x, y0, x + kw, y1)))
            if k in BLACK and i < nw - 1:
                bx = x + kw * 0.68
                blacks.append((base + 12 * octave + BLACK[k], True, (bx, y0, bx + kw * 0.64, bh)))
        return whites + blacks

    def pads(self):
        out, cols, rows = [], 4, 2
        pw, ph = (self.w - 32 - 3 * 16) / cols, (self.h - BODY_Y - 24 - 16) / rows
        for i, name in enumerate(DRUMS):
            r, c = divmod(i, cols)
            x, y = 16 + c * (pw + 16), BODY_Y + 10 + r * (ph + 16)
            out.append((i, name, (x, y, x + pw, y + ph)))
        return out

    def grid_geom(self, rows):
        label_w = 150 if self.s["page"] == "beat" else 80
        x0, x1 = 16 + label_w, self.w - 16
        y0, y1 = BODY_Y + 30, self.h - 12
        return x0, x1, y0, y1, (x1 - x0) / self.s["length"], (y1 - y0) / rows

    # ------------------------------------------------------------ drawing
    def _btn(self, d, box, label, on=False, size=30):
        d.rounded_rectangle(box, radius=14, outline=0, width=3, fill=0 if on else 255)
        f = render.ui_font(size, "semibold")
        while d.textlength(label, font=f) > box[2] - box[0] - 10 and size > 16:
            size -= 2
            f = render.ui_font(size, "semibold")
        d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=f, fill=255 if on else 0, anchor="mm")

    def draw_bar(self):
        img = Image.new("L", (self.w, BAR_H), 255)
        d = ImageDraw.Draw(img)
        for name, value, label, box in self.bar_buttons():
            if name == "page":
                self._btn(d, box, label, on=self.s["page"] == value)
            elif name == "play":
                self._btn(d, box, "■ Stop" if self.playing else "▶ Play", on=self.playing)
            elif name == "rec":
                self._btn(d, box, "● Rec", on=self.recording)
            else:
                self._btn(d, box, label)
        b = self.bpm_box
        d.text(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), "%d BPM" % self.s["bpm"], font=render.ui_font(30, "bold"),
               fill=0, anchor="mm")
        d.line((0, BAR_H - 2, self.w, BAR_H - 2), fill=0, width=2)
        return img

    def draw_row(self):
        img = Image.new("L", (self.w, ROW_Y1 - ROW_Y0 + 8), 255)
        d = ImageDraw.Draw(img)
        s = self.s["settings"]
        sel_ok = self.sel is not None and self.sel < len(self.s["notes"])
        for name, value, label, box in self.row_buttons():
            box = (box[0], box[1] - ROW_Y0, box[2], box[3] - ROW_Y0)
            on = (name == "wave" and s["wave"] == value) or (name == "echo" and s["echo"] == "on") \
                or (name == "release" and s["release"] == "long")
            if name == "release":
                label = "Ring: " + s["release"]
            elif name == "echo":
                label = "Echo " + s["echo"]
            if name == "edit" and not sel_ok:
                d.rounded_rectangle(box, radius=14, outline=170, width=3)       # (nothing picked yet)
                d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=render.ui_font(30, "semibold"),
                       fill=170, anchor="mm")
                continue
            self._btn(d, box, label, on=on)
        if self.s["page"] == "keys":
            ob = self.oct_box
            d.text(((ob[0] + ob[2]) / 2, (ob[3] - ob[1]) / 2 - 12), "C%d" % s["octave"], font=render.ui_font(40, "bold"),
                   fill=0, anchor="mm")
            d.text(((ob[0] + ob[2]) / 2, (ob[3] - ob[1]) / 2 + 26), "octave", font=render.ui_font(22), fill=0, anchor="mm")
        if self.s["page"] == "drums":
            d.text((16, 44), "Tap the pads. With ▶ Play and ● Rec on, hits go into the Beat page.",
                   font=render.ui_font(30), fill=0, anchor="lm")
        return img

    def draw_body(self):
        img = Image.new("L", (self.w, self.h - BODY_Y), 255)
        d = ImageDraw.Draw(img)
        oy = BODY_Y
        page = self.s["page"]
        if page == "keys":
            held = set(self.fingers.values())
            lf = render.ui_font(30, "bold")
            for midi, black, (x0, y0, x1, y1) in self.keys():
                box = (x0, y0 - oy, x1, y1 - oy)
                if black:
                    d.rectangle(box, fill=110 if midi in held else 0)
                else:
                    d.rectangle(box, fill=170 if midi in held else 255, outline=0, width=3)
                    if midi % 12 == 0:
                        d.text(((x0 + x1) / 2, y1 - oy - 34), "C%d" % (midi // 12 - 1), font=lf, fill=0, anchor="mm")
        elif page == "drums":
            down = set(self.pads_down.values())
            for i, name, (x0, y0, x1, y1) in self.pads():
                on = i in down
                d.rounded_rectangle((x0, y0 - oy, x1, y1 - oy), radius=30, outline=0, width=6, fill=0 if on else 245)
                d.text(((x0 + x1) / 2, (y0 + y1) / 2 - oy), name, font=render.ui_font(54, "bold"),
                       fill=255 if on else 0, anchor="mm")
        elif page == "beat":
            x0, x1, y0, y1, cw, rh = self.grid_geom(len(DRUMS))
            self._grid_lines(d, x0, x1, y0 - oy, y1 - oy, cw)
            lf = render.ui_font(28, "semibold")
            for r, name in enumerate(DRUMS):
                ry = y0 - oy + r * rh
                d.text((20, ry + rh / 2), name, font=lf, fill=0, anchor="lm")
                d.line((x0, ry, x1, ry), fill=0, width=1)
                for c in range(self.s["length"]):
                    if self.s["drums"][r][c]:
                        d.rectangle((x0 + c * cw + 5, ry + 5, x0 + (c + 1) * cw - 5, ry + rh - 5), fill=0)
            d.rectangle((x0, y0 - oy, x1, y1 - oy), outline=0, width=3)
        elif page == "notes":
            base = self.s["roll_base"]
            x0, x1, y0, y1, cw, rh = self.grid_geom(ROLL_ROWS)
            lf = render.ui_font(20, "semibold")
            for r in range(ROLL_ROWS):
                midi = base + ROLL_ROWS - 1 - r
                ry = y0 - oy + r * rh
                if NOTE_NAMES[midi % 12].endswith("#"):
                    d.rectangle((x0, ry, x1, ry + rh), fill=228)           # black-key rows shaded
                if midi % 12 in (0, 4, 7, 9) or midi % 12 == 0:
                    d.text((20, ry + rh / 2), "%s%d" % (NOTE_NAMES[midi % 12], midi // 12 - 1), font=lf, fill=0, anchor="lm")
                d.line((x0, ry, x1, ry), fill=0 if midi % 12 == 11 else 200, width=2 if midi % 12 == 11 else 1)
            self._grid_lines(d, x0, x1, y0 - oy, y1 - oy, cw)
            above = below = 0
            for i, n in enumerate(self.s["notes"]):
                if n["step"] >= self.s["length"]:
                    continue
                r = base + ROLL_ROWS - 1 - n["midi"]
                if r < 0:
                    above += 1
                    continue
                if r >= ROLL_ROWS:
                    below += 1
                    continue
                ry = y0 - oy + r * rh
                nx0 = x0 + n["step"] * cw + 3
                nx1 = x0 + min(self.s["length"], n["step"] + n["len"]) * cw - 3
                if i == self.sel:
                    d.rectangle((nx0 - 3, ry - 3, nx1 + 3, ry + rh + 3), fill=255, outline=0, width=5)
                    d.rectangle((nx0 + 6, ry + 6, nx1 - 6, ry + rh - 6), fill=0)
                else:
                    d.rectangle((nx0, ry + 2, nx1, ry + rh - 2), fill=0)
            d.rectangle((x0, y0 - oy, x1, y1 - oy), outline=0, width=3)
            hint = []
            if above:
                hint.append("%d higher (View ↑)" % above)
            if below:
                hint.append("%d lower (View ↓)" % below)
            if hint:
                d.text((x1, 6), "  ·  ".join(hint), font=render.ui_font(22, "semibold"), fill=0, anchor="ra")
        return img

    def _grid_lines(self, d, x0, x1, y0, y1, cw):
        for c in range(self.s["length"] + 1):
            x = x0 + c * cw
            d.line((x, y0, x, y1), fill=0, width=3 if c % 4 == 0 else 1)
        lf = render.ui_font(18)
        for c in range(0, self.s["length"], 4):
            d.text((x0 + c * cw + 4, y0 - 24), str(c // 4 + 1), font=lf, fill=0)

    def playhead(self):
        """The strip above the grid with a marker on the current beat."""
        x0, x1, y0, _, cw, _ = self.grid_geom(8)
        img = Image.new("L", (self.w, 26), 255)
        d = ImageDraw.Draw(img)
        lf = render.ui_font(18)
        for c in range(0, self.s["length"], 4):
            d.text((x0 + c * cw + 4, 2), str(c // 4 + 1), font=lf, fill=0)
        if self.playing:
            beat = int(self.step_now(time.monotonic()) // 4)
            d.rectangle((x0 + beat * 4 * cw + 26, 6, x0 + (beat + 1) * 4 * cw - 4, 20), fill=0)
        return img

    # ------------------------------------------------------------ input
    def down(self, cx, cy, t, slot):
        with self.lock:
            if cy < BAR_H:
                return self._bar(cx, cy)
            if cy < BODY_Y - 6:
                return self._row(cx, cy)
            page = self.s["page"]
            if page == "keys":
                midi = next((m for m, _, (x0, y0, x1, y1) in reversed(self.keys()) if x0 <= cx <= x1 and y0 <= cy <= y1), None)
                if midi is None or self.fingers.get(slot) == midi:
                    return
                self.fingers[slot] = midi
                self.say("N %d %d" % (slot, midi))
                if self.recording and self.playing:
                    self.pending[slot] = (midi, t)
                self.dirty.add("body")
            elif page == "drums":
                pad = next((i for i, _, (x0, y0, x1, y1) in self.pads() if x0 <= cx <= x1 and y0 <= cy <= y1), None)
                if pad is None:
                    return
                self.pads_down[slot] = pad
                self.say("H %d" % pad)
                if self.recording and self.playing:
                    step = int(round(self.step_now(t))) % self.s["length"]
                    if not self.s["drums"][pad][step]:
                        self.remember()
                        self.s["drums"][pad][step] = 1
                        self.say("G " + json.dumps(self.s["drums"], separators=(",", ":")))
                        self.save()
                self.dirty.add("body")
            elif page == "beat":
                x0, x1, y0, y1, cw, rh = self.grid_geom(len(DRUMS))
                if x0 <= cx < x1 and y0 <= cy < y1:
                    r, c = int((cy - y0) // rh), int((cx - x0) // cw)
                    self.remember()
                    self.s["drums"][r][c] ^= 1
                    self.say("G " + json.dumps(self.s["drums"], separators=(",", ":")))
                    self.save()
                    self.dirty.add("body")
            elif page == "notes":
                x0, x1, y0, y1, cw, rh = self.grid_geom(ROLL_ROWS)
                if x0 <= cx < x1 and y0 <= cy < y1:
                    r, c = int((cy - y0) // rh), int((cx - x0) // cw)
                    midi = self.s["roll_base"] + ROLL_ROWS - 1 - r
                    hit = next((i for i, n in enumerate(self.s["notes"])
                                if n["midi"] == midi and n["step"] <= c < n["step"] + n["len"]), None)
                    if hit is not None:
                        self.sel = None if hit == self.sel else hit          # tap again to let go
                    else:
                        self.remember()
                        self.s["notes"].append({"step": c, "len": 1, "midi": midi})
                        self.sel = len(self.s["notes"]) - 1
                        self.say("P " + json.dumps(self.s["notes"], separators=(",", ":")))
                        self.save()
                        self.say("N 99 %d" % midi)                          # hear it
                        threading.Timer(0.25, lambda: self.say("O 99")).start()
                    self.dirty.update({"body", "row"})

    def up(self, slot, t):
        with self.lock:
            if self.fingers.pop(slot, None) is not None:
                self.say("O %d" % slot)
                self.dirty.add("body")
            if self.pads_down.pop(slot, None) is not None:
                self.dirty.add("body")
            rec = self.pending.pop(slot, None)
            if rec and self.playing:
                midi, t0 = rec
                step_s = 60.0 / self.s["bpm"] / 4
                step = int(round(self.step_now(t0))) % self.s["length"]
                length = max(1, int(round((t - t0) / step_s)))
                self.remember()
                self.s["notes"].append({"step": step, "len": min(length, self.s["length"]), "midi": midi})
                self.say("P " + json.dumps(self.s["notes"], separators=(",", ":")))
                self.save()

    def _bar(self, cx, cy):
        for name, value, _, (x0, y0, x1, y1) in self.bar_buttons():
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                continue
            if name == "exit":
                return "exit"
            if name == "page" and value != self.s["page"]:
                self.s["page"] = value
                self.sel = None
                self.dirty.update({"bar", "row", "body", "full"})
                self.save()
            elif name == "play":
                self.playing = not self.playing
                if self.playing:
                    self.play_start = time.monotonic()
                else:
                    self.recording = False
                self.say("T play %d" % self.playing)
                self.dirty.add("bar")
            elif name == "rec":
                self.recording = not self.recording
                if self.recording and not self.playing:
                    self.playing, self.play_start = True, time.monotonic()
                    self.say("T play 1")
                self.dirty.add("bar")
            elif name == "bpm":
                self.s["bpm"] = max(50, min(200, self.s["bpm"] + int(value)))
                if self.playing:                                   # keep the playhead in step
                    self.play_start = time.monotonic()
                self.say("T bpm %d" % self.s["bpm"])
                self.save()
                self.dirty.add("bar")
            return name

    def _row(self, cx, cy):
        s = self.s["settings"]
        for name, value, _, (x0, y0, x1, y1) in self.row_buttons():
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                continue
            if name == "wave":
                s["wave"] = value
                self.say("C wave %s" % value)
            elif name == "octave":
                s["octave"] = max(1, min(7, s["octave"] + int(value)))
                self.dirty.add("body")
            elif name == "release":
                s["release"] = "long" if s["release"] == "short" else "short"
                self.say("C release %s" % s["release"])
            elif name == "echo":
                s["echo"] = "on" if s["echo"] == "off" else "off"
                self.say("C echo %s" % s["echo"])
            elif name == "length":
                self.s["length"] = 32 if self.s["length"] == 16 else 16
                self.say("T len %d" % self.s["length"])
                self.dirty.add("body")
            elif name == "undo":
                if not self.undo:
                    return
                old = json.loads(self.undo.pop())
                self.s["notes"], self.s["drums"] = old["notes"], old["drums"]
                self.sel = None
                self.send_pattern()
                self.dirty.add("body")
            elif name == "clear":
                self.remember()
                if self.s["page"] == "beat":
                    self.s["drums"] = [[0] * MAX_STEPS for _ in DRUMS]
                else:
                    self.s["notes"], self.sel = [], None
                self.send_pattern()
                self.dirty.add("body")
            elif name == "view":
                self.s["roll_base"] = max(12, min(96, self.s["roll_base"] + int(value)))
                self.dirty.add("body")
            elif name == "edit":
                self._edit(value)
            self.save()
            self.dirty.add("row")
            return name

    def _edit(self, how):
        if self.sel is None or self.sel >= len(self.s["notes"]):
            return
        n = self.s["notes"][self.sel]
        self.remember()
        if how == "delete":
            self.s["notes"].pop(self.sel)
            self.sel = None
        elif how == "up":
            n["midi"] = min(108, n["midi"] + 1)
        elif how == "down":
            n["midi"] = max(12, n["midi"] - 1)
        elif how == "left":
            n["step"] = max(0, n["step"] - 1)
        elif how == "right":
            n["step"] = min(self.s["length"] - 1, n["step"] + 1)
        elif how == "longer":
            n["len"] = min(self.s["length"] - n["step"], n["len"] + 1)
        elif how == "shorter":
            n["len"] = max(1, n["len"] - 1)
        self.say("P " + json.dumps(self.s["notes"], separators=(",", ":")))
        if how in ("up", "down"):                                   # hear the new pitch
            self.say("N 99 %d" % n["midi"])
            threading.Timer(0.25, lambda: self.say("O 99")).start()
        self.dirty.add("body")


def run():
    import doom
    import osu
    import show
    ip, _ = kindle.address()
    if not ip:
        print("ERROR Kindle not reachable", flush=True)
        return 1
    for p in (STATE_FILE, PAUSE_FILE):
        with open(p, "w") as f:
            f.write(str(os.getpid()))
    w, h = show.canvas_size()
    out_lock = threading.Lock()
    quit_flag = threading.Event()
    wake = threading.Event()

    def say(line):
        with out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    app = App(w, h, say)

    def on_down(cx, cy, t, slot=0):
        if app.down(cx, cy, t, slot) == "exit":
            quit_flag.set()
        wake.set()

    def on_up(slot, t):
        app.up(slot, t)
        wake.set()

    def read_stdin():
        for line in sys.stdin:
            if line.strip() == "QUIT":
                break
        quit_flag.set()
        wake.set()

    def on_signal(signum, frame):
        quit_flag.set()
        wake.set()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)

    screen = touches = None
    try:
        screen = doom.Kindle(ip)
        app.hello()
        touches = osu.Touches(ip, on_down, on_up)
        threading.Thread(target=read_stdin, daemon=True).start()
        say("READY")
        last_beat, n = None, 0
        while not quit_flag.is_set():
            wake.wait(0.1)
            wake.clear()
            with app.lock:
                dirty, app.dirty = app.dirty, set()
                page, playing = app.s["page"], app.playing
            if "full" in dirty:
                img = Image.new("L", (w, h), 255)
                img.paste(app.draw_bar(), (0, 0))
                img.paste(app.draw_row(), (0, ROW_Y0))
                img.paste(app.draw_body(), (0, BODY_Y))
                _push(screen, img, (0, 0, w, h), "GC16")
                continue
            if "bar" in dirty:
                _push(screen, app.draw_bar(), (0, 0, w, BAR_H), "DU")
            if "row" in dirty:
                _push(screen, app.draw_row(), (0, ROW_Y0, w, ROW_Y1 + 8), "DU")
            if "body" in dirty:
                _push(screen, app.draw_body(), (0, BODY_Y, w, h), "A2" if page in ("keys", "drums") else "DU")
            if page in ("beat", "notes"):                           # the beat marker while playing
                beat = int(app.step_now(time.monotonic()) // 4) if playing else None
                if beat != last_beat:
                    last_beat = beat
                    _push(screen, app.playhead(), (0, BODY_Y + 4, w, BODY_Y + 30), "DU")
            n += 1
            if n % 200 == 0:
                os.utime(PAUSE_FILE)
        for slot in list(app.fingers):
            say("O %d" % slot)
        say("T play 0")
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
        show.refresh_screen(force=True)
        show.push_now()
    return 0


def _push(screen, img, box, wave):
    import doom
    import show
    x, y = doom.panel_xy(box, show.canvas_size())
    if render.ROTATE:
        img = img.rotate(render.ROTATE_DEGREES, expand=True)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return screen.show(buf.getvalue(), x, y, wave)


if __name__ == "__main__":
    sys.exit(run())
