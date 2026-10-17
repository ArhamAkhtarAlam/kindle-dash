#!/usr/bin/env python3
"""Render the wttr.in forecast to a 1072x1448 grayscale PNG for a Kindle.

Usage:
  render.py                 redraw whatever is showing if it changes over time (weather,
                            clock, feeds); wttr.in fetched at most every WEATHER_CACHE_MINUTES
  render.py --print-config  shell variables for push.sh
  render.py --timer         print dashboard.timer for REFRESH_MINUTES
"""

# ============================== CONFIG ==============================
LOCATION = "London"             # your city (any place wttr.in understands); set yours in
                                # local_settings.py (see the end of this block)
KINDLE_IP = "192.168.15.244"
PI_USB_IP = "192.168.15.201"
KINDLE_WIFI_IP = None           # None = learn the Kindle's Wi-Fi address automatically
                                # (used when the USB cable is unplugged)
WIDTH = 1072                    # Kindle screen, portrait
HEIGHT = 1448
ROTATE = True                   # draw landscape (1448x1072), then rotate to portrait
ROTATE_DEGREES = 90             # 90 or 270: flip this if the image is upside down
REFRESH_MINUTES = 1             # push to the Kindle this often (after changing:
                                # render.py --timer > dashboard.timer, then reinstall it)
WEATHER_CACHE_MINUTES = 15      # only ask wttr.in this often (it rate-limits; data is hourly)
FULL_REFRESH_EVERY = 10         # full black/white clear every N pushes, partial otherwise
# Dark mode ghosts more on e-ink (white-on-black edges linger), so it cleans up more often:
DARK_FULL_REFRESH_EVERY = 3     #   minute updates: a full clear every N (instead of the above)
DARK_QUICK_FULL_EVERY = 5       #   taps (games, menu, buttons...): every Nth one flashes clean
DARK_CLEAN_EVERY = 12           #   1-second strips (timer digits, Pi stats): a clean pass every N
CLOCK_FULL_REFRESH_EVERY = 30   # the clock (light or dark): a full flash only every N minutes, since
                                #   its digits ghost very little. Double-tap the screen for one now.
WTTR_NARROW = False             # True = wttr.in narrow mode (noon/night only, ~63 cols)
KINDLE_DIR = "/mnt/us/dashboard"
DISPLAY_TOOL = "eips"           # "eips", or "fbink" if eips -g shows nothing/garbage
REMINDER_FLASHES = 3            # flashes before a reminder shows, and again when dismissed
PHOTO_REFRESHES = 3             # full black/white flashes when a photo goes up (clears ghosting)
PREVENT_SLEEP = True            # stop the Kindle going to sleep/screensaver (re-applied every push)
SSH_KEY = "~/.ssh/kindle_ed25519"

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"   # weather (must be monospace)
UI_FONTS = {                    # everything else: to-do, reminders, messages, menu, footer
    "regular": "fonts/SF-Pro-Display-Regular.otf",
    "medium": "fonts/SF-Pro-Display-Medium.otf",
    "semibold": "fonts/SF-Pro-Display-Semibold.otf",
    "bold": "fonts/SF-Pro-Display-Bold.otf",
}
UI_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
DITHER = "bayer"                # grey dithering: "bayer" (4x4 ordered pattern, crisp and
                                # stable on e-ink) or "diffusion" (Floyd-Steinberg, smoother photos)
ANTIALIAS = False               # False = pure black/white glyphs, crispest on e-ink
MARGIN = 20                     # px around everything
FOOTER_SIZE = 22                # px, footer line with the time
HTTP_TIMEOUT_SEC = 10

# Your own values (your city, ...) go in local_settings.py next to this file, e.g.
#     LOCATION = "Paris"
# It's left out of git, so your details stay private; anything in it replaces the defaults above.
try:
    from local_settings import *  # noqa: F401,F403
except ImportError:
    pass
import os as _os
LOCATION = _os.environ.get("KINDLE_LOCATION", LOCATION)    # (one-off override, e.g. for screenshots)
# ====================================================================
import functools
import os
import shlex
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
OUT_FILE = os.path.join(OUT_DIR, "weather.png")
VIEW_FILE = os.path.join(OUT_DIR, "view")    # classic / v2 / moon (set by show.py)
VIEWS = ("classic", "v2", "moon")


def log(msg):
    print(msg, flush=True)


DARK_FILE = os.path.join(OUT_DIR, "dark")     # exists = dark mode on (show.py dark on/off)


def dark_mode():
    return os.path.exists(DARK_FILE)


def ui_font(size, weight="regular"):
    """The UI font (SF Pro Display) at size px; DejaVu Sans if the file is missing.
    Loaded once per size/weight (re-reading the font file for every label was costly)."""
    return _ui_font(int(size), weight)


@functools.lru_cache(maxsize=256)
def _ui_font(size, weight):
    path = os.path.join(BASE_DIR, UI_FONTS.get(weight, UI_FONTS["regular"]))
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(UI_FALLBACK, size)


def ssh_options():
    """ssh/scp options for the Kindle, shared by push.sh, kiosk.sh and touch.py.
    Old Kindle dropbear builds may only speak ssh-rsa; the +ssh-rsa options just allow it."""
    return ["-i", os.path.expanduser(SSH_KEY), "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            # Reuse one connection per Kindle address: later ssh/scp calls skip the login
            # (~0.3 s each). A dead link is noticed within ~20 s and a new one is made.
            "-o", "ControlMaster=auto", "-o", "ControlPersist=300",
            "-o", "ControlPath=" + os.path.expanduser("~/.ssh/cm-kindle-%C"),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/known_hosts_kindle"),
            "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]


# ----------------------------- data ---------------------------------

def current_view():
    """classic (3-day table), v2 (?format=v2 graph) or moon (wttr.in/moon).
    Switched by show.py weather / weather2 / weathermoon."""
    try:
        with open(VIEW_FILE) as f:
            view = f.read().strip()
        return view if view in VIEWS else "classic"
    except OSError:
        return "classic"


def cache_file(view):
    return os.path.join(OUT_DIR, "weather.txt" if view == "classic" else "weather_%s.txt" % view)


def fetch_weather(view="classic"):
    if view == "moon":
        url = "https://wttr.in/moon?T"                     # moon phase, same everywhere
    else:
        opts = "format=v2&T" if view == "v2" else "nT" if WTTR_NARROW else "T"
        url = "https://wttr.in/%s?%s" % (urllib.parse.quote(LOCATION), opts)
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        text = resp.read().decode("utf-8", "replace")
    expected = "Moon" if view == "moon" else "Weather report"
    if expected not in text or len(text) < 300:
        raise ValueError("unexpected wttr.in response: %r" % text[:120])
    lines = [l.rstrip() for l in text.splitlines()
             if not l.startswith(("Follow @igor_chubin", "Location:"))]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def get_weather():
    """Cached forecast lines + fetch time. Refetches after WEATHER_CACHE_MINUTES;
    on failure falls back to the stale cache. Raises only if there is no cache at all."""
    view = current_view()
    path = cache_file(view)
    try:
        fetched = os.path.getmtime(path)
        with open(path, encoding="utf-8") as f:
            cached = f.read().split("\n")
    except OSError:
        fetched, cached = 0, None
    if cached and time.time() - fetched < WEATHER_CACHE_MINUTES * 60:
        return cached, fetched
    try:
        lines = fetch_weather(view)
    except Exception as e:
        if not cached:
            raise
        log("weather fetch failed, using copy from %s: %s"
            % (datetime.fromtimestamp(fetched).strftime("%H:%M"), e))
        return cached, fetched
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(path + ".tmp", path)
    log("weather fetched from wttr.in (%s view)" % view)
    return lines, time.time()


# ---------------------------- drawing -------------------------------

def line_height(font):
    ascent, descent = font.getmetrics()
    return ascent + descent


# wttr.in's v2 view uses emoji (2 terminal columns wide) and Braille dots for the
# graph. DejaVu Sans Mono has neither, so emoji become a font symbol drawn in their
# 2 columns, and Braille characters are drawn as dots.
EMOJI = {
    0x1F326: "☂", 0x1F327: "☂", 0x2614: "☂", 0x1F302: "☂",       # rain
    0x1F329: "⚡", 0x26C8: "⚡", 0x1F32A: "⚡",                     # thunder
    0x1F328: "❄", 0x2744: "❄", 0x2603: "❄", 0x26C4: "❄",         # snow
    0x1F32B: "≡",                                                 # fog
    0x2600: "☀", 0x1F31E: "☀",                                    # sun
    0x1F324: "☀☁", 0x26C5: "☀☁", 0x1F325: "☁☀",                 # part cloud
    0x2601: "☁",                                                  # cloud
    0x2728: "✦", 0x1F319: "☾",                                    # clear night
    0x1F311: "●", 0x1F312: "◐", 0x1F313: "◐", 0x1F314: "◐",       # moon phases
    0x1F315: "○", 0x1F316: "◑", 0x1F317: "◑", 0x1F318: "◑",
}
BRAILLE = 0x2800
WIDE_BY_DEFAULT = {0x2728, 0x2614, 0x26A1, 0x26C4, 0x26C5, 0x26C8}   # emoji even without U+FE0F


def to_cells(line):
    """Split a line into terminal cells. Each cell is a str (drawn with the font),
    "" (second half of a wide emoji), or an int (Braille dot bits)."""
    cells = []
    chars = list(line)
    for i, ch in enumerate(chars):
        cp = ord(ch)
        if cp == 0xFE0F:                                  # emoji presentation selector
            continue
        if BRAILLE <= cp <= 0x28FF:
            cells.append(cp - BRAILLE)
            continue
        wide = (cp >= 0x1F000 or cp in WIDE_BY_DEFAULT
                or (i + 1 < len(chars) and chars[i + 1] == "\ufe0f"))
        if wide:
            cells += [("E", EMOJI.get(cp, "?")), ""]
        else:
            cells.append(ch)
    return cells


def fit_font(lines, box_w, box_h, path=FONT):
    """Largest monospace font size at which every line fits in box_w x box_h."""
    cols = max(len(to_cells(l)) for l in lines)
    best = None
    for size in range(8, 120):
        font = ImageFont.truetype(path, size)
        if (font.getlength("M" * cols) > box_w
                or line_height(font) * len(lines) > box_h):
            break
        best = font
    return best or ImageFont.truetype(path, 8)


def draw_text_panel(draw, box, lines):
    """Monospace text block, as large as fits, centred in box=(x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    font = fit_font(lines, x1 - x0, y1 - y0)
    lh = line_height(font)
    cw = font.getlength("M")
    rows = [to_cells(l) for l in lines]
    block_w = cw * max(len(r) for r in rows)
    x = x0 + (x1 - x0 - block_w) // 2
    y = y0 + (y1 - y0 - lh * len(lines)) // 2
    dot = max(1.0, cw * 0.16)
    for cells in rows:
        plain = "".join(c if isinstance(c, str) and c else " " for c in cells)
        draw.text((x, y), plain, font=font, fill=0)
        for col, c in enumerate(cells):
            cx = x + col * cw
            if isinstance(c, int):                        # Braille: 2x4 dot grid
                for bit, (dc, dr) in enumerate(((0, 0), (0, 1), (0, 2), (1, 0),
                                                (1, 1), (1, 2), (0, 3), (1, 3))):
                    if c >> bit & 1:
                        px = cx + cw * (0.28 + 0.44 * dc)
                        py = y + lh * (0.125 + 0.25 * dr)
                        draw.ellipse((px - dot, py - dot, px + dot, py + dot), fill=0)
            elif isinstance(c, tuple):                    # emoji stand-in, centred in 2 cells
                sym = c[1]
                draw.text((cx + (2 * cw - font.getlength(sym)) / 2, y), sym, font=font, fill=0)
        y += lh
    return font.size


def draw_footer(draw, box, text):
    """Small right-aligned status line (plus the Kindle's battery) at the bottom of box."""
    import kindle                                  # (kindle imports this module)
    x0, y0, x1, y1 = box
    font = ui_font(FOOTER_SIZE + 4)
    draw.line((x0, y0, x1, y0), fill=0, width=2)
    right = kindle.draw_battery(draw, x1, y0 + 6, height=FOOTER_SIZE + 4)
    if right < x1:
        right -= 30
    draw.text((right - font.getlength(text), y0 + 6), text, font=font, fill=0)


def compose(weather_lines, fetched):
    """Lay out panels on the canvas. Add more panels here."""
    w, h = (HEIGHT, WIDTH) if ROTATE else (WIDTH, HEIGHT)
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    if not ANTIALIAS:
        draw.fontmode = "1"

    footer_h = FOOTER_SIZE + 14
    content = (MARGIN, MARGIN, w - MARGIN, h - MARGIN - footer_h)
    footer = (MARGIN, h - MARGIN - footer_h, w - MARGIN, h - MARGIN)

    size = draw_text_panel(draw, content, weather_lines)
    draw_footer(draw, footer, "%s  ·  weather %s  ·  %s"
                % (LOCATION, datetime.fromtimestamp(fetched).strftime("%H:%M"),
                   datetime.now().strftime("%a %d %b %H:%M")))

    if dark_mode():
        from PIL import ImageOps
        img = ImageOps.invert(img)
    if ROTATE:
        img = img.rotate(ROTATE_DEGREES, expand=True)
    return img, size


def save(img, path=OUT_FILE, fast=False):
    """Always light compression: optimize=True took ~0.2 s per screen (most of a redraw)
    to save a few KB, which the USB/Wi-Fi link doesn't notice. (fast is kept for callers.)"""
    assert img.size == (WIDTH, HEIGHT), img.size
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())   # unique per writer
    img.save(tmp, "PNG", compress_level=1)
    os.replace(tmp, path)


ACTIVE_FILE = os.path.join(OUT_DIR, ".active")
IDLE_MINUTES = 10           # no taps or commands for this long = idle (background work slows)


def mark_active():
    """A tap or a command just happened."""
    try:
        with open(ACTIVE_FILE, "a"):
            pass
        os.utime(ACTIVE_FILE)
    except OSError:
        pass


def is_active():
    try:
        return time.time() - os.path.getmtime(ACTIVE_FILE) < IDLE_MINUTES * 60
    except OSError:
        return False


# ------------------------------ main --------------------------------

def main():
    if "--print-config" in sys.argv:
        for k in ("KINDLE_IP", "PI_USB_IP", "KINDLE_DIR", "DISPLAY_TOOL",
                  "FULL_REFRESH_EVERY", "PREVENT_SLEEP",
                  "PHOTO_REFRESHES", "REMINDER_FLASHES"):
            v = globals()[k]
            if k == "FULL_REFRESH_EVERY":
                import show                        # (show imports this module)
                if show.current_screen() in ("clock", "home"):
                    v = CLOCK_FULL_REFRESH_EVERY
                elif dark_mode():
                    v = DARK_FULL_REFRESH_EVERY
            print("%s=%s" % (k, v))
        print("SSH_KEY=%s" % os.path.expanduser(SSH_KEY))
        print("SSH_OPTS=%s" % shlex.quote(" ".join(ssh_options())))
        return 0
    if "--timer" in sys.argv:
        minutes = "*" if REFRESH_MINUTES == 1 else "0/%d" % REFRESH_MINUTES
        print(TIMER % (REFRESH_MINUTES, minutes), end="")
        return 0

    import show                                    # (show imports this module)
    show.refresh_screen()
    return 0


def render_weather():
    """Fetch (cached) and draw the weather view to out/weather.png."""
    try:
        lines, fetched = get_weather()
    except Exception as e:
        log("weather fetch failed, keeping previous image: %s" % e)
        return
    try:
        img, size = compose(lines, fetched)
        save(img)
        log("rendered %s (%dx%d, %d lines, font %dpx)"
            % (OUT_FILE, img.size[0], img.size[1], len(lines), size))
    except Exception as e:
        log("render failed, keeping previous image: %s" % e)


TIMER = """[Unit]
Description=Refresh Kindle dashboard every %d minute(s)

[Timer]
# At the start of each minute, so the clock screen is never behind.
OnCalendar=*-*-* *:%s:00
AccuracySec=1s

[Install]
WantedBy=timers.target
"""

if __name__ == "__main__":
    sys.exit(main())
