#!/usr/bin/env python3
"""Tick off to-dos by tapping the Kindle's screen.

Runs on the Pi (kindle-touch.service). It opens an ssh session to the Kindle that
streams the touchscreen's raw input events ("cat /dev/input/eventN"), so nothing is
installed on the Kindle. When a reminder is showing, a tap anywhere dismisses it.
When the to-do screen is showing, a tap on a row toggles it, redraws the screen and
pushes it back with a quick partial refresh.
Swiping down from the top edge opens a menu of screens (menu.py): tap one to switch,
tap below it or swipe up to close. In the slideshow, swipe left/right for next/previous.

  touch.py           run
  touch.py --debug   also log every tap's raw position and where it landed
"""

# ============================== CONFIG ==============================
TOUCH_DEVICE = None     # None = auto-detect, or e.g. "/dev/input/event1"
TOUCH_MAX_X = None      # None = touch coordinates are already screen pixels (1072 wide);
TOUCH_MAX_Y = None      #   otherwise the device's max raw value, to scale from
TOUCH_SWAP_XY = False   # set these if --debug shows taps landing in the wrong place
TOUCH_FLIP_X = False
TOUCH_FLIP_Y = False
TAP_SLOP = 60           # finger may move this many units and still count as a tap
SWIPE_TOP_BAND = 0.18   # a "swipe from the top" must start in this top fraction of the screen
SWIPE_MIN = 200         # ...and move at least this many pixels (swipe up closes the menu)
TAP_DEBOUNCE_SEC = 0.6  # ignore a second tap on the screen this soon after one...
DOUBLE_TAP_SEC = 0.5    # ...unless it's close to the first one this soon: a double tap, which
DOUBLE_TAP_SLOP = 120   #   redraws the screen with a clean flash (clears ghosting)
# ====================================================================

import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time

import cal
import clock
import doom
import draw
import feeds
import health
import home
import games
import kindle
import mandel
import menu
import osu
import synth
import wmap
import reminders
import render
import show
import spotify
import timer
import todo

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x35, 0x36, 0x39
BTN_TOUCH = 0x14A
EVENT = struct.Struct("<llHHi")  # struct input_event on the Kindle's 32-bit kernel (16 bytes)

DEBUG = "--debug" in sys.argv
log = render.log


def ssh_cmd(command, ip=None):
    return ["ssh"] + render.ssh_options() + ["root@" + (ip or kindle.address()[0] or render.KINDLE_IP), command]


# --------------------------- device lookup --------------------------

def parse_devices(text):
    """From /proc/bus/input/devices, pick the touchscreen: the device that reports
    multi-touch positions (ABS_MT_POSITION_X), preferring one with 'touch' in its name."""
    best = None
    for block in text.strip().split("\n\n"):
        name, handler, abs_bits = "", None, 0
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"')
            elif line.startswith("H: Handlers="):
                for h in line.split("=", 1)[1].split():
                    if h.startswith("event"):
                        handler = h
            elif line.startswith("B: ABS="):
                abs_bits = 0
                # Highest word first; words are 32 bits on the Kindle's 32-bit kernel.
                for word in line.split("=", 1)[1].split():
                    abs_bits = (abs_bits << 32) | int(word, 16)
        if handler and abs_bits >> ABS_MT_POSITION_X & 1:
            score = 2 if "touch" in name.lower() else 1
            if not best or score > best[0]:
                best = (score, "/dev/input/" + handler, name)
    return best[1:] if best else None


def find_device(ip):
    if TOUCH_DEVICE:
        return TOUCH_DEVICE, "configured"
    out = subprocess.run(ssh_cmd("cat /proc/bus/input/devices", ip), capture_output=True,
                         text=True, timeout=30, stdin=subprocess.DEVNULL).stdout   # (never our input)
    return parse_devices(out)


# ---------------------------- tap detection -------------------------

class TapDetector:
    """Turns raw touch events into gestures when the finger lifts: ("tap", start, start, path)
    if it barely moved, else ("swipe", start, end, path). Handles both multi-touch (ABS_MT_*)
    and single-touch (ABS_X/Y + BTN_TOUCH) drivers."""

    def __init__(self):
        self.x = self.y = None
        self.down = False
        self.start = None
        self.moved = 0
        self.pending = None      # "down" or "up", applied at the next EV_SYN
        self.path = []           # every position while the finger is down (for drawing)

    def feed(self, etype, code, value):
        if etype == EV_ABS:
            if code in (ABS_MT_POSITION_X, ABS_X):
                self.x = value
            elif code in (ABS_MT_POSITION_Y, ABS_Y):
                self.y = value
            elif code == ABS_MT_TRACKING_ID:
                self.pending = "up" if value < 0 else "down"
        elif etype == EV_KEY and code == BTN_TOUCH:
            self.pending = "down" if value else "up"
        elif etype == EV_SYN and self.x is not None and self.y is not None:
            if self.pending == "down" and not self.down:
                self.down, self.start, self.moved = True, (self.x, self.y), 0
                self.path = [self.start]
            elif self.down:
                self.moved = max(self.moved, abs(self.x - self.start[0]),
                                 abs(self.y - self.start[1]))
                if (self.x, self.y) != self.path[-1]:
                    self.path.append((self.x, self.y))
                if self.pending == "up":
                    self.down = False
                    self.pending = None
                    if self.moved <= TAP_SLOP:
                        return ("tap", self.start, self.start, self.path)
                    return ("swipe", self.start, (self.x, self.y), self.path)
            self.pending = None
        return None


def raw_to_panel(x, y):
    if TOUCH_SWAP_XY:
        x, y = y, x
    if TOUCH_MAX_X:
        x = x * (render.WIDTH - 1) / TOUCH_MAX_X
    if TOUCH_MAX_Y:
        y = y * (render.HEIGHT - 1) / TOUCH_MAX_Y
    if TOUCH_FLIP_X:
        x = render.WIDTH - 1 - x
    if TOUCH_FLIP_Y:
        y = render.HEIGHT - 1 - y
    return x, y


# ------------------------------ actions -----------------------------

def _menu_redraw():
    m = json.load(open(menu.MENU_FILE))
    menu.redraw(m.get("screen"), page=m.get("page", "main"))


def paused(pause):
    """A video/mirror/game has the screen: its marker is recent AND its program is still running
    (a crashed one must never leave the Kindle ignoring taps)."""
    try:
        if time.time() - os.path.getmtime(pause) >= 15 * 60:
            return False
        with open(pause) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


_pending = [None]                                      # a single-tap action waiting out DOUBLE_TAP_SEC


def _later(action):
    """Run action once it's clear the tap wasn't the first half of a double tap."""
    if _pending[0]:
        _pending[0].cancel()
    _pending[0] = threading.Timer(DOUBLE_TAP_SEC + 0.1, action)
    _pending[0].daemon = True
    _pending[0].start()


_player = threading.Lock()


def play_weather_map():
    """Weather map Play: step through the hours until Stop, the loops run out, or you leave."""
    if not _player.acquire(blocking=False):
        return                                         # already playing
    try:
        frame_at = time.time()
        while True:
            time.sleep(max(0.0, wmap.FRAME_SECONDS - (time.time() - frame_at)))   # (drawing counts)
            frame_at = time.time()
            covered = reminders.alert_active() or menu.is_open()
            if show.current_screen() != "wmap" or covered:
                wmap.stop()
                break
            if not wmap.step_play():
                break                                  # Stop was tapped
            show.refresh_screen(force=True, screen="wmap")
            if wmap.playing():
                show.push_now(quick=True)
            else:
                log("weather map: played through %d times, stopped" % wmap.PLAY_LOOPS)
                break
        if show.current_screen() == "wmap":
            show.refresh_screen(force=True, screen="wmap")
            show.push_now()                            # one clean flash after the animation
    except Exception as e:
        log("weather map play: %s" % e)
        wmap.stop()
    finally:
        _player.release()


def double_tap(push=True):
    """Redraw whatever is showing with one clean full flash."""
    if _pending[0]:
        _pending[0].cancel()                           # the first tap's own action, if it waited
        _pending[0] = None
    pause = os.path.join(render.OUT_DIR, ".pause")
    if paused(pause):
        return None                                    # video/mirroring: leave it alone
    render.mark_active()
    log("double tap: clean refresh")
    if push:
        show.push_now()                                # (a full, flashing refresh)
    return "refresh"


def handle_gesture(kind, start, end, path=None, push=True):
    """Route a finished touch. Any touch dismisses a showing reminder. With the menu open:
    tap a button to switch screens, tap below it or swipe up to close. Otherwise a swipe
    down from the top opens the menu and a tap ticks a to-do."""
    render.mark_active()
    pause = os.path.join(render.OUT_DIR, ".pause")
    if paused(pause):
        log("tap during video/mirroring: stopping it")    # any touch stops it
        if push:
            try:
                with open(pause) as f:
                    os.kill(int(f.read().strip()), signal.SIGTERM)   # it cleans up and redraws
            except (OSError, ValueError):
                kindle.run("pkill -f video/play.sh; true")
        return "stop video"
    if reminders.alert_active() and show.dismiss_reminder(push=push):
        return "dismissed"
    layout = {"canvas": list(show.canvas_size()), "rotate": render.ROTATE,
              "degrees": render.ROTATE_DEGREES}
    _, h = layout["canvas"]
    sx, sy = todo.panel_to_canvas(*raw_to_panel(*start), layout)
    ex, ey = todo.panel_to_canvas(*raw_to_panel(*end), layout)
    if DEBUG:
        log("%s canvas (%d,%d) -> (%d,%d)" % (kind, sx, sy, ex, ey))
    dy, dx = ey - sy, abs(ex - sx)
    swipe_down = kind == "swipe" and dy >= SWIPE_MIN and dy > dx
    swipe_up = kind == "swipe" and -dy >= SWIPE_MIN and -dy > dx

    if menu.is_open():
        choice = "outside" if swipe_up else menu.hit(sx, sy) if kind == "tap" else None
        if choice == ("dark",):                    # dark mode on/off (redraws the screen)
            on = not render.dark_mode()
            log("dark mode %s" % ("on" if on else "off"))
            show.set_dark(on, push=push)
            return "dark " + ("on" if on else "off")
        if choice == ("wifi",):                    # Wi-Fi on/off button
            on = not kindle.status().get("wifi")
            problem = kindle.set_wifi(on)
            log("Wi-Fi %s%s" % ("on" if on else "off", ": " + problem if problem else ""))
            _menu_redraw()
            if push:
                show.push_now(quick=True)
            return "wifi " + ("on" if on else "off")
        if isinstance(choice, tuple):              # ("light", percent) from the slider
            step = kindle.set_light(choice[1])
            log("front light -> %d%%%s" % (choice[1], "" if step is not None else " (failed)"))
            _menu_redraw()
            if push:
                show.push_now(quick=True)
            return "light %d%%" % choice[1]
        if choice in ("weather", "back"):          # into / out of the weather choices
            m = json.load(open(menu.MENU_FILE))
            menu.redraw(m.get("screen"), page="weather" if choice == "weather" else "main")
            log("menu: %s" % ("weather choices" if choice == "weather" else "back"))
            if push:
                show.push_now(quick=True)
            return "menu " + choice
        if choice == "outside":
            menu.close()
            log("menu closed")
            if push:
                show.push_now(quick=True)
            return "closed"
        if choice:
            log("menu: switching to %s" % choice)
            show.switch_screen(choice, push=push)
            return choice
        return None
    sideways = kind == "swipe" and dx >= SWIPE_MIN and dx > abs(dy)
    if sideways and show.current_screen() == "calendar":
        data = cal.load()
        data["offset"] += 1 if ex < sx else -1      # swipe left = next week
        cal.save(data)
        log("calendar: week %+d" % data["offset"])
        show.switch_screen("calendar", push=push)
        return "week %+d" % data["offset"]
    if sideways and show.current_screen() == "feeds":
        step = 1 if ex < sx else -1
        log("feeds: %s feed" % ("next" if step == 1 else "previous"))
        show.show_feed(step, push="now" if push else None)
        return "feed " + ("next" if step == 1 else "previous")
    if kind == "tap" and show.current_screen() == "feeds":
        row = feeds.row_at(sy)
        if row is None:
            return None
        detail = None if row == "back" else row
        log("feeds: %s" % ("back to list" if detail is None else "open item %d" % detail))
        show.show_feed(detail=detail, push="quick" if push else None)
        return "feed back" if detail is None else "feed item %d" % detail
    if sideways and show.current_screen() == "slides":
        step = 1 if ex < sx else -1                # swipe left = next, right = previous
        log("slideshow: %s" % ("next" if step == 1 else "previous"))
        show.show_slide(step, push="now" if push else None)
        return "next" if step == 1 else "previous"
    screen = show.current_screen()
    band = 0.06 if screen == "draw" else SWIPE_TOP_BAND    # drawing: only the very top edge
    if swipe_down and sy <= h * band:
        menu.open_menu(show.current_image(), show.current_screen())
        log("menu opened")
        if push:
            show.push_now(quick=True)
        return "menu"
    if screen == "draw":
        return handle_draw(kind, sx, sy, path or [start], layout, push)
    if screen == "home" and kind == "tap":             # Home: tap a part for its full screen
        action = home.hit(sx, sy)
        if not action:
            return None
        log("home: opening %s" % action)
        if push:
            show.switch_screen(action, push=True)
        return "home " + action
    if screen == "wmap":                               # weather map: buttons, slider; swipe = hour
        if kind == "tap":
            action = wmap.hit(sx, sy)
        elif sideways:
            action = "next" if ex < sx else "prev"     # swipe left = later
        else:
            action = None
        done = wmap.tap(action) if action else None
        if not done:
            return None
        log("weather map: %s" % done)
        show.refresh_screen(force=True, screen="wmap")
        if push:
            show.push_now(quick=True)
            if done == "play":
                threading.Thread(target=play_weather_map, daemon=True).start()
        return "weather map " + done
    if screen == "mandel":                             # tap = zoom in there, swipe = drag the view
        size = mandel.layout_size()
        if not size:
            return None
        if kind == "tap":
            action = mandel.hit(sx, sy)
            done = mandel.tap(action, sx, sy, size) if action else None
        else:
            done = mandel.pan(ex - sx, ey - sy, size)
        if not done:
            return None
        log("mandelbrot: %s" % done)
        show.refresh_screen(force=True, screen="mandel")
        if push:
            show.push_now()                            # full refresh: clean greys
        return "mandelbrot " + done
    if screen == "games" and kind == "tap":
        action = games.hit(sx, sy)
        if not action or not games.tap(action):
            return None
        log("games: %s" % action)
        show.refresh_screen(force=True, screen="games")
        if push:
            show.push_now(quick=True)
        if games.think():                              # impossible: your move is up, now the Pi's
            show.refresh_screen(force=True, screen="games")
            if push:
                show.push_now(quick=True)
        return "game " + action
    if screen == "stats" and sideways:                 # overview -> processes -> mirror (and back)
        page = health.next_page(1 if ex < sx else -1)  # swipe left = next, right = previous
        log("pi stats: %s" % page)
        w_, h_ = show.canvas_size()
        show.push_region(health.draw(w_, h_), (0, 0, w_, h_), wave="GL16")
        return "stats " + page
    hit_test = health.hit(sx, sy) if screen == "stats" and kind == "tap" else None
    if hit_test == "mirror:start":                     # the Kindle becomes the Pi's screen
        log("pi stats: start mirroring the Pi")
        if push:
            with open(os.path.join(render.OUT_DIR, "mirror.log"), "a") as out:
                proc = subprocess.Popen([sys.executable, os.path.join(render.BASE_DIR, "mirror.py"), "pi", "fast"],
                                        cwd=render.BASE_DIR, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                        start_new_session=True)   # it pauses the dashboard; a tap stops it
            for _ in range(30):                        # couldn't start (e.g. no desktop)? show why
                if proc.poll() is not None:
                    log("pi stats: mirroring didn't start")
                    show.refresh_screen(force=True, screen="stats")
                    show.push_now(quick=True)
                    break
                time.sleep(0.1)
        return hit_test
    if hit_test and hit_test.startswith("sort:"):      # processes: change the order
        health.set_view(sort=hit_test[5:])
        w_, h_ = show.canvas_size()
        show.push_region(health.draw(w_, h_), (0, 0, w_, h_), wave="GL16")
        return hit_test
    if hit_test:                                       # "speedtest:down" / "speedtest:up"
        import speedtest
        which = hit_test.split(":")[1]
        if not speedtest.running():
            def run_test():
                log("speed test (%s): started" % which)
                r = speedtest.run(which)
                res = r.get(which) or {}
                log("speed test (%s): %s" % (which, r["error"].get(which) or health.speed(res.get("bps", 0))))
                if show.current_screen() == "stats":
                    show.refresh_screen(force=True, screen="stats")
                    show.push_now(quick=True)
            threading.Thread(target=run_test, daemon=True).start()
            time.sleep(0.2)                             # let it mark itself as running
            show.refresh_screen(force=True, screen="stats")
            if push:
                show.push_now(quick=True)
        return hit_test
    if screen == "clock" and kind == "tap":           # tap: analogue+digital -> analogue -> digital
        def switch_style():                            # (after a moment: it may be a double tap)
            new = clock.set_style("next")
            log("clock: %s" % new)
            show.refresh_screen(force=True, screen="clock")
            if push:
                show.push_now(quick=True)
        _later(switch_style)
        return "clock style (unless a double tap)"
    if screen == "timer" and kind == "tap":
        action = timer.hit(sx, sy)
        if not action or not timer.tap(action):
            return None
        log("timer: %s" % action)
        show.refresh_screen(force=True, screen="timer")
        if push:
            show.push_now(quick=True)
        return "timer " + action
    if screen == "spotify" and kind == "tap":
        action = spotify.hit(sx, sy)
        if not action:
            return None
        try:
            if action != "refresh":
                spotify.control(action)
            log("spotify: %s" % action)
        except Exception as e:
            log("spotify: %s failed: %s" % (action, e))
        show.refresh_screen(force=True, screen="spotify")
        if push:
            show.push_now(quick=True)
        return "spotify " + action
    if kind == "tap":
        return handle_tap(*start, push=push)
    return None


def handle_draw(kind, sx, sy, path, layout, push=True):
    """Drawing pad: a stroke (or a dot) in the drawing area, or a toolbar button."""
    w, h = layout["canvas"]
    where = draw.hit(sx, sy)
    if isinstance(where, tuple) and kind == "tap":
        what = draw.tool(where[1], w, h)
        log("draw: %s" % what)
    elif where == "canvas":
        pts = [todo.panel_to_canvas(*raw_to_panel(*p), layout) for p in path]
        draw.add_stroke(pts, h)
        what = "stroke (%d points)" % len(pts)
        if DEBUG:
            log("draw: " + what)
    else:
        return None
    show.refresh_screen(force=True, screen="draw")
    if push:
        show.push_now(quick=True)
    return "draw " + what


def handle_tap(raw_x, raw_y, push=True):
    """Dismiss a showing reminder (tap anywhere), else toggle the to-do under the tap.
    Returns "dismissed", the toggled item text, or None."""
    px, py = raw_to_panel(raw_x, raw_y)
    if not show.todo_showing():
        if DEBUG:
            log("tap raw=(%d,%d) panel=(%d,%d): to-do screen not showing, ignored"
                % (raw_x, raw_y, px, py))
        return None
    with todo.locked():
        layout = todo.load_layout()
        if not layout:
            return None
        cx, cy = todo.panel_to_canvas(px, py, layout)
        row = todo.row_at(layout, cx, cy)
        if DEBUG:
            log("tap raw=(%d,%d) panel=(%d,%d) canvas=(%d,%d) row=%s"
                % (raw_x, raw_y, px, py, cx, cy, row and row["text"]))
        if not row:
            return None
        items = todo.load()
        i = row["index"]
        if not (i < len(items) and items[i]["text"] == row["text"]):
            try:                       # list changed since it was drawn: match by text
                i = todo.find(items, row["text"])
            except ValueError:
                return None
        items[i]["done"] = not items[i]["done"]
        todo.save(items)
        show.show_todo_screen()
        log("%s by tap: %s" % ("ticked" if items[i]["done"] else "unticked", items[i]["text"]))
        text = items[i]["text"]
    if push:
        show.push_now(quick=True)
    return text


def listen(device, ip, via):
    log("listening for taps on %s (over %s)" % (device, "USB" if via == "usb" else "Wi-Fi"))
    proc = subprocess.Popen(ssh_cmd("exec cat " + device, ip), stdout=subprocess.PIPE)
    taps, buf, last_tap = TapDetector(), b"", 0.0
    first_tap = None                                   # (Kindle time, position) of the last tap
    layout = {"canvas": list(show.canvas_size()), "rotate": render.ROTATE, "degrees": render.ROTATE_DEGREES}
    pad, pad_checked = None, 0.0                       # DOOM's touch buttons, while it runs
    game_owns_touch = False
    try:
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            if time.time() - pad_checked > 0.5:
                pad_checked = time.time()
                was, game_owns_touch = game_owns_touch, osu.active() or synth.active()
                if was and not game_owns_touch:
                    taps = TapDetector()                 # fresh after the game
                if doom.active():
                    if pad is None:
                        log("DOOM is running: touches go to its buttons")
                        pad = doom.Touch(lambda x, y: todo.panel_to_canvas(*raw_to_panel(x, y), layout))
                elif pad is not None:
                    pad.release_all()
                    pad, taps = None, TapDetector()      # back to normal taps and swipes
            if game_owns_touch:                        # osu! reads the touches itself
                buf = buf[len(buf) - len(buf) % EVENT.size:]
                continue
            while len(buf) >= EVENT.size:
                sec, usec, etype, code, value = EVENT.unpack_from(buf)
                buf = buf[EVENT.size:]
                if pad is not None:
                    pad.feed(etype, code, value)
                    continue
                gesture = taps.feed(etype, code, value)
                if not gesture:
                    continue
                at = sec + usec / 1e6                  # the Kindle's own time of the touch
                if (gesture[0] == "tap" and first_tap and at - first_tap[0] <= DOUBLE_TAP_SEC
                        and max(abs(gesture[1][0] - first_tap[1][0]),
                                abs(gesture[1][1] - first_tap[1][1])) <= DOUBLE_TAP_SLOP):
                    first_tap, last_tap = None, time.time()
                    double_tap()
                elif time.time() - last_tap >= TAP_DEBOUNCE_SEC or gesture[0] == "tap" and \
                        first_tap and at - first_tap[0] > TAP_DEBOUNCE_SEC:
                    last_tap = time.time()
                    first_tap = (at, gesture[1]) if gesture[0] == "tap" else None
                    handle_gesture(*gesture)
    finally:
        proc.kill()
        proc.wait()
    log("touch stream ended (Kindle unplugged or asleep?), reconnecting")


def live_updates():
    """Spotify, kept live while it's on screen. Spotify itself is asked only every
    spotify.POLL_SECONDS (and after buttons / at a song's end); the progress bar runs on its
    own clock in between and is redrawn every PROGRESS_TICK as a small strip. Changes to the
    song, play/pause, volume or device redraw the whole screen (a new song with a full
    refresh, which clears the old cover)."""
    pause = os.path.join(render.OUT_DIR, ".pause")
    live = os.path.join(render.OUT_DIR, ".live_spotify")
    live_timer = os.path.join(render.OUT_DIR, ".live_timer")
    drawn, drawn_sec, timer_sec, stats_at, sampled_at = None, None, None, 0.0, 0.0
    live_stats = os.path.join(render.OUT_DIR, ".live_stats")
    while True:
        time.sleep(1.0 - time.time() % 1.0)             # once a second, on the second
        try:
            done = timer.check_done()                   # the timer ended: flash, whatever's showing
            if done:
                with reminders.locked():
                    reminders.raise_alert([done])
                log("timer done")
                show.push_now(flash=True)
                continue
            every = (health.SAMPLE_SECONDS if show.current_screen() == "stats"
                     else health.BACKGROUND_SAMPLE_SECONDS if render.is_active()
                     else health.IDLE_SAMPLE_SECONDS)     # idle: less background work
            if time.time() - sampled_at >= every - 0.05:
                sampled_at = time.time()                # Pi stats history: always, any screen
                health.record()
                open(live_stats, "a").close()
                os.utime(live_stats)
            screen = show.current_screen()
            covered = (reminders.alert_active() or menu.is_open()
                       or (paused(pause)))
            if screen == "timer" and not covered:      # tick the big digits once a second
                st = timer.load()
                sec = int(timer.remaining(st) + 0.999) if timer.running(st) else None
                if sec is not None and sec != timer_sec:
                    open(live_timer, "a").close()
                    os.utime(live_timer)
                    show.push_region(timer.digits_strip(show.canvas_size()[0], st),
                                     timer.digits_box(show.canvas_size()[0]))
                timer_sec = sec
            else:
                timer_sec = None
            if screen == "stats" and not covered and time.time() - stats_at >= health.REFRESH_SECONDS \
                    and health.view()["page"] != "mirror":    # (the mirror page never changes)
                stats_at = time.time()                  # Pi stats: redraw every second, sent with
                w_, h_ = show.canvas_size()             # FBInk's fast greyscale (GL16) refresh
                show.push_region(health.draw(w_, h_), (0, 0, w_, h_), wave="GL16")
            if screen != "spotify" or covered or not spotify.configured():
                drawn, drawn_sec = None, None
                continue
            open(live, "a").close()
            os.utime(live)                              # tells the minute timer to leave it to us
            s = spotify.current()
            sig = None if not s else (s["title"], s["artist"], s["album"], s["playing"],
                                      s["volume"], s["device"], s["art"])
            if sig != drawn:
                new_song = bool(drawn and sig and drawn[:2] != sig[:2])
                show.refresh_screen(force=True, screen="spotify")
                show.push_now(quick=not new_song)
                if new_song:
                    log("spotify: new song")
                drawn, drawn_sec = sig, int(s["progress"] // spotify.PROGRESS_TICK) if s else None
                continue
            tick = int(s["progress"] // spotify.PROGRESS_TICK) if s else None
            if s and s["playing"] and tick != drawn_sec:
                show.push_spotify_progress(s)
                drawn_sec = tick
        except Exception as e:
            log("live update error: %s" % e)
            time.sleep(2)


def main():
    threading.Thread(target=live_updates, daemon=True).start()
    device, waiting = None, False
    while True:
        try:
            ip, via = kindle.address()                 # USB, or Wi-Fi when unplugged
            if not ip:
                if not waiting:
                    log("Kindle not reachable (USB or Wi-Fi), waiting")
                    waiting = True
                time.sleep(20)
                continue
            waiting = False
            if not device:
                found = find_device(ip)
                if not found:
                    log("no touchscreen found in /proc/bus/input/devices; set TOUCH_DEVICE")
                    time.sleep(60)
                    continue
                device, name = found
                log("touchscreen: %s (%s)" % (device, name))
            listen(device, ip, via)
        except Exception as e:               # never die; systemd would restart anyway
            log("touch listener error: %s" % e)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
