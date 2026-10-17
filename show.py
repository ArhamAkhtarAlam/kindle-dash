#!/usr/bin/env python3
"""Show your own image or text on the Kindle instead of the weather.

  show.py image PATH  [--minutes N]    any image Pillow opens (jpg, png, gif, webp, ...)
  show.py text "words" [--minutes N]   big text, wrapped to fit
  show.py text --file notes.txt        text from a file
  show.py text --stdin                 text from standard input
  show.py weather   (or wttr)          back to the weather dashboard (3-day table)
  show.py weather2  (or wttr2)         weather in wttr.in's v2 view (graph)
  show.py weathermoon (or wttrmoon)    moon phase from wttr.in/moon
  show.py todo                         show the to-do list (tap a row to tick it off)
  show.py todo add "buy milk"          add a to-do
  show.py todo remove 2|"Buy milk"     remove by number or exact text (any case)
  show.py todo done 2|"milk"           tick/untick from the command line
  show.py todo clear                   remove all ticked to-dos
  show.py todo list                    print the list
  show.py reminders add "text" 9:00am [Mon,Wed|weekdays|weekends|daily]
                                       once (no days) or repeating; flashes, then white on black
  show.py reminders [list]             list reminders
  show.py reminders remove 2|"text"    remove by number or exact text
  show.py reminders dismiss            dismiss the reminder on screen (or tap the Kindle)
  show.py reminders test ["text"]      show a reminder right now
  show.py slides                       slideshow (swipe left/right on the Kindle)
  show.py slides add PATH [fit|fill]   add an image (fit = whole picture, fill = cropped to fill)
  show.py slides list | show NAME | remove NAME | next | prev
  show.py clock                        big clock, date, forecast line, next reminder
  show.py feeds                        RSS/Atom feeds (swipe left/right on the Kindle)
  show.py feeds add URL ["Name"] [headlines|image]
  show.py feeds list | show NAME | remove NAME | next | prev | refresh
  show.py xkcd                         the xkcd feed
  show.py games [ttt|c4|reversi|mines] [pi|2p] [easy|normal|hard]   touch games
  show.py spotify [setup|status|play|pause|next|prev|volume N]   now playing + controls
  show.py draw [clear|save]            drawing pad (draw with a finger on the Kindle)
  show.py calendar                     month + 7-day agenda (swipe left/right for other weeks)
  show.py calendar add ICAL_URL ["Name"] | list | remove NAME | next | prev | today | refresh
  show.py brightness [40%|off|max|up|down]   Kindle front light
  show.py wifi [on|off]                Kindle Wi-Fi (the Pi uses it when USB is unplugged)
  show.py timer [25m|pause|resume|reset|add 5m]   countdown with on-screen buttons
  show.py dark [on|off|toggle]         dark mode: screens inverted (photos/slides left alone)
  show.py stats                        Pi stats: CPU, temperature, memory, disk, network, services
  show.py hackers                      the honeypot's password of the day and today's attacks
  show.py speedtest [down|up]          internet speed test (also buttons on the Pi stats screen)
  show.py home                         Home: clock, weather, what's coming up, to-dos, the Pi's vitals
  show.py status                       what is showing now (+ Kindle battery)
  show.py health                       battery/connection as JSON (for the Mac watcher)
  show.py --current                    (used by push.sh) image path to push

--minutes N switches back to Home after N minutes; without it, your
image/text stays until you switch screens. Add --no-push to only
prepare the image without sending it to the Kindle.
"""

import json
from datetime import datetime, timedelta
import os
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageOps

import cal
import clock
import health
import home
import timer
import draw
import games
import hackers
import kindle
import mandel
import menu
import wmap
import render
import reminders
import slides
import spotify
import feeds
import todo

CUSTOM_FILE = os.path.join(render.OUT_DIR, "custom.png")
STATE_FILE = os.path.join(render.OUT_DIR, "custom.json")
TEXT_MARGIN = 60
GRAY_LEVELS = 16        # Kindle PW3 panel: 16 shades of gray


def canvas_size():
    """Same orientation as the weather: landscape if ROTATE, portrait otherwise."""
    return (render.HEIGHT, render.WIDTH) if render.ROTATE else (render.WIDTH, render.HEIGHT)


def finish(img, path=CUSTOM_FILE, photo=False, keep=()):
    """Rotate like the weather image and save it (by default as the custom screen).
    In dark mode the screen is inverted, except photos (photo=True) and any `keep` boxes
    (canvas coordinates, e.g. Spotify's album art)."""
    if render.dark_mode() and not photo:
        img = invert(img, keep)
    if render.ROTATE:
        img = img.rotate(render.ROTATE_DEGREES, expand=True)
    render.save(img, path)


# ------------------------------ image -------------------------------

def invert(img, keep=()):
    out = ImageOps.invert(img.convert("L"))
    for box in keep:
        box = tuple(int(v) for v in box)
        out.paste(img.convert("L").crop(box), box[:2])
    return out


def to_16_grays(img):
    """Dither to the panel's 16 gray levels so photos don't band (render.DITHER picks how)."""
    if render.DITHER == "bayer":
        import dither
        return dither.levels(img, GRAY_LEVELS)
    levels = [round(i * 255 / (GRAY_LEVELS - 1)) for i in range(GRAY_LEVELS)]
    pal = Image.new("P", (1, 1))
    pal.putpalette([v for v in levels for _ in range(3)] + [0] * (768 - 3 * GRAY_LEVELS))
    return img.convert("RGB").quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG).convert("L")


def make_image(path, mode="fit", label=None):
    """fit = whole picture, white around it; fill = cover the screen, cropping the edges.
    label (e.g. "3 / 7") is drawn small in the bottom-right corner."""
    src = Image.open(path)
    src.seek(0)                                   # first frame of a GIF
    src = ImageOps.exif_transpose(src)            # respect phone photo rotation
    if src.mode in ("RGBA", "LA", "P"):
        src = src.convert("RGBA")
        bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
        src = Image.alpha_composite(bg, src)      # transparent -> white
    src = src.convert("L")
    w, h = canvas_size()
    if mode == "fill":
        img = ImageOps.fit(src, (w, h), Image.Resampling.LANCZOS)
    else:
        fitted = ImageOps.contain(src, (w, h), Image.Resampling.LANCZOS)
        img = Image.new("L", (w, h), 255)
        img.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
    img = to_16_grays(img)
    if label:
        d = ImageDraw.Draw(img)
        font = render.ui_font(28, "semibold")
        tw = d.textlength(label, font=font)
        x1, y1 = w - 24, h - 24
        d.rounded_rectangle((x1 - tw - 36, y1 - 50, x1, y1), radius=25, fill=255, outline=0, width=2)
        d.text((x1 - tw - 18, y1 - 43), label, font=font, fill=0)
    finish(img, photo=True)


# ------------------------------- text -------------------------------

def wrap(text, font, max_w):
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            cand = word if not line else line + " " + word
            if font.getlength(cand) <= max_w:
                line = cand
                continue
            if line:
                lines.append(line)
            while font.getlength(word) > max_w:          # break very long words
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut]) > max_w:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def make_text(text):
    text = text.replace("\r\n", "\n").replace("\t", "    ").strip("\n")
    if not text.strip():
        raise SystemExit("no text given")
    w, h = canvas_size()
    box_w, box_h = w - 2 * TEXT_MARGIN, h - 2 * TEXT_MARGIN
    best = None
    lo, hi = 10, 400                               # binary search: largest size that fits
    while lo <= hi:
        size = (lo + hi) // 2
        font = render.ui_font(size, "medium")
        lines = wrap(text, font, box_w)
        lh = int(size * 1.25)
        if lh * len(lines) <= box_h:
            best, lo = (font, lines, lh), size + 1
        else:
            hi = size - 1
    if best is None:
        raise SystemExit("text is too long to fit on the screen")
    font, lines, lh = best
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    if not render.ANTIALIAS and font.size < 40:
        draw.fontmode = "1"
    center = len(lines) <= 4                       # short messages centred, notes left-aligned
    y = (h - lh * len(lines)) // 2
    for line in lines:
        x = (w - font.getlength(line)) // 2 if center else TEXT_MARGIN
        draw.text((x, y), line, font=font, fill=0)
        y += lh
    finish(img)
    return font.size, len(lines)


# ------------------------------ state -------------------------------

def read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_custom():
    for p in (STATE_FILE, CUSTOM_FILE):
        try:
            os.remove(p)
        except OSError:
            pass


def current_image():
    if reminders.alert_active():                   # a reminder covers every other screen
        return reminders.ALERT_IMAGE
    if menu.is_open():                             # then the pull-down menu
        return menu.MENU_IMAGE
    st = read_state()
    if st and os.path.exists(CUSTOM_FILE):
        if st.get("until") is None or time.time() < st["until"]:
            return CUSTOM_FILE
        clear_custom()                             # expired: back to Home, the default screen
        set_screen("home")
        refresh_screen(force=True, screen="home")
        return CUSTOM_FILE
    return render.OUT_FILE


def push_now(photo=False, quick=False, flash=False):
    """Send the current screen. quick = partial refresh, no flash (taps, Spotify, games...),
    done directly from here for speed; flash = several full flashes (reminder shown/dismissed)."""
    if quick:
        return fast_push()
    if not photo and not flash:                    # screen switches: one clean flash, done here
        return fast_push(full=True)                # (push.sh is ~1.2 s slower: it starts Python 5x)
    script = os.path.join(render.BASE_DIR, "push.sh")
    mode = "--alert" if flash else "--photo" if photo else "--now"
    return subprocess.call([script, mode], stdin=subprocess.DEVNULL)   # it never reads input


def push_spotify_progress(s):
    w, _ = canvas_size()
    return push_region(spotify.progress_strip(w, s), spotify.progress_box(w))


def push_region(strip, box, wave=None):
    """Redraw only one strip of the screen and refresh only that patch of the Kindle (FBInk's
    fast DU waveform; a cleaner GC16 every CLEAN_EVERY pushes). Also patches the saved screen
    image so it matches what's shown. Falls back to a normal quick push if that fails."""
    import fcntl
    import hashlib
    import io
    w, h = canvas_size()
    if render.dark_mode():
        strip = ImageOps.invert(strip.convert("L"))
    bx0, by0, bx1, by1 = box
    if not render.ROTATE:
        part, left, top = strip, bx0, by0
    elif render.ROTATE_DEGREES == 90:               # canvas (x, y) -> panel (y, w-1-x)
        part, left, top = strip.rotate(90, expand=True), by0, w - bx1
    else:                                           # 270: canvas (x, y) -> panel (h-1-y, x)
        part, left, top = strip.rotate(270, expand=True), h - by1, bx0
    ip, _ = kindle.address()
    if not ip:
        return 0
    push_region.count = getattr(push_region, "count", 0) + 1
    if push_region.count % (render.DARK_CLEAN_EVERY if render.dark_mode() else CLEAN_EVERY) == 0:
        wave = "GC16"                               # a clean refresh now and then (less ghosting)
    elif wave is None:
        wave = "DU"                                 # fast black/white (progress bars, digits)
    part = part.convert("L")
    full_screen = part.size == (render.WIDTH, render.HEIGHT)
    buf = io.BytesIO()
    part.save(buf, "PNG", compress_level=1)
    cmd = ("cat > /tmp/kd-region.png && /mnt/us/libkh/bin/fbink -q -W %s "
           "-g file=/tmp/kd-region.png,x=%d,y=%d" % (wave, left, top))
    with open(os.path.join(render.OUT_DIR, ".push.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:                                           # keep the saved screen in step
            if full_screen:
                full = part                            # (the whole screen: nothing to patch)
            else:
                full = Image.open(CUSTOM_FILE).convert("L")
                full.paste(part, (left, top))
            render.save(full, CUSTOM_FILE, fast=True)
        except OSError:
            pass
        try:
            r = subprocess.run(["ssh"] + render.ssh_options() + ["root@" + ip, cmd],
                               input=buf.getvalue(), capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            return 1
        if r.returncode == 0:
            with open(CUSTOM_FILE, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()
            with open(os.path.join(render.OUT_DIR, ".last_push"), "w") as f:
                f.write("%s %d\n" % (digest, time.time()))
            return 0
    render.log("strip-only push failed (%s), sending the whole screen"
               % r.stderr.decode(errors="replace").strip()[:120])
    return fast_push()


CLEAN_EVERY = 30        # every Nth progress update uses a clean (GC16) refresh of that strip


def _count(name):
    """A push counter kept in out/ (shared by touch.py and the commands). Returns the new value."""
    path = os.path.join(render.OUT_DIR, ".%s_count" % name)
    try:
        with open(path) as f:
            n = int(f.read().strip() or 0) + 1
    except (OSError, ValueError):
        n = 1
    with open(path, "w") as f:
        f.write(str(n))
    return n


def fast_push(full=False):
    """Quick partial refresh in one ssh command over the shared connection, skipping push.sh
    (its Python start-ups and separate copy/display steps). Same lock and records as push.sh.
    full=True: one flashing full refresh (a screen switch), which clears ghosting."""
    import fcntl
    import hashlib
    ip, _ = kindle.address()
    if not ip:
        render.log("Kindle not reachable (USB or Wi-Fi), skipping push")
        return 0
    img = current_image()
    remote = render.KINDLE_DIR + "/screen.png"
    clean = full or (render.dark_mode() and _count("quick") % render.DARK_QUICK_FULL_EVERY == 0)
    if render.DISPLAY_TOOL == "fbink":                  # (clean: a full flash, wipes dark-mode ghosting)
        draw_cmd = "/mnt/us/libkh/bin/fbink -q %s-g file='%s'" % ("-c -f " if clean else "", remote)
    else:
        draw_cmd = ("eips -c && eips -f -g '%s'" if clean else "eips -g '%s'") % remote
    awake = "lipc-set-prop com.lab126.powerd preventScreenSaver %d 2>/dev/null; " % (1 if render.PREVENT_SLEEP else 0)
    cmd = ("export PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH; mkdir -p '%s' && cat > '%s.tmp' && "
           "mv '%s.tmp' '%s' && { %s%s; } >/dev/null" % (render.KINDLE_DIR, remote, remote, remote, awake, draw_cmd))
    with open(os.path.join(render.OUT_DIR, ".push.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)                    # never at the same time as push.sh
        with open(img, "rb") as f:
            data = f.read()
        try:
            r = subprocess.run(["ssh"] + render.ssh_options() + ["root@" + ip, cmd],
                               input=data, capture_output=True, timeout=30)   # (input = the image)
        except subprocess.TimeoutExpired:
            render.log("quick push timed out")
            return 1
        if r.returncode != 0:
            render.log("quick push failed: %s" % r.stderr.decode(errors="replace").strip()[:200])
            return r.returncode
        with open(os.path.join(render.OUT_DIR, ".last_push"), "w") as f:   # the timer won't resend it
            f.write("%s %d\n" % (hashlib.sha1(data).hexdigest(), time.time()))
    return 0


def dismiss_reminder(push=True):
    """Clear the reminder on screen and flash back to whatever was showing before."""
    with reminders.locked():
        if not reminders.dismiss():
            return False
    render.log("reminder dismissed")
    refresh_screen(force=True)                     # e.g. the clock moved on meanwhile
    if push:
        push_now(flash=True)
    return True


def reminders_command(rest, no_push):
    sub = rest[0] if rest else "list"
    args = rest[1:]
    if sub == "list":
        print(reminders.format_list(reminders.load()))
        return 0
    if sub == "dismiss":
        print("dismissed" if dismiss_reminder(push=not no_push) else "no reminder is showing")
        return 0
    if sub == "test":
        with reminders.locked():
            reminders.raise_alert([" ".join(args) or "This is a test reminder"])
        return 0 if no_push else push_now(flash=True)
    if sub == "add":
        days = None
        if len(args) >= 3 and reminders.parse_days(args[-1]) is not None:
            days = reminders.parse_days(args.pop())
        when = args.pop() if len(args) >= 2 else ""
        hhmm = reminders.parse_time(when)
        text = " ".join(args).strip()
        if not text:
            raise SystemExit('usage: reminders add "text" 9:00am [Mon,Wed|weekdays|weekends|daily]')
        if not hhmm:
            raise SystemExit("can't understand the time %r (try 9:00am, 9pm or 21:30)" % when)
        with reminders.locked():
            items = reminders.load()
            r = reminders.new_reminder(text, hhmm, days)
            items.append(r)
            reminders.save(items)
        print("added: %s" % reminders.describe(r))
        print(reminders.format_list(items))
        return 0
    if sub in ("remove", "rm"):
        with reminders.locked():
            items = reminders.load()
            try:
                i = todo.find([{"text": r["text"]} for r in items], " ".join(args),
                              verb="remove", noun="reminders")
            except ValueError as e:
                raise SystemExit(str(e))
            print("removed: %s" % reminders.describe(items.pop(i)))
            reminders.save(items)
        print(reminders.format_list(items))
        return 0
    raise SystemExit("unknown reminders command %r (try: add, remove, list, dismiss, test)" % sub)


def todo_showing():
    st = read_state()
    return bool(st) and st.get("kind") == "todo" and current_image() == CUSTOM_FILE


def show_todo_screen():
    """Redraw the to-do screen from out/todos.json and make it the current screen."""
    w, h = canvas_size()
    img, rows = todo.draw_screen(todo.load(), w, h)
    finish(img)
    todo.save_layout(w, h, rows)
    with open(STATE_FILE, "w") as f:
        json.dump({"kind": "todo", "what": "to-do list", "until": None}, f)


def todo_command(rest, no_push):
    sub = rest[0] if rest else "show"
    arg = " ".join(rest[1:]).strip()
    with todo.locked():
        items = todo.load()
        if sub == "show":
            show_todo_screen()
            print(todo.format_list(items))
            return 0 if no_push else push_now()
        if sub == "list":
            print(todo.format_list(items))
            return 0
        if sub == "add":
            if not arg:
                raise SystemExit('usage: todo add "something"')
            items.append({"text": arg, "done": False})
            print("added: %s" % arg)
        elif sub in ("remove", "rm", "done", "undone"):
            if not arg:
                raise SystemExit('usage: todo %s NUMBER|"text"' % sub)
            try:
                i = todo.find(items, arg, verb=sub)
            except ValueError as e:
                raise SystemExit(str(e))
            if sub in ("remove", "rm"):
                print("removed: %s" % items.pop(i)["text"])
            else:
                # "done" toggles, so the same command can untick; "undone" always unticks
                items[i]["done"] = not items[i]["done"] if sub == "done" else False
                print("%s: %s" % ("ticked" if items[i]["done"] else "unticked", items[i]["text"]))
        elif sub == "clear":
            n = sum(1 for it in items if it["done"])
            items = [it for it in items if not it["done"]]
            print("cleared %d ticked to-do(s)" % n)
        else:
            raise SystemExit("unknown todo command %r (try: add, remove, done, clear, list)" % sub)
        todo.save(items)
        print(todo.format_list(items))
        if todo_showing():
            show_todo_screen()
            return 0 if no_push else push_now(quick=True)
    return 0


WEATHER_COMMANDS = {"weather": "classic", "wttr": "classic",
                    "weather2": "v2", "wttr2": "v2",
                    "weathermoon": "moon", "wttrmoon": "moon"}


def current_screen():
    """Which of SCREENS is showing, or None (an image/text from the Mac)."""
    if todo_showing():
        return "todo"
    st = read_state()
    if st and st.get("kind") in ("slides", "clock", "feeds", "calendar", "games", "spotify", "draw",
                                 "timer", "stats", "mandel", "wmap", "home", "hackers") \
            and current_image() == CUSTOM_FILE:
        return st["kind"]
    if st:
        return None
    return {"classic": "wttr", "v2": "wttr2", "moon": "wttrmoon"}[render.current_view()]


def switch_screen(name, push=True):
    """Show a weather view or the to-do list (used by the commands and the menu)."""
    menu.close()
    if name == "todo":
        with todo.locked():
            show_todo_screen()
        return push_now() if push else 0
    if name == "feeds":
        if not feeds.load()["items"]:
            make_text("No feeds yet\n\nkindle feeds add https://example.com/rss.xml")
            set_screen("text", "no feeds yet")
            return push_now() if push else 0
        return show_feed(detail=None, push="now" if push else None)
    if name in ("games", "spotify", "draw", "timer", "stats", "hackers"):
        set_screen(name)
        refresh_screen(force=True, screen=name)
        return push_now() if push else 0
    if name == "calendar":
        set_screen("calendar")
        refresh_screen(force=True, screen="calendar")
        return push_now() if push else 0
    if name == "home":
        set_screen("home")
        refresh_screen(force=True, screen="home")
        return push_now() if push else 0
    if name == "wmap":
        wmap.stop()                                # (a Play left on by a restart)
        set_screen("wmap")
        refresh_screen(force=True, screen="wmap")
        return push_now() if push else 0
    if name == "mandel":
        set_screen("mandel")
        refresh_screen(force=True, screen="mandel")
        return push_now() if push else 0
    if name == "clock":
        set_screen(name)
        refresh_screen(force=True, screen=name)    # draws it (it may not exist yet)
        return push_now() if push else 0
    if name == "slides":
        if not slides.load()["items"]:             # from the menu: explain instead of failing
            make_text("No slides yet\n\nkindle slides add ~/Pictures/photo.jpg")
            with open(STATE_FILE, "w") as f:
                json.dump({"kind": "text", "what": "no slides yet", "until": None}, f)
            return push_now() if push else 0
        return show_slide(None, push="photo" if push else None)
    clear_custom()
    view = WEATHER_COMMANDS[name]
    os.makedirs(render.OUT_DIR, exist_ok=True)
    with open(render.VIEW_FILE, "w") as f:
        f.write(view)
    print("back to the weather (%s view)" % view)
    render.render_weather()
    return push_now() if push else 0


def set_screen(kind, what=None):
    with open(STATE_FILE, "w") as f:
        json.dump({"kind": kind, "what": what or kind, "until": None}, f)


def refresh_screen(force=False, screen=None):
    """Redraw the current screen if it changes over time: the weather views (footer clock),
    the clock, the to-do footer, and a feed when it has new items. Called every minute by
    render.py; skipped while a reminder or the menu is covering the screen."""
    if not force and (reminders.alert_active() or menu.is_open()):
        return
    screen = screen or current_screen()
    w, h = canvas_size()
    if screen in ("wttr", "wttr2", "wttrmoon"):
        render.render_weather()
    elif screen == "clock":
        finish(clock.draw(w, h))
    elif screen == "home":                          # every minute: the clock and the Pi's numbers
        finish(home.draw(w, h))
    elif screen == "hackers":                       # new attacks show up every few minutes
        if force or time.localtime().tm_min % hackers.REDRAW_MINUTES == 0:
            finish(hackers.draw(w, h))
    elif screen == "games" and force:
        img, targets = games.draw(w, h, games.load())
        finish(img)
        games.save_layout(targets)
    elif screen == "stats":
        live = os.path.join(render.OUT_DIR, ".live_stats")
        if force or not (os.path.exists(live) and time.time() - os.path.getmtime(live) < 30):
            finish(health.draw(w, h))                # (touch.py redraws it while it's running)
    elif screen == "timer":
        live = os.path.join(render.OUT_DIR, ".live_timer")
        if force or not (os.path.exists(live) and time.time() - os.path.getmtime(live) < 15):
            img, targets = timer.draw(w, h)
            finish(img)
            timer.save_layout(targets)
    elif screen == "wmap":                          # live: new data hourly, and the hour moving on
        st = wmap.load()
        if not force and wmap.stale(st["area"]):
            try:
                wmap.fetch(st["area"], w - wmap.PANEL_W, h - wmap.BAR_H)
            except Exception as e:
                render.log("weather map: fetch failed, keeping the old data: %s" % e)
        if force or wmap.needs_redraw():
            finish(wmap.draw(w, h))
    elif screen == "mandel" and force:              # (only when you move: never on the minute timer)
        finish(mandel.draw(w, h))
    elif screen == "draw" and force:
        img, buttons = draw.draw(w, h, draw.load())
        finish(img)
        draw.save_layout(buttons, h)
    elif screen == "spotify":
        live = os.path.join(render.OUT_DIR, ".live_spotify")
        if not force and os.path.exists(live) and time.time() - os.path.getmtime(live) < 15:
            return                                  # touch.py is keeping it up to date
        try:
            s, err = (spotify.current(), None) if spotify.configured() else \
                (None, "Spotify isn't set up yet (kindle spotify setup)")
        except Exception as e:
            s, err = None, "Spotify: %s" % e
        img, buttons = spotify.draw(w, h, s, err)
        finish(img, keep=[spotify.art_box(w, h)] if s and s.get("art") else ())
        spotify.save_layout(buttons, s)
    elif screen == "calendar":
        changed = cal.refresh_all()               # re-fetched every 15 min
        if changed or force or time.localtime().tm_min % 15 == 0:   # also moves "now" along
            finish(cal.draw(w, h))
    elif screen == "feeds":
        data = feeds.load()
        if data["items"]:
            items, changed = feeds.fetch(data["items"][data["index"]])
            if changed or force:
                draw_feed(data, items)
    elif screen == "todo" and force:
        with todo.locked():
            show_todo_screen()
    # photos, text and slides never change on their own: nothing to redraw


def show_feed(step=None, index=None, detail="keep", push="now", force=False):
    """Show a feed: the current one, step +1/-1, or a given index; detail = article index
    to open, None to go back to the list, "keep" to leave as is.
    push: "now" (one flash), "quick" (partial, no flash) or None."""
    data = feeds.load()
    if not data["items"]:
        raise SystemExit('no feeds yet: kindle feeds add https://example.com/rss.xml')
    if index is not None:
        data["index"] = index
    elif step:
        data["index"] = (data["index"] + step) % len(data["items"])
    if step or index is not None:
        data["detail"] = None
    if detail != "keep":
        data["detail"] = detail
    feeds.save(data)
    feed = data["items"][data["index"]]
    items, _ = feeds.fetch(feed, force=force)
    draw_feed(data, items)
    print("feed %d/%d: %s" % (data["index"] + 1, len(data["items"]), feed["name"]))
    if push == "now":
        return push_now()
    if push == "quick":
        return push_now(quick=True)
    return 0


def draw_feed(data, items):
    w, h = canvas_size()
    img, rows = feeds.draw(w, h, data, items)
    finish(img)
    feeds.save_layout(rows)
    set_screen("feeds", data["items"][data["index"]]["name"])


def feeds_command(rest, no_push):
    sub = rest[0] if rest else "show"
    args = rest[1:]
    push = None if no_push else "now"
    data = feeds.load()
    if sub == "list":
        print(feeds.format_list(data))
        return 0
    if sub == "add":
        style = None
        if args and args[-1].lower() in feeds.STYLES:
            style = args.pop().lower()
        if not args or not args[0].startswith(("http://", "https://")):
            raise SystemExit('usage: feeds add URL ["Name"] [headlines|image]')
        url, name = args[0], " ".join(args[1:]).strip()
        try:
            title, items = feeds.parse(feeds._get(url))
        except feeds.ET.ParseError:
            raise SystemExit("%s isn't an RSS/Atom feed (it looks like a web page). Look for "
                             "an RSS link on the site, often ending in /rss, /feed or .xml" % url)
        except Exception as e:
            raise SystemExit("couldn't fetch %s: %s" % (url, e))
        name = name or title or url
        if name.casefold() in {f["name"].casefold() for f in data["items"]}:
            raise SystemExit("there's already a feed called %r" % name)
        data["items"].append({"name": name, "url": url, "style": style or feeds.guess_style(items)})
        feeds.save(data)
        print("added feed: %s (%s, %d items)" % (name, data["items"][-1]["style"], len(items)))
        print(feeds.format_list(data))
        return 0
    if sub in ("show", "remove", "rm") and args:
        try:
            i = todo.find([{"text": f["name"]} for f in data["items"]], " ".join(args),
                          verb="show" if sub == "show" else "remove", noun="feeds")
        except ValueError as e:
            raise SystemExit(str(e))
        if sub == "show":
            return show_feed(index=i, push=push)
        removed = data["items"].pop(i)
        data["index"] = min(data["index"], max(0, len(data["items"]) - 1))
        data["detail"] = None
        feeds.save(data)
        print("removed feed: %s" % removed["name"])
        print(feeds.format_list(data))
        if current_screen() == "feeds":
            return show_feed(push=push) if data["items"] else switch_screen("home", push=not no_push)
        return 0
    if sub in ("show", "next", "prev", "previous", "refresh"):
        step = {"next": 1, "prev": -1, "previous": -1}.get(sub)
        return show_feed(step, detail=None, push=push, force=(sub == "refresh"))
    raise SystemExit("unknown feeds command %r (try: add, list, show, remove, next, prev, refresh)" % sub)


def calendar_command(rest, no_push):
    sub = rest[0] if rest else "show"
    args = rest[1:]
    data = cal.load()
    if sub == "list":
        print(cal.format_list(data))
        return 0
    if sub == "add":
        if not args:
            raise SystemExit('usage: calendar add ICAL_URL ["Name"]')
        url, name = cal.normalise(args[0]), " ".join(args[1:]).strip() or "Calendar %d" % (len(data["items"]) + 1)
        if not url.startswith(("http://", "https://")):
            raise SystemExit("that doesn't look like a link: use the calendar's iCal (.ics) address")
        if name.casefold() in {c["name"].casefold() for c in data["items"]}:
            raise SystemExit("there's already a calendar called %r" % name)
        try:
            raw, _ = cal.fetch(url, force=True)
        except Exception as e:
            raise SystemExit("couldn't read that as an iCal (.ics) calendar: %s" % e)
        data["items"].append({"name": name, "url": url})
        cal.save(data)
        now = datetime.now()
        n = len(cal.events(now, now + timedelta(days=7)))
        print("added calendar: %s (%d events in the next 7 days)" % (name, n))
        print(cal.format_list(data))
        return 0 if (no_push or current_screen() != "calendar") else switch_screen("calendar")
    if sub in ("remove", "rm"):
        try:
            i = todo.find([{"text": c["name"]} for c in data["items"]], " ".join(args),
                          verb="remove", noun="calendar")
        except ValueError as e:
            raise SystemExit(str(e))
        removed = data["items"].pop(i)
        cal.save(data)
        print("removed calendar: %s" % removed["name"])
        print(cal.format_list(data))
        return 0 if (no_push or current_screen() != "calendar") else switch_screen("calendar")
    if sub in ("show", "today", "next", "prev", "previous", "refresh"):
        data["offset"] = {"today": 0, "show": 0, "refresh": data["offset"]}.get(
            sub, data["offset"] + (1 if sub == "next" else -1))
        cal.save(data)
        if sub == "refresh":
            cal.refresh_all(force=True)
        return switch_screen("calendar", push=not no_push)
    raise SystemExit("unknown calendar command %r (try: add, remove, list, next, prev, today, refresh)" % sub)


def spotify_command(rest, no_push):
    sub = rest[0] if rest else "show"
    if sub == "show":
        return switch_screen("spotify", push=not no_push)
    if sub in ("setup", "code", "status", "volume", "play", "pause", "toggle", "next", "prev", "previous"):
        code = subprocess.call([os.path.join(render.BASE_DIR, "venv/bin/python"),
                                os.path.join(render.BASE_DIR, "spotify.py")] + rest)
        if code == 0 and sub not in ("setup", "code", "status") and current_screen() == "spotify" and not no_push:
            refresh_screen(force=True, screen="spotify")
            push_now(quick=True)
        return code
    raise SystemExit("unknown spotify command %r (try: setup, status, play, pause, next, prev, volume N)" % sub)


def wifi_command(rest):
    if rest and rest[0] in ("on", "off"):
        problem = kindle.set_wifi(rest[0] == "on")
        if problem:
            raise SystemExit(problem)
    st, k = kindle.status(), kindle.health()
    print("Kindle Wi-Fi: %s%s" % ({True: "on", False: "off"}.get(st["wifi"], "unknown"),
                                  " (%s, %s)" % (st["ssid"], st["wifi_ip"]) if st["wifi"] and st["ssid"] else ""))
    print("Pi is reaching it over: %s" % {"usb": "USB", "wifi": "Wi-Fi"}.get(k["link"], "nothing right now"))
    return 0


def timer_command(rest, no_push):
    if rest:
        sub = rest[0].lower()
        if sub in ("pause", "stop"):
            timer.pause()
        elif sub in ("resume", "start", "go"):
            timer.start()
        elif sub == "reset":
            timer.reset()
        elif sub == "add" and len(rest) > 1:
            secs = timer.parse_duration(" ".join(rest[1:]))
            if not secs:
                raise SystemExit("usage: timer add 5m")
            timer.add(secs)
        else:
            secs = timer.parse_duration(" ".join(rest))
            if not secs:
                raise SystemExit("usage: timer [25m | 90s | 1h30m | 10 | pause | resume | reset | add 5m]")
            timer.set_and_start(secs)
    st = timer.load()
    print("timer: %s %s" % (timer._fmt(timer.remaining(st)),
                            "running" if timer.running(st) else "paused" if st["left"] else "not set"))
    return switch_screen("timer", push=not no_push)


def set_dark(on, push=True):
    """Dark mode on/off, then redraw what's showing. Photos and slides stay as they are."""
    if on:
        open(render.DARK_FILE, "w").close()
    elif os.path.exists(render.DARK_FILE):
        os.remove(render.DARK_FILE)
    menu.close()
    st, screen = read_state(), current_screen()
    if st and st.get("kind") == "text" and os.path.exists(CUSTOM_FILE):
        render.save(ImageOps.invert(Image.open(CUSTOM_FILE).convert("L")), CUSTOM_FILE)   # can't redraw: flip it
    elif not (st and st.get("kind") in ("image", "slides")):
        refresh_screen(force=True, screen=screen)
    return push_now() if push else 0


def dark_command(rest, no_push):
    arg = rest[0].lower() if rest else "toggle"
    if arg not in ("on", "off", "toggle", "status"):
        raise SystemExit("usage: dark [on|off|toggle|status]")
    if arg != "status":
        set_dark(not render.dark_mode() if arg == "toggle" else arg == "on", push=not no_push)
    print("dark mode: %s" % ("on" if render.dark_mode() else "off"))
    return 0


def brightness_command(rest):
    if not rest:
        pct = kindle.light_percent()
        print("front light: %s" % ("unknown (no push yet)" if pct is None else "%d%%" % pct))
        return 0
    arg = rest[0].lower().rstrip("%")
    cur = kindle.light_percent() or 0
    pct = {"off": 0, "max": 100, "full": 100, "up": cur + 10, "down": cur - 10}.get(arg)
    if pct is None:
        try:
            pct = float(arg)
        except ValueError:
            raise SystemExit("usage: brightness [0-100%|off|max|up|down]")
    step = kindle.set_light(pct)
    if step is None:
        raise SystemExit("couldn't reach the Kindle to change the light")
    print("front light: %d%% (step %d/%d)" % (round(step * 100 / kindle.LIGHT_MAX), step, kindle.LIGHT_MAX))
    return 0


def show_slide(step=None, index=None, push="photo"):
    """Show a slide: the current one, step +1/-1 from it, or a given index.
    push: "photo" (several flashes), "now" (one flash, for swipes) or None."""
    data = slides.load()
    if not data["items"]:
        raise SystemExit("no slides yet: kindle slides add ~/Pictures/photo.jpg [fit|fill]")
    n = len(data["items"])
    if index is not None:
        data["index"] = index
    elif step:
        data["index"] = (data["index"] + step) % n
    slides.save(data)
    item = data["items"][data["index"]]
    make_image(slides.path_of(item), item["mode"], "%d / %d" % (data["index"] + 1, n))
    with open(STATE_FILE, "w") as f:
        json.dump({"kind": "slides", "what": item["name"], "until": None}, f)
    print("slide %d/%d: %s" % (data["index"] + 1, n, item["name"]))
    if push == "photo":
        return push_now(photo=True)
    if push == "now":
        return push_now()
    return 0


def slides_command(rest, no_push):
    sub = rest[0] if rest else "show"
    args = rest[1:]
    push = None if no_push else "photo"
    if sub == "list":
        print(slides.format_list(slides.load()))
        return 0
    if sub == "add":
        name = arg_value(args, "--name")
        mode = "fit"
        if args and args[-1].lower() in slides.MODES:
            mode = args.pop().lower()
        if len(args) != 1:
            raise SystemExit("usage: slides add PATH [fit|fill]")
        path = args[0]
        try:
            Image.open(path).verify()
        except Exception:
            raise SystemExit("can't read %s as an image" % path)
        data = slides.load()
        name = slides.add(data, path, name or os.path.splitext(os.path.basename(path))[0], mode)
        slides.save(data)
        print("added slide: %s (%s)" % (name, mode))
        print(slides.format_list(data))
        return 0
    if sub in ("show", "remove", "rm") and args:
        data = slides.load()
        try:
            i = todo.find([{"text": it["name"]} for it in data["items"]], " ".join(args),
                          verb="show" if sub == "show" else "remove", noun="slides")
        except ValueError as e:
            raise SystemExit(str(e))
        if sub == "show":
            return show_slide(index=i, push=push)
        item = slides.remove(data, i)
        slides.save(data)
        print("removed slide: %s" % item["name"])
        print(slides.format_list(data))
        if current_screen() == "slides":           # it was on screen: show what's left
            if data["items"]:
                return show_slide(push=push)
            return switch_screen("home", push=not no_push)
        return 0
    if sub in slides.MODES:                        # fit/fill: all slides, or one by name/number
        data = slides.load()
        if not data["items"]:
            raise SystemExit("no slides yet: kindle slides add ~/Pictures/photo.jpg [fit|fill]")
        if args and args != ["all"]:
            try:
                picked = [todo.find([{"text": it["name"]} for it in data["items"]], " ".join(args),
                                    verb="change", noun="slides")]
            except ValueError as e:
                raise SystemExit(str(e))
        else:
            picked = range(len(data["items"]))
        for i in picked:
            data["items"][i]["mode"] = sub
        slides.save(data)
        print("%s: %s" % ("fill (cover the screen, edges cropped)" if sub == "fill"
                          else "fit (whole picture, white around it)",
                          data["items"][picked[0]]["name"] if len(picked) == 1 else "all %d slides" % len(picked)))
        print(slides.format_list(data))
        if current_screen() == "slides" and data["index"] in picked:
            return show_slide(push=push)           # it's on screen: redraw it
        return 0
    if sub in ("show", "next", "prev", "previous"):
        step = {"next": 1, "prev": -1, "previous": -1}.get(sub)
        return show_slide(step, push=push)
    raise SystemExit("unknown slides command %r (try: add, list, show, remove, next, prev, fit, fill)" % sub)


def arg_value(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise SystemExit("%s needs a value" % flag)
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def main():
    sys.stdout.reconfigure(line_buffering=True)   # keep our lines in order with push.sh's
    args = sys.argv[1:]
    if args == ["--current"]:
        print(current_image())
        return 0
    no_push = "--no-push" in args
    if no_push:
        args.remove("--no-push")
    minutes = arg_value(args, "--minutes")
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd, rest = args[0], args[1:]
    if cmd not in ("status", "health"):
        render.mark_active()                       # something from the Mac: we're in use
    if cmd in ("image", "text"):
        menu.close()                               # a new screen from the Mac replaces the menu
    if cmd == "status":
        st = read_state()
        if reminders.alert_active():
            print("showing a reminder (tap the Kindle or run: reminders dismiss)")
        elif current_image() == CUSTOM_FILE:
            until = st.get("until")
            print("showing %s: %s%s" % (st["kind"], st["what"],
                  "" if until is None else " (until %s)" % time.strftime("%H:%M", time.localtime(until))))
        else:
            print("showing the weather (%s view)" % render.current_view())
        k = kindle.health()
        if k["battery"] is not None:
            print("Kindle: battery %d%%%s, light %s, last seen %s%s" % (
                k["battery"], " (charging)" if k["charging"] else "",
                "?" if k["light"] is None else "%d%%" % k["light"],
                "never" if k["seen_min_ago"] is None else "%g min ago" % k["seen_min_ago"],
                {"usb": " over USB", "wifi": " over Wi-Fi"}.get(k["link"], "")))
        return 0
    if cmd == "todo":
        return todo_command(rest, no_push)
    if cmd in ("reminders", "reminder"):
        return reminders_command(rest, no_push)
    if cmd in ("slides", "slideshow"):
        menu.close()
        return slides_command(rest, no_push)
    if cmd in ("weathermap", "wmap", "map"):
        menu.close()
        for a in rest:
            if a in ("rain", "clouds", "wind", "temp", "temperature"):
                wmap.tap("layer:" + ("temp" if a == "temperature" else a))
            elif a in ("state", "country"):
                wmap.tap("area:" + a)
            elif a == "now":
                wmap.tap("now")
            else:
                raise SystemExit("kindle weathermap [rain|clouds|wind|temp] [state|country] [now]")
        return switch_screen("wmap", push=not no_push)
    if cmd in ("mandelbrot", "mandel", "fractal"):
        menu.close()
        if rest[:1] and rest[0] in ("reset", "out", "back", "detail"):
            mandel.tap(rest[0])
        elif rest:
            raise SystemExit("kindle mandelbrot [reset|out|back|detail]")
        return switch_screen("mandel", push=not no_push)
    if cmd == "home":
        menu.close()
        return switch_screen("home", push=not no_push)
    if cmd == "clock":
        if rest:
            want = {"analog": "analog", "analogue": "analog", "digital": "digital", "both": "both"}.get(rest[0])
            if not want:
                print("kindle clock [analog|digital|both]")
                return 1
            clock.set_style(want)
        return switch_screen("clock", push=not no_push)
    if cmd in ("games", "game"):
        menu.close()
        aliases = {"ttt": "ttt", "tictactoe": "ttt", "c4": "c4", "connect4": "c4",
                   "reversi": "reversi", "othello": "reversi", "mines": "mines", "minesweeper": "mines",
                   "checkers": "checkers", "draughts": "checkers", "chess": "chess",
                   "battleship": "battleship", "sudoku": "sudoku"}
        st = games.load()
        for a in rest:
            if a in ("2p", "pi"):
                st["mode"] = a
            elif a in ("easy", "normal", "hard", "impossible"):
                st["level"] = a
        game = next((aliases[a] for a in rest if a in aliases), None)
        if game:
            games.new_game(st, game)
        games.save(st)
        return switch_screen("games", push=not no_push)
    if cmd == "spotify":
        menu.close()
        return spotify_command(rest, no_push)
    if cmd == "draw":
        menu.close()
        if rest[:1] == ["save"]:
            print(draw.save_png(*canvas_size()))
            return 0
        if rest[:1] == ["clear"]:
            draw.tool("clear", *canvas_size())
        return switch_screen("draw", push=not no_push)
    if cmd in ("calendar", "cal"):
        menu.close()
        return calendar_command(rest, no_push)
    if cmd in ("feeds", "feed", "rss"):
        menu.close()
        return feeds_command(rest, no_push)
    if cmd == "xkcd":                              # shortcut for the xkcd feed
        menu.close()
        data = feeds.load()
        if not any(f["url"] == feeds.DEFAULT[0]["url"] for f in data["items"]):
            data["items"].append(dict(feeds.DEFAULT[0]))
            feeds.save(data)
        i = [f["url"] for f in data["items"]].index(feeds.DEFAULT[0]["url"])
        return show_feed(index=i, push=None if no_push else "now")
    if cmd == "wifi":
        return wifi_command(rest)
    if cmd == "timer":
        menu.close()
        return timer_command(rest, no_push)
    if cmd == "dark":
        return dark_command(rest, no_push)
    if cmd == "speedtest":
        import speedtest
        kinds = [k for k in rest if k in ("down", "up")] or ["down", "up"]
        failed = False
        for kind in kinds:
            print("testing %s (about 10 seconds)..." % ("download" if kind == "down" else "upload"), flush=True)
            r = speedtest.run(kind)
            if r["error"].get(kind):
                print("  %s test failed: %s" % (kind, r["error"][kind]))
                failed = True
            elif kind == "down":
                print("  download %s  ·  ping %.0f ms" % (health.speed(r["down"]["bps"]), r["down"]["ping"]))
            else:
                print("  upload %s" % health.speed(r["up"]["bps"]))
        if current_screen() == "stats" and not no_push:
            refresh_screen(force=True, screen="stats")
            push_now(quick=True)
        return 1 if failed else 0
    if cmd in ("stats", "pistats"):
        menu.close()
        return switch_screen("stats", push=not no_push)
    if cmd in ("hackers", "honeypot"):
        menu.close()
        return switch_screen("hackers", push=not no_push)
    if cmd in ("brightness", "light"):
        return brightness_command(rest)
    if cmd == "health":
        print(json.dumps(kindle.health()))
        return 0
    if cmd in WEATHER_COMMANDS:
        return switch_screen(cmd, push=not no_push)

    if cmd == "image":
        if len(rest) != 1:
            raise SystemExit("usage: show.py image PATH")
        make_image(rest[0])
        what = os.path.basename(rest[0])
        print("prepared image %s" % what)
    elif cmd == "text":
        file_path = arg_value(rest, "--file")
        if "--stdin" in rest:
            text = sys.stdin.read()
        elif file_path:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            text = " ".join(rest)
        size, n = make_text(text)
        what = (text.strip().splitlines() or [""])[0][:40]
        print("prepared text (%d lines at %dpx)" % (n, size))
    else:
        raise SystemExit("unknown command %r (try: image, text, todo, reminders, slides, clock, calendar, feeds, xkcd, games, spotify, draw, hackers, brightness, wttr, wttr2, wttrmoon, status)" % cmd)

    until = time.time() + float(minutes) * 60 if minutes else None
    with open(STATE_FILE, "w") as f:
        json.dump({"kind": cmd, "what": what, "until": until}, f)
    if until:
        print("switching back to the weather at %s" % time.strftime("%H:%M", time.localtime(until)))
    return 0 if no_push else push_now(photo=(cmd == "image"))


if __name__ == "__main__":
    sys.exit(main())
