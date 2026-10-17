#!/usr/bin/env python3
"""DOOM on the Kindle. The game runs on the Pi (doomgeneric, built by doom/build.sh, with the
free Freedoom game data or your own WAD); frames are brightened, dithered and sent to the Kindle
over one ssh connection, and touch buttons around the picture are the controls.

Only started with `kindle doom` (it isn't in the menu or the games list).

  doom.py start [fast|gray] [WAD]   run it in the background (builds it the first time)
  doom.py stop                      quit and put the dashboard back
  doom.py wads                      list the game data files in doom/wads/

fast = black/white, FBInk's quick A2 refresh (default); gray = 16 greys, slower but clearer.
WAD = a file name in doom/wads/ without .wad (default: doom1 or doom if you added one,
else freedoom1). The touch listener (touch.py) feeds the buttons to the game while it runs;
the dashboard is paused like for mirroring, and comes back when you quit.
"""

import hashlib
import io
import os
import signal
import struct
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageOps

import kindle
import render

DOOM_DIR = os.path.join(render.BASE_DIR, "doom")
BINARY = os.path.join(DOOM_DIR, "doom-kindle")
WAD_DIR = os.path.join(DOOM_DIR, "wads")
STATE_FILE = os.path.join(render.OUT_DIR, ".doom")       # pid while running (touch.py looks)
PAUSE_FILE = os.path.join(render.OUT_DIR, ".pause")      # dashboard: hands off
LOG_FILE = os.path.join(render.OUT_DIR, "doom.log")
FB_FILE = "/dev/shm/kindle-doom.fb"
KEYS_FILE = "/dev/shm/kindle-doom.keys"
FBINK = "/mnt/us/libkh/bin/fbink"
REMOTE = "/tmp/doom.png"                                 # Kindle's RAM disk
GAMMA = 0.75                                             # DOOM is dark; e-ink needs it lighter
CLEAN_EVERY_SEC = 20                                     # a clean refresh of the picture (ghosting)
FRAME_W, FRAME_H = 320, 200

# Doom key codes (doomgeneric's doomkeys.h)
K_RIGHT, K_LEFT, K_UP, K_DOWN = 0xAE, 0xAC, 0xAD, 0xAF
K_STRAFE_L, K_STRAFE_R, K_USE, K_FIRE = 0xA0, 0xA1, 0xA2, 0xA3
K_ESC, K_ENTER, K_TAB = 27, 13, 9

# ------------------------------ layout ------------------------------
# Canvas (landscape, 1448x1072): the picture in the middle at 4:3 (DOOM's own shape),
# weapons down the left, menu keys down the right, movement and fire along the bottom.
GAME_BOX = (214, 8, 1234, 773)


def _column(x0, x1, y0, y1, items, gap=8):
    n = len(items)
    h = (y1 - y0 - gap * (n - 1)) / n
    return [(label, key, (x0, round(y0 + i * (h + gap)), x1, round(y0 + i * (h + gap) + h)))
            for i, (label, key) in enumerate(items)]


def layout():
    """[(label, key, box)]; key is a Doom key code, or "quit"."""
    zones = _column(8, 206, 8, 773, [("Fist  1", ord("1")), ("Gun  2", ord("2")), ("Shotgun  3", ord("3")),
                                     ("Chaingun  4", ord("4")), ("Rockets  5", ord("5")),
                                     ("Plasma  6", ord("6")), ("BFG  7", ord("7"))])
    zones += _column(1242, 1440, 8, 773, [("Menu", K_ESC), ("Enter", K_ENTER), ("Yes", ord("y")),
                                          ("Map", K_TAB), ("Quit DOOM", "quit")])
    y0, y1 = 785, 1064
    ym = (y0 + y1) // 2
    cw = 200
    pad = [("‹ Strafe", K_STRAFE_L), ("▲", K_UP), ("Strafe ›", K_STRAFE_R),
           ("◀ Turn", K_LEFT), ("▼", K_DOWN), ("Turn ▶", K_RIGHT)]
    for i, (label, key) in enumerate(pad):
        r, c = divmod(i, 3)
        x = 8 + c * (cw + 8)
        zones.append((label, key, (x, y0 if r == 0 else ym + 4, x + cw, ym - 4 if r == 0 else y1)))
    zones.append(("Use / open", K_USE, (636, y0, 836, y1)))
    zones.append(("FIRE", K_FIRE, (844, y0, 1440, y1)))
    return zones


def draw_controls(w, h):
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.rectangle(GAME_BOX, fill=0)
    for label, key, box in layout():
        big = key in (K_FIRE, K_UP, K_DOWN, K_LEFT, K_RIGHT)
        filled = key == K_FIRE
        d.rounded_rectangle(box, radius=18, outline=0, width=4, fill=0 if filled else 255)
        size = 60 if key == K_FIRE else 40 if big else 30
        font = render.ui_font(size, "bold" if big or key == "quit" else "semibold")
        while d.textlength(label, font=font) > box[2] - box[0] - 16 and size > 16:
            size -= 2
            font = render.ui_font(size, "semibold")
        d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=font,
               fill=255 if filled else 0, anchor="mm")
    return img


# ------------------------------ touch -------------------------------

def active():
    """True while DOOM is running (touch.py sends the Kindle's touches here)."""
    try:
        with open(STATE_FILE) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


class Touch:
    """Raw Kindle touch events -> Doom key presses. A key is held while a finger is on its
    button; with two fingers (if the panel reports them) you can e.g. walk and fire."""
    EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
    ABS_X, ABS_Y, ABS_MT_SLOT = 0x00, 0x01, 0x2F
    ABS_MT_X, ABS_MT_Y, ABS_MT_TRACKING_ID, BTN_TOUCH = 0x35, 0x36, 0x39, 0x14A

    def __init__(self, to_canvas):
        self.to_canvas = to_canvas          # raw (x, y) -> canvas (x, y)
        self.zones = layout()
        self.slot = 0
        self.fingers = {}                   # slot -> [x, y, down]
        self.held = set()
        self.fd = None

    def _finger(self):
        return self.fingers.setdefault(self.slot, [None, None, False])

    def feed(self, etype, code, value):
        if etype == self.EV_ABS:
            if code == self.ABS_MT_SLOT:
                self.slot = value
            elif code in (self.ABS_MT_X, self.ABS_X):
                self._finger()[0] = value
            elif code in (self.ABS_MT_Y, self.ABS_Y):
                self._finger()[1] = value
            elif code == self.ABS_MT_TRACKING_ID:
                self._finger()[2] = value >= 0
        elif etype == self.EV_KEY and code == self.BTN_TOUCH:
            if not value:                   # last finger up (single-touch drivers)
                for f in self.fingers.values():
                    f[2] = False
            else:
                self._finger()[2] = True
        elif etype == self.EV_SYN:
            self._update()

    def _zone(self, x, y):
        cx, cy = self.to_canvas(x, y)
        for _, key, (x0, y0, x1, y1) in self.zones:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return key
        return None

    def _update(self):
        now = {self._zone(f[0], f[1]) for f in self.fingers.values()
               if f[2] and f[0] is not None and f[1] is not None} - {None}
        if "quit" in now and "quit" not in self.held:
            stop()
        for key in now - self.held:
            self._send(1, key)
        for key in self.held - now:
            self._send(0, key)
        self.held = now

    def _send(self, pressed, key):
        if key == "quit":
            return
        try:
            if self.fd is None:
                self.fd = os.open(KEYS_FILE, os.O_WRONLY | os.O_NONBLOCK)
            os.write(self.fd, bytes([pressed, key]))
        except OSError:                      # game gone or restarting: try again next time
            if self.fd is not None:
                os.close(self.fd)
            self.fd = None

    def release_all(self):
        for key in list(self.held):
            self._send(0, key)
        self.held = set()


# ------------------------------ frames ------------------------------

def _lut():
    return [round(255 * (i / 255) ** GAMMA) for i in range(256)]


LUT = _lut()


def frame_png(raw, mode, canvas_w):
    """8-bit grey frame -> PNG of just the game box, rotated for the panel."""
    import dither
    import show
    src = Image.frombytes("L", (FRAME_W, FRAME_H), raw)
    src = ImageOps.autocontrast(src, cutoff=1).point(LUT)
    x0, y0, x1, y1 = GAME_BOX
    img = src.resize((x1 - x0, y1 - y0), Image.BILINEAR)
    img = dither.bw(img) if mode == "fast" else show.to_16_grays(img)
    if render.ROTATE:
        img = img.rotate(render.ROTATE_DEGREES, expand=True)
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def panel_xy(box, canvas):
    """Where a canvas box lands on the portrait panel (top-left)."""
    w, h = canvas
    x0, y0, x1, y1 = box
    if not render.ROTATE:
        return x0, y0
    if render.ROTATE_DEGREES == 90:
        return y0, w - x1
    return h - y1, x0


class Kindle:
    """One ssh session running a small loop on the Kindle: for each frame it reads a header
    line and the PNG, shows it with FBInk and answers "ok" (so we never queue up frames)."""

    LOOP = ("export PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH; "
            "while read n x y wave; do head -c $n > %s; "
            "case $wave in FULL) f='-c -f';; A2) f='-W A2';; DU) f='-W DU';; GC16) f='-W GC16';; *) f='';; esac; "
            "%s -q $f -g file=%s,x=$x,y=$y >/dev/null 2>&1; echo ok; done" % (REMOTE, FBINK, REMOTE))

    def __init__(self, ip):
        self.proc = subprocess.Popen(["ssh"] + render.ssh_options() + ["root@" + ip, self.LOOP],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)  # (nothing may hold our output open)

    def show(self, png, x, y, wave):
        self.proc.stdin.write(b"%d %d %d %s\n" % (len(png), x, y, wave.encode()))
        self.proc.stdin.write(png)
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip() == b"ok"

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ------------------------------ running -----------------------------

def wads():
    try:
        return sorted(n[:-4] for n in os.listdir(WAD_DIR) if n.lower().endswith(".wad"))
    except OSError:
        return []


def pick_wad(name=None):
    have = wads()
    if name:
        if name not in have:
            raise SystemExit("no %s.wad in doom/wads (have: %s)" % (name, ", ".join(have) or "none"))
        return name
    for pref in ("doom1", "doom", "doomu", "doom2", "freedoom1", "freedoom2"):
        if pref in have:
            return pref
    return have[0] if have else None


def run(mode, wad):
    import show
    ip, via = kindle.address()
    if not ip:
        print("Kindle not reachable (USB or Wi-Fi)", flush=True)
        return 1
    for p in (FB_FILE, KEYS_FILE):
        if os.path.exists(p):
            os.remove(p)
    for p in (PAUSE_FILE, STATE_FILE):
        with open(p, "w") as f:
            f.write(str(os.getpid()))

    def on_signal(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)

    game = subprocess.Popen([BINARY, "-iwad", os.path.join(WAD_DIR, wad + ".wad")], cwd=DOOM_DIR,
                            env=dict(os.environ, KINDLE_DOOM_FB=FB_FILE, KINDLE_DOOM_KEYS=KEYS_FILE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    screen = Kindle(ip)
    canvas = show.canvas_size()
    gx, gy = panel_xy(GAME_BOX, canvas)
    shown, started, last_clean, last_count, last_hash = 0, time.time(), time.time(), None, None
    print("DOOM (%s) on the Kindle over %s, %s mode" % (wad, "USB" if via == "usb" else "Wi-Fi", mode), flush=True)
    try:
        controls = draw_controls(*canvas)
        if render.ROTATE:
            controls = controls.rotate(render.ROTATE_DEGREES, expand=True)
        buf = io.BytesIO()
        controls.save(buf, "PNG", compress_level=1)
        if not screen.show(buf.getvalue(), 0, 0, "FULL"):
            raise RuntimeError("the Kindle didn't draw the screen")
        while game.poll() is None:
            try:
                with open(FB_FILE, "rb") as f:
                    data = f.read(16 + FRAME_W * FRAME_H)
            except OSError:
                data = b""
            if len(data) < 16 + FRAME_W * FRAME_H:
                time.sleep(0.05)
                continue
            count = struct.unpack_from("<I", data, 12)[0]
            if count == last_count:
                time.sleep(0.01)
                continue
            last_count = count
            png = frame_png(data[16:], mode, canvas[0])
            digest = hashlib.sha1(png).digest()
            clean = time.time() - last_clean > CLEAN_EVERY_SEC
            if digest == last_hash and not clean:
                time.sleep(0.02)
                continue
            last_hash = digest
            wave = "GC16" if clean or mode == "gray" else "A2"
            if not screen.show(png, gx, gy, wave):
                raise RuntimeError("lost the connection to the Kindle")
            if clean:
                last_clean = time.time()
            shown += 1
            if shown % 10 == 0:
                os.utime(PAUSE_FILE)                     # keep the dashboard paused
            if shown % 100 == 0:
                print("%d frames, %.1f per second" % (shown, shown / (time.time() - started)), flush=True)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("DOOM stopped: %s" % e, flush=True)
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        if game.poll() is None:
            game.terminate()
            try:
                game.wait(5)
            except subprocess.TimeoutExpired:
                game.kill()
        screen.close()
        secs = max(1, time.time() - started)
        print("stopped: %d frames in %ds (%.1f per second)" % (shown, secs, shown / secs), flush=True)
        for p in (STATE_FILE, PAUSE_FILE, FB_FILE, KEYS_FILE):
            try:
                os.remove(p)
            except OSError:
                pass
        show.refresh_screen(force=True)                  # the dashboard again
        show.push_now()
    return 0


def start(mode, wad):
    if active():
        print("DOOM is already running (kindle doom stop)")
        return 0
    if not os.path.exists(BINARY) or not wads():
        print("building DOOM first (once, a couple of minutes)...", flush=True)
        if subprocess.call([os.path.join(DOOM_DIR, "build.sh")]) != 0:
            return 1
    wad = pick_wad(wad)
    if not wad:
        print("no game data in doom/wads")
        return 1
    ip, _ = kindle.address()
    if not ip:
        print("Kindle not reachable (USB or Wi-Fi)")
        return 1
    log = open(LOG_FILE, "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", mode, wad], cwd=render.BASE_DIR,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):                                  # wait until it's up
        if active():
            break
        time.sleep(0.1)
    print("DOOM (%s, %s mode) is starting on the Kindle." % (wad, mode))
    print("Controls are on screen. Menu > New Game to play; \"Quit DOOM\" (or kindle doom stop) ends it.")
    return 0


def stop():
    try:
        with open(STATE_FILE) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "start"
    if cmd == "run":
        return run(args[1], args[2])
    if cmd == "stop":
        print("stopping DOOM" if stop() else "DOOM isn't running")
        return 0
    if cmd == "wads":
        print("\n".join(wads()) or "(none: doom/build.sh downloads Freedoom)")
        return 0
    if cmd in ("start", "fast", "gray"):
        rest = args[1:] if cmd == "start" else args
        mode = "gray" if "gray" in rest else "fast"
        names = [a for a in rest if a not in ("fast", "gray")]
        return start(mode, names[0] if names else None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
