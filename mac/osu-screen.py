"""osu!mania on the Kindle's screen too (`kindle osu pad`): grabs osu!'s window, finds the mania
columns by watching where the notes move, and writes black-on-white frames of just the columns,
stretched so column k lines up with lane k on the Kindle pad.

Runs with the Kindle Sim's Python (it has numpy, Pillow and Quartz). Output on stdout:
10-digit byte count + PNG, repeatedly (osu.py frames on the Pi shows them).

  osu-screen.py KEYS [WIDTH HEIGHT] [--preview DIR]
"""

import io
import os
import sys
import time

import numpy as np
import Quartz
from PIL import Image, ImageDraw, ImageFont

FPS = 15
WORK_W = 480                                   # analysis width (the window is shrunk to this first)
INK = 0.55                                     # brightness (0-1, after stretching) that counts as a note


def osu_window():
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    best = None
    for w in wins:
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        b = w["kCGWindowBounds"]
        if owner.startswith("osu") and w.get("kCGWindowLayer", 0) == 0 and b["Width"] > 300:
            if not best or b["Width"] * b["Height"] > best[1]["Width"] * best[1]["Height"]:
                best = (w["kCGWindowNumber"], b)
    return best


def grab(win):
    wid, b = win
    rect = Quartz.CGRectMake(b["X"], b["Y"], b["Width"], b["Height"])
    img = Quartz.CGWindowListCreateImage(rect, Quartz.kCGWindowListOptionIncludingWindow, wid,
                                         Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution)
    if img is None:
        return None
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    row = Quartz.CGImageGetBytesPerRow(img)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
    a = np.frombuffer(data, dtype=np.uint8).reshape(h, row // 4, 4)[:, :w, :3].astype(np.float32)
    gray = a[:, :, 0] * 0.11 + a[:, :, 1] * 0.59 + a[:, :, 2] * 0.30    # (BGRA)
    small = np.asarray(Image.fromarray(gray).resize((WORK_W, max(1, int(h * WORK_W / w))), Image.BILINEAR))
    return small


class Stage:
    """Where the columns are: the band of x where things move (falling notes), smoothed."""

    def __init__(self):
        self.activity = np.zeros(WORK_W, np.float32)
        self.prev = None
        self.x0, self.x1 = WORK_W * 0.35, WORK_W * 0.65  # (a guess until notes start falling)

    def update(self, g):
        if self.prev is not None and self.prev.shape == g.shape:
            move = np.abs(g - self.prev).mean(axis=0)
            self.activity = self.activity * 0.97 + move * 0.03
            total = self.activity.sum()
            if total > WORK_W * 0.4:                          # enough motion to go on
                c = np.cumsum(self.activity) / total
                lo, hi = np.searchsorted(c, 0.04), np.searchsorted(c, 0.96)
                if hi - lo > WORK_W * 0.05:
                    self.x0 += (lo - self.x0) * 0.1
                    self.x1 += (hi - self.x1) * 0.1
        self.prev = g


def frame(g, stage, size):
    w, h = size
    x0, x1 = int(stage.x0), max(int(stage.x0) + 2, int(stage.x1))
    crop = g[:, x0:x1]
    lo, hi = np.percentile(crop, 20), np.percentile(crop, 99.5)
    norm = np.clip((crop - lo) / max(hi - lo, 1), 0, 1)
    img = Image.fromarray(((norm < INK) * 255).astype(np.uint8)).resize((w, h), Image.NEAREST)
    return img


def waiting(size, text):
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 56)
    except OSError:
        font = ImageFont.load_default()
    d.text((size[0] / 2, size[1] / 2), text, font=font, fill=0, anchor="mm")
    return img


def send(img):
    buf = io.BytesIO()
    img.convert("1").save(buf, "PNG", compress_level=1)
    data = buf.getvalue()
    sys.stdout.buffer.write(b"%010d" % len(data) + data)
    sys.stdout.buffer.flush()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keys = int(args[0]) if args else 4
    size = (int(args[1]), int(args[2])) if len(args) > 2 else (1448, 1002)
    preview = sys.argv[sys.argv.index("--preview") + 1] if "--preview" in sys.argv else None
    if not Quartz.CGPreflightScreenCaptureAccess():
        Quartz.CGRequestScreenCaptureAccess()
        sys.stderr.write("macOS needs Screen Recording permission for this Terminal "
                         "(System Settings > Privacy & Security > Screen Recording)\n")
    stage, win, found_at, n = Stage(), None, 0, 0
    while True:
        t = time.time()
        if t - found_at > 2:
            win, found_at = osu_window(), t
        g = grab(win) if win else None
        if g is None:
            send(waiting(size, "Open osu! on the Mac"))
            time.sleep(1)
            continue
        stage.update(g)
        img = frame(g, stage, size)
        send(img)
        n += 1
        if preview and n % FPS == 0:
            img.save(os.path.join(preview, "frame.png"))
        time.sleep(max(0, 1 / FPS - (time.time() - t)))


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
