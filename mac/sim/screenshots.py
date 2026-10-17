"""Renders the README's screen previews into docs/screens/ with the simulator's worker, in a
throwaway workspace (your simulator data isn't touched). Pi stats comes from the real Pi.

  mac/sim/screenshots.py          (run with the simulator's Python)
"""

import json
import os
import subprocess
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(PROJECT, "docs", "screens")
PI = os.environ.get("KINDLE_PI", "arham@arham-pi")
WIDTH = 720                                           # README image width (half size)

SHOTS = [
    ("weather", [["wttr"]]),
    ("weather2", [["wttr2"]]),
    ("clock", [["clock", "both"]]),
    ("clock-analog", [["clock", "analog"]]),
    ("calendar", [["calendar"]]),
    ("todo", [["todo", "add", "Buy milk"], ["todo", "add", "Call the bank"],
              ["todo", "add", "Finish the chemistry notes"], ["todo"]]),
    ("timer", [["timer", "25m"]]),
    ("games", [["games"]]),
    ("chess", [["games", "chess", "2p"]]),
    ("menu", [["clock", "both"], "menu"]),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    home = tempfile.mkdtemp(prefix="kindle-shots-")
    env = dict(os.environ, KINDLE_SIM_HOME=home)
    w = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "worker.py")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)

    def ask(**req):
        w.stdin.write(json.dumps(req) + "\n")
        w.stdin.flush()
        reply = json.loads(w.stdout.readline())
        if reply.get("error"):
            print("  ", reply["error"].strip().splitlines()[-1])
        return reply

    for name, steps in SHOTS:
        for step in steps:
            if step == "menu":                         # swipe down from the top (panel x = canvas y)
                r = ask(op="gesture", kind="swipe", start=[10, 536], end=[600, 536],
                        path=[[10, 536], [300, 536], [600, 536]])
            else:
                r = ask(op="command", args=step)
        if name == "chess":                            # a few moves in: 1. e4 e5 2. Nf3 (Nc6 selected)
            for sq in ("e2", "e4", "e7", "e5", "g1", "f3", "b8"):
                r = save_tap(ask, sq)
        save(name, r)
    w.stdin.close()
    w.wait()
    stats = os.path.join(home, "stats.png")
    cmd = ("cd ~/kindle-dash && venv/bin/python -c 'import sys, health, show; "
           "health.draw(*show.canvas_size()).save(sys.stdout.buffer, \"PNG\")'")
    with open(stats, "wb") as f:
        if subprocess.run(["ssh", PI, cmd], stdout=f).returncode == 0:
            shrink(Image.open(stats)).save(os.path.join(OUT, "stats.png"), optimize=True)
            print("stats (from the Pi)")


def save_tap(ask, sq):
    """Tap a chess square (white at the bottom, 2-player board) through the touch code."""
    top, cell = 125, (1072 - 90 - 125 - 34) // 8
    x0 = (1448 - 8 * cell) // 2
    col, row = "abcdefgh".index(sq[0]), 8 - int(sq[1])
    cx, cy = x0 + col * cell + cell // 2, top + row * cell + cell // 2
    px, py = cy, 1448 - 1 - cx                         # canvas -> panel (rotated 90)
    return ask(op="gesture", kind="tap", start=[px, py], end=[px, py], path=[[px, py]])


def shrink(img):
    img = img.convert("L")
    return img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.LANCZOS)


def save(name, reply):
    img = Image.open(reply["image"])
    if reply["rotate"]:
        img = img.rotate(-reply["degrees"], expand=True)   # back to how you hold it
    shrink(img).save(os.path.join(OUT, name + ".png"), optimize=True)
    print(name)


if __name__ == "__main__":
    main()
