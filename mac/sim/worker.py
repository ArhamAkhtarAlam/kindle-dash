"""Runs the real dashboard code on the Mac for the Kindle simulator (app.py starts it).

It works in its own folder (SIM_HOME/dash): links to the project's code and fonts, plus its own
out/, calendar/, feeds/... so nothing here touches the Pi or the Kindle. Everything that would
talk to the Kindle is replaced: pushes just record what kind of refresh the Kindle would have
done, strip updates patch the saved screen like they do on the Pi, and Kindle commands fail
quietly (as if it were unplugged).

Protocol: one JSON request per line on stdin, one JSON reply per line on the original stdout.
Anything the code prints (render.log etc.) goes to stderr, which app.py shows in its log.
"""

import contextlib
import io
import json
import os
import sys
import time
import traceback

PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SIM_HOME = os.path.expanduser(os.environ.get("KINDLE_SIM_HOME", "~/Library/Application Support/Kindle Sim"))
DASH = os.path.join(SIM_HOME, "dash")


def build_workspace():
    """Link every project .py file and fonts/ into DASH (new files get linked on each start)."""
    os.makedirs(os.path.join(DASH, "out"), exist_ok=True)
    names = [n for n in os.listdir(PROJECT) if n.endswith(".py")] + ["fonts", "fractal"]
    for n in names:
        link, target = os.path.join(DASH, n), os.path.join(PROJECT, n)
        if os.path.islink(link) and os.readlink(link) == target:
            continue
        if os.path.lexists(link):
            os.remove(link)                            # (only ever one of our own links)
        os.symlink(target, link)
    stub = os.path.join(DASH, "push.sh")
    if not os.path.exists(stub):
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n# simulator: there is no Kindle to push to\nexit 0\n")
        os.chmod(stub, 0o755)


build_workspace()
os.chdir(DASH)
sys.path.insert(0, DASH)

proto = os.fdopen(os.dup(1), "w", buffering=1)        # replies only
os.dup2(2, 1)                                         # prints -> stderr -> the app's log
sys.stdout = sys.stderr

from PIL import ImageFont   # noqa: E402

# The Pi's DejaVu fonts (the weather's monospace, chess pieces), copied by install.sh.
FONT_DIR = os.path.expanduser("~/Library/Application Support/Kindle Sim/fonts")
_truetype = ImageFont.truetype


def _font(font=None, *a, **kw):
    if isinstance(font, str) and font.startswith("/usr/share/fonts/") and not os.path.exists(font):
        local = os.path.join(FONT_DIR, os.path.basename(font))
        if os.path.exists(local):
            font = local
    return _truetype(font, *a, **kw)


ImageFont.truetype = _font

import render       # noqa: E402  (after the workspace exists)
import kindle       # noqa: E402
import show         # noqa: E402
import touch        # noqa: E402
import timer        # noqa: E402
import reminders    # noqa: E402
import health       # noqa: E402
from PIL import Image, ImageOps   # noqa: E402

pushes = []                                           # what the Kindle would have done


def sim_push_now(photo=False, quick=False, flash=False):
    pushes.append("flash" if flash else "photo" if photo else "quick" if quick else "full")
    return 0


def sim_fast_push():
    pushes.append("quick")
    return 0


def sim_push_region(strip, box, wave=None):
    """Patch the saved screen exactly like show.push_region does, without sending it."""
    w, h = show.canvas_size()
    if render.dark_mode():
        strip = ImageOps.invert(strip.convert("L"))
    bx0, by0, bx1, by1 = box
    if not render.ROTATE:
        part, left, top = strip, bx0, by0
    elif render.ROTATE_DEGREES == 90:
        part, left, top = strip.rotate(90, expand=True), by0, w - bx1
    else:
        part, left, top = strip.rotate(270, expand=True), h - by1, bx0
    part = part.convert("L")
    if part.size == (render.WIDTH, render.HEIGHT):
        full = part
    else:
        full = Image.open(show.CUSTOM_FILE).convert("L")
        full.paste(part, (left, top))
    render.save(full, show.CUSTOM_FILE, fast=True)
    pushes.append("region %s" % (wave or "DU"))
    return 0


show.push_now = sim_push_now
show.fast_push = sim_fast_push
show.push_region = sim_push_region
kindle.address = lambda *a, **k: (None, None)
kindle.run = lambda *a, **k: (1, "")
touch.raw_to_panel = lambda x, y: (x, y)              # the app sends panel pixels already


def _no_stats(w, h):
    from PIL import ImageDraw
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.text((60, 50), "Pi stats", font=render.ui_font(56, "bold"), fill=0)
    lines = ["This screen reads the Pi's own CPU, memory and network",
             "counters (/proc), so it only works on the Pi.",
             "", "Run  kindle stats  to see it on the real Kindle."]
    for n, line in enumerate(lines):
        d.text((60, 200 + n * 60), line, font=render.ui_font(40), fill=0)
    return img


if not os.path.exists("/proc/stat"):                  # on the Mac: no Pi counters to read
    health.draw = _no_stats
    health.record = lambda *a, **k: None

state = {"minute": None}


def ensure_screen():
    if not os.path.exists(render.OUT_FILE) and show.current_image() == render.OUT_FILE:
        render.render_weather()
    if not os.path.exists(show.current_image()):
        img = Image.new("L", show.canvas_size(), 255)
        show.finish(img, render.OUT_FILE)


def tick():
    """What the Pi does in the background: the 1 s live updates and the minute timer."""
    done = timer.check_done()
    if done:
        with reminders.locked():
            reminders.raise_alert([done])
        show.push_now(flash=True)
        return
    screen = show.current_screen()
    covered = reminders.alert_active() or show.menu.is_open()
    if screen == "timer" and not covered:
        st = timer.load()
        if timer.running(st):
            w = show.canvas_size()[0]
            show.push_region(timer.digits_strip(w, st), timer.digits_box(w))
    if screen == "stats" and not covered and os.path.exists("/proc/stat"):
        health.record()
        w, h = show.canvas_size()
        show.push_region(health.draw(w, h), (0, 0, w, h), wave="GL16")
    if screen == "wmap" and not covered:              # the weather map's Play button
        import wmap
        if wmap.playing() and time.time() - state.get("frame", 0) >= wmap.FRAME_SECONDS - 0.05:
            state["frame"] = time.time()
            wmap.step_play()
            show.refresh_screen(force=True, screen="wmap")
            pushes.append("quick")
    minute = time.strftime("%H:%M")
    if minute != state["minute"]:                     # the Pi's every-minute dashboard.timer
        first = state["minute"] is None
        state["minute"] = minute
        if not first:
            if reminders.check():
                show.push_now(flash=True)
            show.refresh_screen()
            pushes.append("minute")


class Output(io.StringIO):
    def reconfigure(self, **kw):                      # show.main() asks for line buffering
        pass


MAC_ONLY = {
    "osu": "osu! runs on the real Kindle with the music on this Mac",
    "doom": "DOOM streams to the real Kindle",
    "mirror": "mirroring streams to the real Kindle", "yt": "videos stream to the real Kindle",
    "video": "videos stream to the real Kindle", "update": "it updates the Pi", "backup": "it backs up the Pi",
    "sim": "you're in it", "kiosk": "it's for the real Kindle",
}


def command(args):
    if args and args[0] in MAC_ONLY:
        return 1, ("'kindle %s' isn't part of the simulator (%s). Run it in Terminal: kindle %s"
                   % (args[0], MAC_ONLY[args[0]], " ".join(args)))
    out = Output()
    old = sys.argv
    sys.argv = ["show.py"] + list(args)
    try:
        with contextlib.redirect_stdout(out):
            try:
                code = show.main()
            except SystemExit as e:
                code = e.code
                if isinstance(code, str):                  # the command's error message
                    out.write("error: " + code + "\n")
                    code = 1
    finally:
        sys.argv = old
    return code, out.getvalue()


def handle(req):
    op = req.get("op")
    result = {}
    if op == "gesture":
        path = [tuple(p) for p in req.get("path") or []] or None
        last, now = state.get("tap"), time.time()
        if (req["kind"] == "tap" and last and now - last[0] <= touch.DOUBLE_TAP_SEC + 0.15
                and max(abs(req["start"][0] - last[1][0]), abs(req["start"][1] - last[1][1])) <= touch.DOUBLE_TAP_SLOP):
            state["tap"] = None                        # double click = the Kindle's double tap
            result["result"] = touch.double_tap(push=True)
        else:
            state["tap"] = (now, req["start"]) if req["kind"] == "tap" else None
            result["result"] = touch.handle_gesture(req["kind"], tuple(req["start"]), tuple(req["end"]),
                                                    path=path, push=True)
    elif op == "command":
        result["code"], result["output"] = command(req["args"])
    elif op == "tick":
        tick()
    elif op == "refresh":
        show.refresh_screen(force=True, screen=show.current_screen())
        pushes.append("full")
    elif op == "hello":
        pass
    return result


def reply(data):
    ensure_screen()
    img = show.current_image()
    w, h = show.canvas_size()
    data.update(image=img, mtime=os.path.getmtime(img) if os.path.exists(img) else 0,
                screen=show.current_screen(), menu=show.menu.is_open(), dark=render.dark_mode(),
                canvas=[w, h], rotate=render.ROTATE, degrees=render.ROTATE_DEGREES,
                panel=[render.WIDTH, render.HEIGHT], tap_slop=touch.TAP_SLOP,
                pushes=list(pushes), workspace=DASH)
    pushes.clear()
    proto.write(json.dumps(data, default=str) + "\n")


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        t = time.time()
        try:
            data = handle(req)
            data["error"] = None
        except Exception:
            data = {"error": traceback.format_exc()}
            print(data["error"])
        data["ms"] = round((time.time() - t) * 1000)
        data["id"] = req.get("id")
        try:
            reply(data)
        except Exception:
            proto.write(json.dumps({"id": req.get("id"), "error": traceback.format_exc()}) + "\n")


if __name__ == "__main__":
    main()
