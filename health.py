"""Health screen: a cleaner, e-ink-friendly take on btop for the Pi.

Big cards for CPU (with a bar per core), temperature, memory and disk; one-hour graphs of CPU
and temperature; a status row (services, the Kindle's link, errors); and separate download and
upload panels with live speed, a one-hour graph and a speed test button each; and any
errors logged in the last hour. Everything comes from /proc, /sys and systemd, nothing extra
to install. While it's on screen, touch.py redraws it every REFRESH_SECONDS.
"""

import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime

from PIL import Image, ImageDraw

import render

HISTORY_FILE = os.path.join(render.OUT_DIR, "health_history.json")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "health_layout.json")      # where the Speed test button is
REFRESH_SECONDS = 1         # redraw this often while the screen is up (touch.py)
SAMPLE_SECONDS = 1          # one reading (graph point) this often while Pi stats is showing
BACKGROUND_SAMPLE_SECONDS = 5   # ...and this often on any other screen
IDLE_SAMPLE_SECONDS = 30        # ...or this often when idle (no taps/commands for render.IDLE_MINUTES)
SLOW_SECONDS = 10           # services, errors, throttling, Kindle status: re-checked this often
SAVE_SECONDS = 300          # history written to disk this often (spares the SD card)
HISTORY_MINUTES = 60
NET_IFACES = ("eth0", "wlan0")      # links counted for upload/download speed
SERVICES = [("dashboard.timer", "Timer"), ("kindle-touch.service", "Taps"),
            ("tailscaled.service", "Tailscale"), ("ssh.service", "SSH")]
M = 40


# ------------------------------ readings ----------------------------

def _cpu_times():
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu"):
                name, *vals = line.split()
                vals = [int(v) for v in vals[:8]]
                out[name] = (sum(vals) - vals[3] - vals[4], sum(vals))   # busy, total (idle+iowait out)
    return out


def _net_bytes():
    """(received, sent) bytes so far on the internet-facing links (Ethernet + Wi-Fi)."""
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() in NET_IFACES:
                v = data.split()
                rx, tx = rx + int(v[0]), tx + int(v[8])
    return rx, tx


# Kept in memory by whichever process samples (touch.py, every SAMPLE_SECONDS whatever is on
# screen) and saved to HISTORY_FILE once a minute, so the SD card isn't written every 5 s.
_state = {"hist": None, "cpu": None, "net": None, "use": {}, "saved": 0.0}


def record():
    """Take a reading (CPU per core, temperature, network speed) if one is due, keep the last
    HISTORY_MINUTES of them, and return the latest per-core CPU use."""
    now = time.time()
    if _state["hist"] is None:
        try:
            with open(HISTORY_FILE) as f:
                _state["hist"] = json.load(f)
        except (OSError, ValueError):
            _state["hist"] = []
    hist = _state["hist"]
    if hist and now - hist[-1][0] < SAMPLE_SECONDS and _state["use"]:
        return _state["use"]
    cpu_now, net_now = _cpu_times(), (_net_bytes(), now)
    if _state["cpu"] is None:                          # first reading in this process: measure
        time.sleep(0.5)                                # over half a second to get a rate
        _state["cpu"], _state["net"] = cpu_now, net_now
        cpu_now, now = _cpu_times(), time.time()
        net_now = (_net_bytes(), now)
    use = {}
    for k, (busy, total) in cpu_now.items():
        pb, pt = _state["cpu"].get(k, (busy, total))
        use[k] = 100.0 * (busy - pb) / (total - pt) if total > pt else 0.0
    (rx0, tx0), t0 = _state["net"]
    (rx1, tx1), t1 = net_now
    dt = max(0.1, t1 - t0)
    down, up = max(0, rx1 - rx0) * 8 / dt, max(0, tx1 - tx0) * 8 / dt   # bits per second
    _state.update(cpu=cpu_now, net=net_now, use=use)
    hist.append([round(now, 1), round(use.get("cpu", 0.0), 1), temperature(), round(down), round(up)])
    while hist and now - hist[0][0] > HISTORY_MINUTES * 60:
        hist.pop(0)
    if now - _state["saved"] >= SAVE_SECONDS:
        with open(HISTORY_FILE + ".tmp", "w") as f:
            json.dump(hist, f)
        os.replace(HISTORY_FILE + ".tmp", HISTORY_FILE)
        _state["saved"] = now
    return use


_slow_cache = {}


def _slow(name, fn):
    """fn() at most every SLOW_SECONDS; the 1-second redraws reuse the last answer."""
    hit = _slow_cache.get(name)
    if not hit or time.time() - hit[0] >= SLOW_SECONDS:
        _slow_cache[name] = hit = (time.time(), fn())
    return hit[1]


def speed(bps):
    if bps >= 1e6:
        return "%.1f Mbps" % (bps / 1e6)
    if bps >= 1e3:
        return "%.0f kbps" % (bps / 1e3)
    return "%.0f bps" % bps


def temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def throttled():
    """Short note if the Pi has been throttled or under-volted, else None."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        flags = int(out.strip().split("=")[1], 16)
    except Exception:
        return None
    if flags & 0x1:
        return "under-voltage now!"
    if flags & 0x4:
        return "throttled now"
    if flags & 0x10000:
        return "under-voltage earlier"
    if flags & 0x40000:
        return "throttled earlier"
    return None


def memory():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0]) * 1024
    total, avail = info["MemTotal"], info.get("MemAvailable", info["MemFree"])
    return total - avail, total


def uptime():
    with open("/proc/uptime") as f:
        secs = float(f.read().split()[0])
    d, rest = divmod(int(secs), 86400)
    h, m = rest // 3600, rest % 3600 // 60
    return ("%dd %dh" % (d, h)) if d else ("%dh %dm" % (h, m))


def services():
    out = []
    for unit, label in SERVICES:
        state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
        out.append((label, state or "unknown"))
    return out


def recent_errors(n=3):
    out = subprocess.run(["journalctl", "-p", "err", "--since", "-1h", "-n", str(n), "-o", "short",
                          "--no-pager", "-q"], capture_output=True, text=True).stdout
    lines = []
    for line in out.splitlines():
        parts = line.split(None, 4)                  # "Oct 03 10:20:01 host unit[pid]: message"
        if len(parts) == 5:
            lines.append("%s %s" % (parts[2][:5], parts[4]))
    return lines


# ------------------------------ drawing -----------------------------

def _card(d, box, title, value, sub, frac):
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=20, outline=0, width=3)
    d.text((x0 + 22, y0 + 16), title, font=render.ui_font(28, "semibold"), fill=0)
    d.text((x0 + 22, y0 + 52), value, font=render.ui_font(72, "bold"), fill=0)
    d.text((x0 + 22, y1 - 82), sub, font=render.ui_font(26), fill=0)
    if frac is not None:                                   # usage bar along the bottom
        bx0, bx1, by = x0 + 22, x1 - 22, y1 - 40
        d.rounded_rectangle((bx0, by, bx1, by + 18), radius=9, outline=0, width=2)
        if frac > 0.02:
            d.rounded_rectangle((bx0, by, bx0 + (bx1 - bx0) * min(1, frac), by + 18), radius=9, fill=0)


def _graph(d, box, title, points, lo, hi, unit):
    """points: [(time, value)]. Time runs left to right over the last HISTORY_MINUTES with
    "now" at the right edge, so whatever has been collected so far is plotted where it belongs."""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=20, outline=0, width=3)
    f = render.ui_font(26, "semibold")
    d.text((x0 + 22, y0 + 14), title, font=f, fill=0)
    if points:
        txt = "now %.0f%s" % (points[-1][1], unit)
        d.text((x1 - 22 - d.textlength(txt, font=f), y0 + 14), txt, font=f, fill=0)
    gx0, gy0, gx1, gy1 = x0 + 22, y0 + 56, x1 - 22, y1 - 44
    for frac in (0.5,):                                   # dotted middle line
        y = gy1 - (gy1 - gy0) * frac
        for x in range(int(gx0), int(gx1), 14):
            d.line((x, y, x + 6, y), fill=150, width=1)
    d.line((gx0, gy1, gx1, gy1), fill=0, width=2)
    now, span = time.time(), HISTORY_MINUTES * 60
    pts = [(gx1 - (gx1 - gx0) * min(span, now - t) / span,
            gy1 - (gy1 - gy0) * (min(hi, max(lo, v)) - lo) / (hi - lo)) for t, v in points]
    if len(pts) >= 2:
        d.polygon(pts + [(pts[-1][0], gy1), (pts[0][0], gy1)], fill=200)
        d.line(pts, fill=0, width=3)
    if pts:
        x, y = pts[-1]
        d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=0)  # the latest reading
    small = render.ui_font(20)
    d.text((gx0, gy1 + 8), "%d min ago" % HISTORY_MINUTES, font=small, fill=0)
    d.text((gx1 - d.textlength("now", font=small), gy1 + 8), "now", font=small, fill=0)
    mid = "%d min" % (HISTORY_MINUTES // 2)
    d.text(((gx0 + gx1 - d.textlength(mid, font=small)) / 2, gy1 + 8), mid, font=small, fill=0)


def hit(cx, cy):
    """"speedtest:down|up" (overview), "sort:cpu|mem|name|pid" (processes),
    "mirror:start" (mirror page), else None."""
    try:
        with open(LAYOUT_FILE) as f:
            buttons = json.load(f)
    except (OSError, ValueError):
        return None
    for action, box in buttons.items():
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            return action
    return None


VIEW_FILE = os.path.join(render.OUT_DIR, "stats_view.json")     # which page, how processes sort
SORTS = [("cpu", "CPU"), ("mem", "Memory"), ("name", "Name"), ("pid", "PID")]
PAGES = ("overview", "procs", "mirror")                          # swipe sideways between them


def view():
    try:
        with open(VIEW_FILE) as f:
            v = json.load(f)
    except (OSError, ValueError):
        v = {}
    if v.get("page") not in PAGES:
        v["page"] = "overview"
    v.setdefault("sort", "cpu")
    return v


def set_view(**changes):
    v = view()
    v.update(changes)
    with open(VIEW_FILE, "w") as f:
        json.dump(v, f)
    return v


def draw(w, h):
    """The page that's selected: the overview, the process list, or mirroring (swipe to switch)."""
    v = view()
    if v["page"] == "procs":
        return _draw_procs(w, h, v["sort"])
    if v["page"] == "mirror":
        return _draw_mirror(w, h)
    return _draw_overview(w, h)


# ------------------------------ processes ---------------------------

_proc_prev = {}                 # pid -> (cpu ticks, time) from the previous redraw
CLK_TCK = os.sysconf("SC_CLK_TCK")


def processes():
    """User processes (kernel threads left out) with CPU % since the last call (per core,
    like top: a busy multi-threaded process can pass 100%) and resident memory."""
    now = time.time()
    total_mem = memory()[1]
    try:
        import pwd
        users = {u.pw_uid: u.pw_name for u in pwd.getpwall()}
    except Exception:
        users = {}
    out, seen = [], {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open("/proc/%d/stat" % pid) as f:
                stat = f.read()
            with open("/proc/%d/status" % pid) as f:
                status = f.read()
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue                                   # it exited meanwhile
        rss = next((int(l.split()[1]) * 1024 for l in status.splitlines() if l.startswith("VmRSS:")), None)
        if rss is None:
            continue                                   # kernel thread
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
        ticks = int(fields[11]) + int(fields[12])      # utime + stime
        uid = next((int(l.split()[1]) for l in status.splitlines() if l.startswith("Uid:")), 0)
        prev = _proc_prev.get(pid)
        cpu = 100.0 * (ticks - prev[0]) / CLK_TCK / (now - prev[1]) if prev and now > prev[1] else 0.0
        seen[pid] = (ticks, now)
        cmd = " ".join(cmd.split())                    # newlines/tabs in a command line -> spaces
        out.append({"pid": pid, "name": comm, "cmd": cmd or comm, "cpu": max(0.0, cpu),
                    "mem": rss, "mem_pct": 100.0 * rss / total_mem, "user": users.get(uid, str(uid))})
    _proc_prev.clear()
    _proc_prev.update(seen)
    return out


def _mem_text(b):
    return "%.1f GB" % (b / 2 ** 30) if b >= 2 ** 30 else "%.0f MB" % (b / 2 ** 20)


def next_page(step):
    """Move step pages along (wrapping round). Returns the new page."""
    i = PAGES.index(view()["page"])
    return set_view(page=PAGES[(i + step) % len(PAGES)])["page"]


def _draw_mirror(w, h):
    """A single button that turns the Kindle into the Pi's screen (mirror.py pi)."""
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.text((M, 26), "Pi screen", font=render.ui_font(52, "bold"), fill=0)
    _page_dots(d, w, "mirror")
    lines = ["Shows the Raspberry Pi's desktop on the Kindle,", "a few times a second, in black and white.",
             "Tap the Kindle anywhere to stop."]
    f = render.ui_font(38)
    for n, line in enumerate(lines):
        d.text(((w - d.textlength(line, font=f)) / 2, 210 + n * 56), line, font=f, fill=0)
    box = (w / 2 - 330, 470, w / 2 + 330, 650)
    d.rounded_rectangle(box, radius=30, fill=0)
    bf = render.ui_font(64, "bold")
    d.text((w / 2, (box[1] + box[3]) / 2), "Start mirroring", font=bf, fill=255, anchor="mm")
    try:                                          # the last start failed: say why
        import mirror
        if time.time() - os.path.getmtime(mirror.ERROR_FILE) < 3600:
            with open(mirror.ERROR_FILE) as f:
                err = f.read().strip()
            ef = render.ui_font(30, "semibold")
            words, lines, cur = err.split(), [], ""
            for word in words:
                if cur and d.textlength(cur + " " + word, font=ef) > w - 2 * M - 80:
                    lines.append(cur)
                    cur = word
                else:
                    cur = (cur + " " + word).strip()
            lines.append(cur)
            box_y = 690
            d.rounded_rectangle((M, box_y, w - M, box_y + 40 + 44 * len(lines)), radius=18, outline=0, width=3)
            for n, line in enumerate(lines):
                d.text((w / 2, box_y + 42 + 44 * n), line, font=ef, fill=0, anchor="mm")
    except OSError:
        pass
    hint = "Swipe sideways for Pi stats and processes"
    sf = render.ui_font(28)
    d.text(((w - d.textlength(hint, font=sf)) / 2, h - 90), hint, font=sf, fill=0)
    with open(LAYOUT_FILE, "w") as fh:
        json.dump({"mirror:start": list(box)}, fh)
    return img


def _page_dots(d, w, page):
    for k, p in enumerate(PAGES):
        cx = w / 2 + (k - (len(PAGES) - 1) / 2) * 30
        d.ellipse((cx - 7, 46, cx + 7, 60), fill=0 if p == page else 255, outline=0, width=2)   # level with the title


def _draw_procs(w, h, sort):
    record()                                           # keep the history ticking
    procs = processes()
    key = {"cpu": lambda p: (-p["cpu"], -p["mem"]), "mem": lambda p: (-p["mem"], -p["cpu"]),
           "name": lambda p: (p["name"].lower(), p["pid"]), "pid": lambda p: p["pid"]}[sort]
    procs.sort(key=key)
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.text((M, 26), "Processes", font=render.ui_font(52, "bold"), fill=0)
    meta = "%d running  ·  %s" % (len(procs), datetime.now().strftime("%H:%M:%S"))
    mf = render.ui_font(30)
    d.text((w - M - d.textlength(meta, font=mf), 44), meta, font=mf, fill=0)
    _page_dots(d, w, "procs")
    # sort buttons
    buttons = {}
    bf = render.ui_font(28, "semibold")
    d.text((M, 132), "Sort by", font=render.ui_font(28), fill=0)
    x = M + 120
    for k, label in SORTS:
        bw = d.textlength(label, font=bf) + 50
        box = (x, 122, x + bw, 172)
        on = k == sort
        d.rounded_rectangle(box, radius=14, outline=0, width=3, fill=0 if on else 255)
        d.text((x + 25, 131), label, font=bf, fill=255 if on else 0)
        buttons["sort:" + k] = box
        x += bw + 16
    hint = "swipe for the overview"
    d.text((w - M - d.textlength(hint, font=render.ui_font(24)), 136), hint, font=render.ui_font(24), fill=0)
    # table
    cols = [("PID", M, "l"), ("Name", M + 120, "l"), ("CPU", w - M - 420, "r"),
            ("Memory", w - M - 230, "r"), ("User", w - M - 130, "l")]
    hf, rf, rb = render.ui_font(26, "semibold"), render.ui_font(26), render.ui_font(26, "semibold")
    y = 196
    for title, cx, align in cols:
        tw = d.textlength(title, font=hf)
        d.text((cx - tw if align == "r" else cx, y), title, font=hf, fill=0)
    y += 40
    d.line((M, y - 4, w - M, y - 4), fill=0, width=3)
    row_h = 38
    name_w = cols[2][1] - 110 - cols[1][1]
    for n, p in enumerate(procs):
        if y + row_h > h - M:
            break
        if n % 2 == 1:
            d.rectangle((M, y - 2, w - M, y + row_h - 4), fill=232)
        label = p["cmd"]
        while d.textlength(label, font=rf) > name_w and len(label) > 4:
            label = label[:-2].rstrip() + "…"
        cells = [(str(p["pid"]), rf), (label, rf), ("%.1f%%" % p["cpu"], rb if sort == "cpu" else rf),
                 (_mem_text(p["mem"]), rb if sort == "mem" else rf), (p["user"][:9], rf)]
        for (text, font), (_, cx, align) in zip(cells, cols):
            tw = d.textlength(text, font=font)
            d.text((cx - tw if align == "r" else cx, y), text, font=font, fill=0)
        y += row_h
    with open(LAYOUT_FILE, "w") as f:
        json.dump(buttons, f)
    return img


def _draw_overview(w, h):
    import kindle
    use = record()
    hist = _state["hist"]
    cpu = use.get("cpu", 0.0)
    temp = temperature()
    used, total = memory()
    du = shutil.disk_usage("/")

    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    d.text((M, 26), socket.gethostname(), font=render.ui_font(52, "bold"), fill=0)
    meta = "up %s  ·  %s" % (uptime(), datetime.now().strftime("%a %-d %b  %H:%M:%S"))
    mf = render.ui_font(30)
    d.text((w - M - d.textlength(meta, font=mf), 44), meta, font=mf, fill=0)
    _page_dots(d, w, "overview")

    # --- status row (where the services panel used to be): dots for services, the Kindle, errors
    sf = render.ui_font(26, "semibold")
    k = _slow("kindle", kindle.health)
    try:
        ago = (time.time() - os.path.getmtime(os.path.join(render.OUT_DIR, ".last_push"))) / 60
        pushed = "pushed " + ("just now" if ago < 1 else "%d min ago" % ago)
    except OSError:
        pushed = "no pushes yet"
    link = {"usb": "USB", "wifi": "Wi-Fi"}.get(k["link"], "not reachable")
    batt = "" if k["battery"] is None else " %d%%%s" % (k["battery"], " charging" if k["charging"] else "")
    errs = _slow("errors", lambda: recent_errors(3))
    items = [(label, state == "active") for label, state in _slow("services", services)]
    items += [("Kindle %s%s · %s" % (link, batt, pushed), k["link"] is not None),
              ("No errors (1 h)" if not errs else "%d error%s (1 h)" % (len(errs), "s" if len(errs) > 1 else ""),
               not errs)]
    x, y = M, 100
    for label, good in items:
        d.ellipse((x, y + 8, x + 18, y + 26), fill=0 if good else 255, outline=0, width=3)
        d.text((x + 26, y), label, font=sf, fill=0)
        x += 26 + d.textlength(label, font=sf) + 30

    # --- cards
    top, ch, gap = 150, 230, 20
    cw = (w - 2 * M - 3 * gap) / 4
    cores = sorted((k_ for k_ in use if k_ != "cpu"), key=lambda k_: int(k_[3:]))
    boxes = [(M + i * (cw + gap), top, M + i * (cw + gap) + cw, top + ch) for i in range(4)]
    _card(d, boxes[0], "CPU", "%.0f%%" % cpu, "", None)
    cf = render.ui_font(26)
    cores_label = "%d cores" % len(cores)
    d.text((boxes[0][2] - 22 - d.textlength(cores_label, font=cf), top + 18), cores_label, font=cf, fill=0)
    bx0, by1 = boxes[0][0] + 22, boxes[0][3] - 22                 # a little bar per core
    bw = (cw - 44 - 10 * (len(cores) - 1)) / max(1, len(cores))
    for i, c in enumerate(cores):
        bx = bx0 + i * (bw + 10)
        d.rectangle((bx, by1 - 50, bx + bw, by1), outline=0, width=2)
        hgt = 50 * use[c] / 100
        if hgt > 2:
            d.rectangle((bx, by1 - hgt, bx + bw, by1), fill=0)
    note = _slow("throttled", throttled)
    _card(d, boxes[1], "Temperature", "%.0f°C" % temp if temp else "?",
          note or ("cool" if temp and temp < 60 else "warm" if temp and temp < 75 else "hot"),
          (temp or 0) / 85)
    _card(d, boxes[2], "Memory", "%.0f%%" % (100 * used / total),
          "%.1f of %.0f GB" % (used / 2 ** 30, total / 2 ** 30), used / total)
    _card(d, boxes[3], "Disk", "%.0f%%" % (100 * du.used / du.total),
          "%.0f GB free of %.0f" % (du.free / 1e9, du.total / 1e9), du.used / du.total)

    # --- CPU + temperature graphs
    gtop, gh = top + ch + gap, 220
    gw = (w - 2 * M - gap) / 2
    _graph(d, (M, gtop, M + gw, gtop + gh), "CPU, last hour", [(p[0], p[1]) for p in hist], 0, 100, "%")
    temps = [(p[0], p[2]) for p in hist if p[2] is not None]
    _graph(d, (M + gw + gap, gtop, w - M, gtop + gh), "Temperature, last hour", temps, 30, 85, "°C")

    # --- download | upload panels, each with its own live speed, graph and speed test
    import speedtest
    btop = gtop + gh + gap
    tests = speedtest.load()
    testing = speedtest.running()
    buttons = {}
    for n, (kind, title, idx) in enumerate((("down", "Download", 3), ("up", "Upload", 4))):
        x0 = M + n * (gw + gap)
        x1 = x0 + gw
        d.rounded_rectangle((x0, btop, x1, h - M), radius=20, outline=0, width=3)
        d.text((x0 + 22, btop + 14), title, font=render.ui_font(30, "semibold"), fill=0)
        now_val = hist[-1][idx] if hist and len(hist[-1]) > idx else 0
        d.text((x0 + 22, btop + 50), speed(now_val), font=render.ui_font(56, "bold"), fill=0)
        busy = testing == kind
        bbox = (x1 - 22 - 230, btop + 14, x1 - 22, btop + 64)
        d.rounded_rectangle(bbox, radius=14, outline=0, width=3, fill=0 if busy else 255)
        blabel = "Testing…" if busy else ("Test " + title.lower())
        bf = render.ui_font(26, "semibold")
        d.text(((bbox[0] + bbox[2] - d.textlength(blabel, font=bf)) / 2, btop + 25), blabel, font=bf,
               fill=255 if busy else 0)
        buttons["speedtest:" + kind] = bbox
        # one-hour graph of this direction only, its own scale
        gx0, gx1, gy0, gy1 = x0 + 22, x1 - 22, btop + 150, h - M - 80
        pts_raw = [(p[0], p[idx]) for p in hist if len(p) > idx]
        peak = max([1e5] + [v for _, v in pts_raw])
        now_t, span = time.time(), HISTORY_MINUTES * 60
        pts = [(gx1 - (gx1 - gx0) * min(span, now_t - t) / span, gy1 - (gy1 - gy0) * v / peak) for t, v in pts_raw]
        d.line((gx0, gy1, gx1, gy1), fill=0, width=2)
        if len(pts) >= 2:
            d.polygon(pts + [(pts[-1][0], gy1), (pts[0][0], gy1)], fill=200)
            d.line(pts, fill=0, width=3)
        tiny = render.ui_font(20)
        d.text((gx0, gy0 - 26), "peak %s, last hour" % speed(peak), font=tiny, fill=0)
        # last test (or why it failed)
        res, err = tests.get(kind), tests["error"].get(kind)
        if err:
            line = "Test failed: " + err
        elif res:
            line = "Speed test %s%s  ·  %s%s%s" % (
                speed(res["bps"]), " (best second %s)" % speed(res["best"]) if res.get("best") else "",
                datetime.fromtimestamp(res["at"]).strftime("%-I:%M %p").lower(),
                "  ·  ping %.0f ms" % res["ping"] if "ping" in res else "",
                "  ·  " + res["server"] if res.get("server") else "")
        else:
            line = "Tap Test %s to measure your line's speed" % title.lower()
        size = 24
        while size > 15 and d.textlength(line, font=render.ui_font(size, "semibold")) > gx1 - gx0:
            size -= 1
        d.text((gx0, h - M - 60), line, font=render.ui_font(size, "semibold"), fill=0)
    with open(LAYOUT_FILE, "w") as f:
        json.dump(buttons, f)
    return img
