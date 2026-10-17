"""Kindle Sim: the dashboard on the Mac, driven with the mouse. For testing screens and
layouts without the Kindle.

The real code runs in worker.py (its own copy of the data, nothing sent to the Pi or the
Kindle). Click = tap, drag = swipe (or a stroke on the drawing pad). Saving any project .py
file restarts the worker, so changes show up straight away.

Keys: ↓ open the menu · ↑ close it · ← → swipe sideways · ⌘R reload · ⌘S screenshot
"""

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageOps, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, "..", ".."))
PI = os.environ.get("KINDLE_PI", "arham@arham-pi")

SCREENS = [("Weather", ["wttr"]), ("Weather 2", ["wttr2"]), ("Moon", ["wttrmoon"]),
           ("Clock", ["clock"]), ("Calendar", ["calendar"]), ("Timer", ["timer"]),
           ("To-do", ["todo"]), ("Games", ["games"]), ("Draw", ["draw"]),
           ("Feeds", ["feeds"]), ("Slides", ["slides"]), ("Spotify", ["spotify"]),
           ("Pi stats", ["stats"]), ("xkcd", ["xkcd"])]
PAPER = ((28, 28, 30), (236, 234, 226))       # e-ink black and "white" for the paper look
BG, BEZEL = "#26272b", "#111214"


def python():
    """The simulator venv's Python. (Inside a Tk app, framework Python re-launches itself as
    Python.app, and sys.executable can then point there, without the venv's packages.)"""
    for p in (os.environ.get("KINDLE_SIM_PYTHON"), os.path.join(sys.prefix, "bin", "python"),
              os.path.expanduser("~/Library/Application Support/Kindle Sim/venv/bin/python")):
        if p and os.path.exists(p):
            return p
    return sys.executable


class Worker:
    """worker.py in a subprocess; replies and log lines come back on queues."""

    def __init__(self, replies, logs):
        self.replies, self.logs = replies, logs
        self.proc = None
        self.pending = 0
        self.lock = threading.Lock()

    def start(self):
        self.stop()
        self.pending = 0
        self.proc = subprocess.Popen([python(), "-u", os.path.join(HERE, "worker.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        proc = self.proc
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def send(self, op, **kw):
        if not self.proc or self.proc.poll() is not None:
            return False
        kw["op"] = op
        try:
            with self.lock:
                self.proc.stdin.write(json.dumps(kw) + "\n")
                self.proc.stdin.flush()
                self.pending += 1
        except (BrokenPipeError, OSError):
            return False
        return True

    def _read(self, proc):
        for line in proc.stdout:
            try:
                self.replies.put(json.loads(line))
            except ValueError:
                self.logs.put(line.rstrip())
        if proc is self.proc:
            self.replies.put({"stopped": True})

    def _read_err(self, proc):
        for line in proc.stderr:
            self.logs.put(line.rstrip())


def rounded(canvas, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, **kw)


class App:
    def __init__(self, root):
        self.root = root
        self.replies, self.logs = queue.Queue(), queue.Queue()
        self.worker = Worker(self.replies, self.logs)
        self.info = {}                    # the latest reply (screen, canvas size, rotation...)
        self.panel_img = None             # what the Kindle shows (portrait)
        self.shown_key = None
        self.view = None                  # (scale, x offset, y offset, image w, image h)
        self.drag = None
        self.mtimes = self._mtimes()
        self.landscape = tk.BooleanVar(value=True)
        self.paper = tk.BooleanVar(value=True)
        self.flashes = tk.BooleanVar(value=True)
        self._build()
        self.worker.start()
        self.worker.send("hello")
        root.after(30, self._poll)
        root.after(1000, self._tick)
        root.after(1000, self._watch)

    # ------------------------------ layout ------------------------------

    def _build(self):
        r = self.root
        r.title("Kindle Sim")
        r.geometry("1320x880")
        r.minsize(900, 600)
        r.configure(bg=BG)
        style = ttk.Style()
        style.configure("Side.TFrame", background=BG)
        style.configure("Side.TLabel", background=BG, foreground="#e8e8e8")
        style.configure("Head.TLabel", background=BG, foreground="#9a9ca3", font=("SF Pro Text", 11, "bold"))
        style.configure("Title.TLabel", background=BG, foreground="#ffffff", font=("SF Pro Display", 20, "bold"))
        style.configure("Side.TCheckbutton", background=BG, foreground="#e8e8e8")

        side = ttk.Frame(r, style="Side.TFrame", padding=(14, 14, 14, 8))
        side.pack(side="right", fill="y")
        main = tk.Frame(r, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(main, bg=BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.status = tk.Label(main, text="starting…", anchor="w", bg=BG, fg="#b9bcc4",
                               font=("SF Mono", 11), padx=14, pady=6)
        self.status.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self._show(force=True))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        ttk.Label(side, text="Kindle Sim", style="Title.TLabel").pack(anchor="w")
        ttk.Label(side, text="click = tap · drag = swipe", style="Side.TLabel").pack(anchor="w", pady=(0, 10))

        ttk.Label(side, text="SCREENS", style="Head.TLabel").pack(anchor="w")
        grid = ttk.Frame(side, style="Side.TFrame")
        grid.pack(fill="x", pady=(4, 10))
        for n, (label, args) in enumerate(SCREENS):
            b = ttk.Button(grid, text=label, command=lambda a=args: self.command(a))
            b.grid(row=n // 2, column=n % 2, sticky="ew", padx=2, pady=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        ttk.Label(side, text="GESTURES", style="Head.TLabel").pack(anchor="w")
        g = ttk.Frame(side, style="Side.TFrame")
        g.pack(fill="x", pady=(4, 10))
        for n, (label, fn) in enumerate([("Menu  ↓", self.menu_down), ("Close  ↑", self.menu_up),
                                         ("◀  Swipe", lambda: self.sideways(-1)),
                                         ("Swipe  ▶", lambda: self.sideways(1))]):
            ttk.Button(g, text=label, command=fn).grid(row=n // 2, column=n % 2, sticky="ew", padx=2, pady=2)
        g.columnconfigure(0, weight=1)
        g.columnconfigure(1, weight=1)

        ttk.Label(side, text="VIEW", style="Head.TLabel").pack(anchor="w")
        for text, var in (("Landscape (as you hold it)", self.landscape), ("Paper colours", self.paper),
                          ("Flash on full refreshes", self.flashes)):
            ttk.Checkbutton(side, text=text, variable=var, style="Side.TCheckbutton",
                            command=lambda: self._show(force=True)).pack(anchor="w")
        self.dark_btn = ttk.Button(side, text="Dark mode", command=self.toggle_dark)
        self.dark_btn.pack(fill="x", pady=(4, 10))

        ttk.Label(side, text="COMMAND  (like  kindle …)", style="Head.TLabel").pack(anchor="w")
        self.entry = ttk.Entry(side)
        self.entry.pack(fill="x", pady=(4, 2))
        self.entry.bind("<Return>", lambda e: self.run_entry())
        ttk.Label(side, text="e.g.  todo add milk · timer 2m · games chess", style="Side.TLabel",
                  font=("SF Pro Text", 10)).pack(anchor="w", pady=(0, 10))

        ttk.Label(side, text="TOOLS", style="Head.TLabel").pack(anchor="w")
        t = ttk.Frame(side, style="Side.TFrame")
        t.pack(fill="x", pady=(4, 8))
        for n, (label, fn) in enumerate([("Redraw", lambda: self.worker.send("refresh")),
                                         ("Reload code", self.reload), ("Screenshot", self.screenshot),
                                         ("Copy data from Pi", self.copy_from_pi),
                                         ("Open data folder", self.open_data)]):
            ttk.Button(t, text=label, command=fn).grid(row=n // 2, column=n % 2, sticky="ew", padx=2, pady=2)
        t.columnconfigure(0, weight=1)
        t.columnconfigure(1, weight=1)

        ttk.Label(side, text="LOG", style="Head.TLabel").pack(anchor="w")
        self.log = tk.Text(side, width=40, height=10, bg="#1a1b1e", fg="#c9ccd3", relief="flat",
                           font=("SF Mono", 10), wrap="word", highlightthickness=0, padx=6, pady=6)
        self.log.pack(fill="both", expand=True, pady=(4, 0))
        self.log.tag_configure("err", foreground="#ff8a80")
        self.log.tag_configure("me", foreground="#8ab4f8")

        r.bind("<Key>", self._key)
        r.bind("<Command-r>", lambda e: self.reload())
        r.bind("<Command-s>", lambda e: self.screenshot())

    # ------------------------------ geometry ----------------------------

    def _canvas_to_panel(self, x, y):
        """Dashboard canvas (landscape) -> Kindle panel pixels, as the Pi's touch code expects."""
        i = self.info
        if not i.get("rotate"):
            return x, y
        w, h = i["canvas"]
        if i["degrees"] == 90:
            return y, w - 1 - x
        return h - 1 - y, x

    def _event_to_panel(self, ex, ey):
        if not self.view:
            return None
        scale, ox, oy, iw, ih = self.view
        x = min(max((ex - ox) / scale, 0), iw - 1)
        y = min(max((ey - oy) / scale, 0), ih - 1)
        if self.landscape.get() and self.info.get("rotate"):
            return self._canvas_to_panel(x, y)
        return x, y

    def _display_image(self):
        img = self.panel_img
        if img is None:
            return None
        if self.landscape.get() and self.info.get("rotate"):
            img = img.rotate(-self.info["degrees"], expand=True)
        return img

    # ------------------------------ drawing -----------------------------

    def _show(self, force=False, inverted=False):
        img = self._display_image()
        if img is None:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 50 or ch < 50:
            return
        pad = 26
        scale = min((cw - 2 * pad - 20) / img.width, (ch - 2 * pad - 20) / img.height)
        dw, dh = int(img.width * scale), int(img.height * scale)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.view = (scale, ox, oy, img.width, img.height)
        small = img.convert("L").resize((dw, dh), Image.LANCZOS)
        if inverted:
            small = ImageOps.invert(small)
        if self.paper.get():
            small = ImageOps.colorize(small, PAPER[0], PAPER[1])
        self.tk_img = ImageTk.PhotoImage(small)
        c = self.canvas
        c.delete("all")
        rounded(c, ox - pad, oy - pad, ox + dw + pad, oy + dh + pad, 30, fill=BEZEL, outline="#3a3b40")
        c.create_image(ox, oy, image=self.tk_img, anchor="nw")

    def _flash(self):
        if not self.flashes.get():
            return self._show()
        self._show(inverted=True)
        self.root.after(110, self._show)

    # ------------------------------ input -------------------------------

    def _press(self, e):
        p = self._event_to_panel(e.x, e.y)
        if p:
            self.drag = {"start": p, "path": [p], "last": (e.x, e.y)}

    def _motion(self, e):
        if not self.drag:
            return
        p = self._event_to_panel(e.x, e.y)
        lp = self.drag["path"][-1]
        if abs(p[0] - lp[0]) + abs(p[1] - lp[1]) >= 3:
            self.drag["path"].append(p)
        lx, ly = self.drag["last"]
        self.canvas.create_line(lx, ly, e.x, e.y, fill="#d0473a", width=3, tags="trail",
                                capstyle="round")
        self.drag["last"] = (e.x, e.y)

    def _release(self, e):
        if not self.drag:
            return
        end = self._event_to_panel(e.x, e.y)
        start, path = self.drag["start"], self.drag["path"] + [end]
        self.drag = None
        slop = self.info.get("tap_slop", 60)
        moved = max(max(abs(p[0] - start[0]), abs(p[1] - start[1])) for p in path)
        kind = "tap" if moved <= slop else "swipe"
        if kind == "tap":
            self.canvas.create_oval(e.x - 14, e.y - 14, e.x + 14, e.y + 14, outline="#d0473a",
                                    width=3, tags="trail")
        self.gesture(kind, start if kind == "tap" else start, start if kind == "tap" else end, path)

    def gesture(self, kind, start, end, path=None):
        r = lambda p: [int(round(p[0])), int(round(p[1]))]
        self.last_action = kind
        self.worker.send("gesture", kind=kind, start=r(start), end=r(end),
                         path=[r(p) for p in (path or [start, end])])

    def _swipe_canvas(self, x0, y0, x1, y1):
        if not self.info.get("canvas"):
            return
        a, b = self._canvas_to_panel(x0, y0), self._canvas_to_panel(x1, y1)
        steps = [self._canvas_to_panel(x0 + (x1 - x0) * k / 8, y0 + (y1 - y0) * k / 8) for k in range(9)]
        self.gesture("swipe", a, b, steps)

    def menu_down(self):
        w, h = self.info.get("canvas", (1448, 1072))
        self._swipe_canvas(w / 2, 10, w / 2, h * 0.55)

    def menu_up(self):
        w, h = self.info.get("canvas", (1448, 1072))
        self._swipe_canvas(w / 2, h * 0.7, w / 2, h * 0.1)

    def sideways(self, direction):
        """direction -1: finger moves left (next page), +1: moves right."""
        w, h = self.info.get("canvas", (1448, 1072))
        a, b = (w * 0.75, w * 0.25) if direction < 0 else (w * 0.25, w * 0.75)
        self._swipe_canvas(a, h / 2, b, h / 2)

    def _key(self, e):
        if isinstance(self.root.focus_get(), (ttk.Entry, tk.Entry, tk.Text)):
            return
        fn = {"Down": self.menu_down, "Up": self.menu_up,
              "Left": lambda: self.sideways(-1), "Right": lambda: self.sideways(1)}.get(e.keysym)
        if fn:
            fn()

    # ------------------------------ actions -----------------------------

    def command(self, args):
        self.say("kindle " + " ".join(shlex.quote(a) for a in args), "me")
        self.worker.send("command", args=args)

    def run_entry(self):
        text = self.entry.get().strip()
        if not text:
            return
        try:
            args = shlex.split(text)
        except ValueError as e:
            return self.say("can't read that: %s" % e, "err")
        if args and args[0] == "kindle":
            args = args[1:]
        self.entry.delete(0, "end")
        self.canvas.focus_set()
        if args:
            self.command(args)

    def toggle_dark(self):
        self.command(["dark", "off" if self.info.get("dark") else "on"])

    def reload(self, why="reloaded"):
        self.say("— %s —" % why, "me")
        self.worker.start()
        self.worker.send("hello")
        self.shown_key = None

    def screenshot(self):
        img = self._display_image()
        if img is None:
            return
        path = os.path.expanduser("~/Desktop/kindle-sim-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
        img.save(path)
        self.say("saved %s" % path, "me")

    def open_data(self):
        ws = self.info.get("workspace")
        if ws:
            subprocess.Popen(["open", ws])

    def copy_from_pi(self):
        ws = self.info.get("workspace")
        if not ws:
            return
        if not messagebox.askyesno("Copy data from the Pi",
                                   "Replace the simulator's to-dos, reminders, games, calendars, feeds, "
                                   "slides and drawings with a copy of the Pi's?\n\nThe Pi is only read, "
                                   "nothing on it changes. Spotify login isn't copied."):
            return
        self.say("copying from %s…" % PI, "me")

        def run():
            ok = True
            for d in ("out", "calendar", "feeds", "slides", "draw"):
                r = subprocess.run(["rsync", "-a", "--exclude", ".*", "%s:kindle-dash/%s/" % (PI, d),
                                    os.path.join(ws, d) + "/"], capture_output=True, text=True)
                if r.returncode not in (0, 23):           # 23: folder doesn't exist on the Pi
                    ok = False
                    self.logs.put("copy %s failed: %s" % (d, r.stderr.strip()[:200]))
            self.logs.put("copied the Pi's data" if ok else "copy finished with errors")
            self.worker.send("refresh")

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------ loops -------------------------------

    def say(self, text, tag=None):
        self.log.insert("end", text + "\n", tag or ())
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "100.0")
        self.log.see("end")

    def _poll(self):
        while not self.logs.empty():
            line = self.logs.get()
            self.say(line, "err" if ("Error" in line or "Traceback" in line or "failed" in line) else None)
        while not self.replies.empty():
            d = self.replies.get()
            if d.get("stopped"):
                self.worker.pending = 0
                self.status.configure(text="the dashboard code stopped (see the log); save a file or ⌘R to restart")
                continue
            self.worker.pending = max(0, self.worker.pending - 1)
            self._apply(d)
        self.root.after(30, self._poll)

    def _apply(self, d):
        if d.get("output"):
            out = d["output"].rstrip()
            self.say(out, "err" if d.get("code") or out.startswith(("error:", "'kindle ")) else None)
        if d.get("error"):
            self.say(d["error"].strip().splitlines()[-1], "err")
        if "canvas" not in d:
            return
        self.info = d
        self.dark_btn.configure(text="Dark mode: on" if d["dark"] else "Dark mode: off")
        key = (d["image"], d["mtime"])
        flash = any(p in ("flash", "photo", "full") for p in d["pushes"])
        if key != self.shown_key:
            self.shown_key = key
            try:
                self.panel_img = Image.open(d["image"]).convert("L")
                self.panel_img.load()
            except OSError as e:
                self.say("couldn't open the screen image: %s" % e, "err")
            self._flash() if flash else self._show()
        elif self.canvas.find_withtag("trail"):
            self._show()
        pushes = [p for p in d["pushes"] if p != "minute"]
        bits = ["screen: %s" % ("menu" if d["menu"] else d["screen"] or "?")]
        if d.get("result"):
            bits.append(str(d["result"]))
        if pushes:
            bits.append("refresh: " + ", ".join(pushes))
        if d.get("ms") and (pushes or d.get("result") or d.get("output")):
            bits.append("%d ms" % d["ms"])
        if pushes or d.get("result") or d.get("output") or not self.status.cget("text").startswith("screen"):
            self.status.configure(text="   ·   ".join(bits))

    def _tick(self):
        if self.worker.pending == 0:
            self.worker.send("tick")
        self.root.after(1000 - int(time.time() * 1000) % 1000 or 1000, self._tick)

    def _mtimes(self):
        out = {}
        for d in (PROJECT, HERE):
            for n in os.listdir(d):
                if n.endswith(".py"):
                    try:
                        out[os.path.join(d, n)] = os.path.getmtime(os.path.join(d, n))
                    except OSError:
                        pass
        return out

    def _watch(self):
        now = self._mtimes()
        changed = [os.path.basename(p) for p, t in now.items() if self.mtimes.get(p) != t]
        self.mtimes = now
        if changed:
            if "app.py" in changed:
                self.say("app.py changed: quit and reopen Kindle Sim to use it", "me")
            self.reload("code changed: %s" % ", ".join(sorted(changed)))
            self.worker.send("refresh")
        self.root.after(1000, self._watch)


def selftest(root, app):
    """--selftest: drive the window with synthetic mouse events and print what happened."""
    steps, seen = [], []

    def click_canvas(x, y, to=None):
        """Press/drag/release at dashboard-canvas coords, through the real mouse handlers."""
        scale, ox, oy, iw, ih = app.view
        ev = lambda cx, cy: type("E", (), {"x": ox + cx * scale, "y": oy + cy * scale})()
        app._press(ev(x, y))
        if to:
            for k in range(1, 9):
                app._motion(ev(x + (to[0] - x) * k / 8, y + (to[1] - y) * k / 8))
        app._release(ev(*(to or (x, y))))

    w, h = 1448, 1072
    steps += [("command", lambda: app.command(["clock"])),
              ("tap clock", lambda: click_canvas(w / 2, h / 2)),
              ("drag down from top", lambda: click_canvas(w / 2, 15, (w / 2, h * 0.6))),
              ("tap outside menu", lambda: click_canvas(w / 2, h - 40)),
              ("games chess", lambda: app.command(["games", "chess"])),
              ("Down key", lambda: app.menu_down()),
              ("Up key", lambda: app.menu_up())]

    def run(i=0):
        if app.worker.pending:
            return root.after(100, run, i)
        if i:
            seen.append("%-20s -> screen=%s menu=%s result=%s" % (steps[i - 1][0], app.info.get("screen"),
                        app.info.get("menu"), app.info.get("result")))
        if i == len(steps):
            print("\n".join(seen))
            print("view:", app.view and [round(v, 3) for v in app.view])
            app.worker.stop()
            return root.destroy()
        steps[i][1]()
        root.after(300, run, i + 1)

    root.after(2500, run)


def main():
    root = tk.Tk()
    icon = os.path.join(HERE, "icon.png")
    if os.path.exists(icon):
        root.iconphoto(True, tk.PhotoImage(file=icon))
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.worker.stop(), root.destroy()))
    if "--selftest" in sys.argv:
        selftest(root, app)
    root.mainloop()


if __name__ == "__main__":
    main()
