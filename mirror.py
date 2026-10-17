#!/usr/bin/env python3
"""Mirror a screen onto the Kindle. The Mac's "kindle mirror" streams screenshots to this
over ssh; each frame is fitted to the Kindle (same orientation as every other screen),
turned into e-ink pixels, and pushed with FBInk. If frames arrive faster than the panel
can show them, only the newest is shown, so it never falls behind.

  mirror.py receive [fast|gray]   read frames on stdin: 10-digit length + image bytes
  mirror.py video FILE [fast|gray]  play a video file in real time (kindle yt / sound on the Mac);
                                  prints START when the first frame is up
  mirror.py pi [fast|gray]        show the Pi's own (headless) desktop: the Kindle becomes
                                  the Pi's screen. The desktop is resized to the Kindle's
                                  1448x1072 while mirroring and put back afterwards.

fast = black/white, FBInk's quick A2 refresh (a few frames a second)
gray = 16 grey levels, normal refresh (sharper text and photos, about 1 frame a second)
Greys are dithered with a 4x4 Bayer pattern (render.DITHER = "bayer"), which stays still
between frames instead of shimmering.
The dashboard is paused while mirroring; a tap on the Kindle stops it.
"""

import hashlib
import io
import os
import signal
import subprocess
import sys
import threading
import time

from PIL import Image, ImageOps

import kindle
import render

PAUSE_FILE = os.path.join(render.OUT_DIR, ".pause")
ERROR_FILE = os.path.join(render.OUT_DIR, "mirror_error.txt")   # why the last start failed (Mirror page shows it)
FBINK = "/mnt/us/libkh/bin/fbink"
REMOTE = "/tmp/mirror.png"                 # Kindle's RAM disk: no wear on its storage
FULL_EVERY_SEC = 30                        # a full refresh now and then clears ghosting
PI_FPS = 4                                 # Pi desktop: screenshots per second (at most)
VIDEO_FPS = 10                             # video files: frames decoded per second


class Latest:
    """Holds only the newest frame from the reader thread."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame, self.done = None, False

    def put(self, data):
        with self.cond:
            self.frame = data
            self.cond.notify()

    def take(self):
        with self.cond:
            while self.frame is None and not self.done:
                self.cond.wait()
            data, self.frame = self.frame, None
            return data


def read_frames(stream, latest):
    try:
        while True:
            head = stream.read(10)
            if len(head) < 10:
                break
            data = stream.read(int(head))
            if not data:
                break
            latest.put(data)
    finally:
        with latest.cond:
            latest.done = True
            latest.cond.notify()


# ------------------------- Pi desktop source -------------------------

def _wayland_env():
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return env


def _pi_output():
    """(output name, "WxH") of the Pi's desktop screen (the headless one when no monitor)."""
    out = subprocess.run(["wlr-randr"], capture_output=True, text=True, env=_wayland_env()).stdout
    name, mode = None, None
    for line in out.splitlines():
        if line and not line.startswith(" "):
            name = name or line.split()[0]
        elif "(current)" in line and name and not mode:
            mode = line.split()[0]
    if not name:
        if subprocess.run(["pgrep", "-x", "kwin_wayland"], capture_output=True).returncode == 0:
            raise SystemExit("The Pi is on the KDE Plasma desktop, which can't be mirrored. At the Pi's "
                             "login screen, pick the Raspberry Pi desktop (labwc) instead.")
        raise SystemExit("Couldn't find the Pi's desktop (is anyone logged in to it?)")
    return name, mode


def _set_mode(name, mode):
    subprocess.run(["wlr-randr", "--output", name, "--custom-mode", mode],
                   env=_wayland_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _plasma():
    return subprocess.run(["pgrep", "-x", "kwin_wayland"], capture_output=True).returncode == 0


def _waiting(text):
    """Show a note on the Mirror page while we wait (e.g. for approval on the Pi's screen)."""
    import show
    with open(ERROR_FILE, "w") as f:
        f.write(text)
    if show.current_screen() == "stats":
        show.refresh_screen(force=True, screen="stats")
        show.push_now(quick=True)


def start_screencast(w, h):
    """KDE Plasma: the desktop's screen sharing (pi_screencast.py). Returns the running helper
    once frames are coming, or raises SystemExit with the reason (no approval, ...)."""
    helper = subprocess.Popen(["/usr/bin/python3", os.path.join(render.BASE_DIR, "pi_screencast.py"),
                               str(w), str(h), str(PI_FPS)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL)
    errors = []

    def read_err():
        for line in helper.stderr:
            line = line.decode(errors="replace").strip()
            print("screen sharing: " + line, flush=True)
            if line.startswith("first time"):
                _waiting("Waiting for you: on the Pi's screen (remote desktop), tick 'allow restoring' "
                         "and click Allow to share the screen.")
            if line.startswith("ERROR"):
                errors.append(line[6:])
    threading.Thread(target=read_err, daemon=True).start()
    first = helper.stdout.readline()
    if not first.startswith(b"READY"):
        helper.wait(5)
        time.sleep(0.2)
        raise SystemExit("Mirroring on KDE Plasma: " + (errors[0] if errors else "screen sharing didn't start"))
    return helper


def capture_screencast(helper, w, h, latest, stop):
    size = w * h
    while not stop.is_set():
        data = helper.stdout.read(size)
        if len(data) < size:
            break
        latest.put(Image.frombytes("L", (w, h), data))
    with latest.cond:
        latest.done = True
        latest.cond.notify()


def capture_pi(latest, stop):
    """Screenshot the Pi desktop (with pointer) PI_FPS times a second into latest."""
    env = _wayland_env()
    while not stop.is_set():
        t = time.time()
        shot = subprocess.run(["grim", "-c", "-t", "ppm", "-"], capture_output=True, env=env)
        if shot.returncode == 0 and shot.stdout:
            latest.put(shot.stdout)
        stop.wait(max(0, 1 / PI_FPS - (time.time() - t)))
    with latest.cond:
        latest.done = True
        latest.cond.notify()


def capture_video(path, latest, stop):
    """Decode a video in real time (ffmpeg -re) into raw Kindle-sized grey frames."""
    import show
    w, h = show.canvas_size()
    vf = ("fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,format=gray" % (VIDEO_FPS, w, h, w, h))
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-i", path, "-an", "-vf", vf,
                             "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE)
    size = w * h
    try:
        while not stop.is_set():
            frame = proc.stdout.read(size)
            if len(frame) < size:
                break
            latest.put(Image.frombytes("L", (w, h), frame))
    finally:
        proc.kill()
        proc.wait()
        with latest.cond:
            latest.done = True
            latest.cond.notify()


def to_panel(data, mode):
    """A screenshot (image bytes) or a decoded frame (PIL image) -> PNG for the Kindle
    (portrait panel, canvas rotated like the rest)."""
    import show                                    # (for canvas_size / to_16_grays)
    w, h = show.canvas_size()
    src = data.convert("L") if isinstance(data, Image.Image) else Image.open(io.BytesIO(data)).convert("L")
    img = Image.new("L", (w, h), 255)
    fitted = ImageOps.contain(src, (w, h), Image.Resampling.LANCZOS)
    img.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
    if mode == "fast":                             # black/white for the fast A2 refresh
        if render.DITHER == "bayer":
            import dither
            img = dither.bw(img)                   # 4x4 pattern: stable between frames
        else:
            img = img.convert("1")                 # Floyd-Steinberg
    else:
        img = show.to_16_grays(img)
    if render.ROTATE:
        img = img.rotate(render.ROTATE_DEGREES, expand=True)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=1)
    return buf.getvalue()


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("receive", "pi", "video") or (args[0] == "video" and len(args) < 2):
        print(__doc__)
        return 2
    source = args[0]
    video_path = args[1] if source == "video" else None
    rest = args[2:] if source == "video" else args[1:]
    mode = rest[0] if rest and rest[0] in ("fast", "gray") else "fast"
    ip, via = kindle.address()
    if not ip:
        print("Kindle not reachable (USB or Wi-Fi)", flush=True)
        return 1
    # One ssh connection to the Kindle, reused for every frame (ControlMaster).
    ctl = "/tmp/kindle-mirror-%d" % os.getpid()
    ssh = ["ssh"] + render.ssh_options() + ["-o", "ControlMaster=auto", "-o", "ControlPath=" + ctl,
                                            "-o", "ControlPersist=20", "root@" + ip]
    helper = None
    if source == "pi":                             # check the desktop BEFORE pausing anything, so a
        try:                                       # failed start can't leave the Kindle unresponsive
            if _plasma():
                import show
                helper = start_screencast(*show.canvas_size())
                pi_desktop = None
            else:
                pi_desktop = _pi_output()
        except SystemExit as e:
            with open(ERROR_FILE, "w") as f:
                f.write(str(e))
            print(e, flush=True)
            return 1
    try:
        os.remove(ERROR_FILE)
    except OSError:
        pass
    with open(PAUSE_FILE, "w") as f:               # dashboard: hands off (and how to stop us)
        f.write(str(os.getpid()))

    def stop(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)

    latest, stop_capture, restore = Latest(), threading.Event(), None
    if source == "pi" and helper:
        import show
        w, h = show.canvas_size()
        threading.Thread(target=capture_screencast, args=(helper, w, h, latest, stop_capture), daemon=True).start()
        what = "the Pi's KDE desktop (screen sharing)"
    elif source == "pi":
        import show
        name, old_mode = pi_desktop
        w, h = show.canvas_size()                  # Kindle landscape: 1448x1072
        if name.upper().startswith(("NOOP", "HEADLESS")):
            _set_mode(name, "%dx%d" % (w, h))      # virtual screen: make it exactly Kindle-sized
            restore = (name, old_mode)
            what = "the Pi's desktop (resized to %dx%d, was %s)" % (w, h, old_mode)
        else:                                      # a real monitor: leave its resolution alone
            what = "the Pi's desktop on %s (%s, scaled to fit)" % (name, old_mode)
        threading.Thread(target=capture_pi, args=(latest, stop_capture), daemon=True).start()
    elif source == "video":
        threading.Thread(target=capture_video, args=(video_path, latest, stop_capture), daemon=True).start()
        what = "the video"
    else:
        threading.Thread(target=read_frames, args=(sys.stdin.buffer, latest), daemon=True).start()
        what = "the incoming screen"
    print("mirroring %s to the Kindle over %s (%s mode). Ctrl+C or tap the Kindle to stop."
          % (what, "USB" if via == "usb" else "Wi-Fi", mode), flush=True)
    shown, last_hash, last_full, started = 0, None, 0, time.time()
    try:
        # stdin=DEVNULL: with no text to print, FBInk reads text from stdin, which here would
        # be the incoming screenshots (it printed them as "invalid UTF-8" and ate the frames).
        subprocess.run(ssh + ["%s -q -c -f" % FBINK], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        while True:
            data = latest.take()
            if data is None:
                break
            png = to_panel(data, mode)
            digest = hashlib.sha1(png).hexdigest()
            if digest == last_hash:                # nothing changed on screen
                os.utime(PAUSE_FILE)
                continue
            last_hash = digest
            full = time.time() - last_full > FULL_EVERY_SEC
            flags = "-f" if full else ("-W A2" if mode == "fast" else "")
            subprocess.run(ssh + ["cat > %s && %s -q %s -g file=%s" % (REMOTE, FBINK, flags, REMOTE)],
                           input=png, stdout=subprocess.DEVNULL)
            if full:
                last_full = time.time()
            if shown == 0 and source == "video":
                print("START", flush=True)             # the Mac starts the sound now
            shown += 1
            os.utime(PAUSE_FILE)                   # keep the pause fresh while we run
    except KeyboardInterrupt:
        pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)     # finish cleaning up even if asked to stop again
        stop_capture.set()
        if helper and helper.poll() is None:
            helper.terminate()                     # stops the screen sharing too
        if restore and restore[1]:
            _set_mode(*restore)                    # the Pi desktop's own size again
        secs = max(1, time.time() - started)
        print("stopped: %d frames in %ds (%.1f per second)" % (shown, secs, shown / secs), flush=True)
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass
        subprocess.run(["ssh", "-o", "ControlPath=" + ctl, "-O", "exit", "root@" + ip],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import show                                # put the dashboard back
        show.refresh_screen(force=True)
        show.push_now()
    return 0


if __name__ == "__main__":
    sys.exit(main())
