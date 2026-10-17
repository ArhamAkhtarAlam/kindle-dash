#!/usr/bin/env python3
"""Reminders: at the set time the Kindle flashes, then shows the reminder in white on
black until it's dismissed (tap the screen, or "show.py reminders dismiss").

Stored in out/reminders.json. push.sh runs "reminders.py --check" every minute
(from the timer); it prints "fired" when a reminder has just gone off.
"""

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

import render

REMINDERS_FILE = os.path.join(render.OUT_DIR, "reminders.json")
ALERT_FILE = os.path.join(render.OUT_DIR, "alert.json")     # active alert(s)
ALERT_IMAGE = os.path.join(render.OUT_DIR, "alert.png")
LOCK_FILE = os.path.join(render.OUT_DIR, ".reminders.lock")
GRACE_MIN = 10          # a reminder still fires if the Pi/timer was up to this late
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@contextmanager
def locked():
    os.makedirs(render.OUT_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load():
    return _load(REMINDERS_FILE, [])


def save(items):
    _save(REMINDERS_FILE, items)


# ------------------------------ parsing -----------------------------

def parse_time(s):
    """'9am', '9:00am', '9:30 pm', '21:30', '0930' -> 'HH:MM', or None."""
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*([ap]\.?m\.?)?", s.strip().lower())
    if not m:
        return None
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ampm.startswith("p") else 0)
    if h > 23 or mins > 59:
        return None
    return "%02d:%02d" % (h, mins)


def parse_days(s):
    """'Mon,Wed' / 'weekdays' / 'weekends' / 'daily' -> sorted list of 0-6 (Mon=0), or None."""
    s = s.strip().lower()
    if s in ("daily", "everyday", "every day", "all"):
        return list(range(7))
    if s in ("weekdays", "weekday"):
        return [0, 1, 2, 3, 4]
    if s in ("weekends", "weekend"):
        return [5, 6]
    days = set()
    for part in re.split(r"[,\s/]+", s):
        if not part:
            continue
        hits = [i for i, d in enumerate(DAY_NAMES) if part[:3] == d.lower()]
        if not hits:
            return None
        days.add(hits[0])
    return sorted(days) or None


def describe(r):
    if r.get("days"):
        days = r["days"]
        when = ("every day" if len(days) == 7 else "weekdays" if days == [0, 1, 2, 3, 4]
                else "weekends" if days == [5, 6] else ",".join(DAY_NAMES[d] for d in days))
    else:
        when = "once, " + datetime.strptime(r["date"], "%Y-%m-%d").strftime("%a %d %b")
    return "%s  %s  (%s)" % (to_12h(r["time"]), r["text"], when)


def to_12h(hhmm):
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M%p").lstrip("0").lower()


def format_list(items):
    if not items:
        return "  (no reminders)"
    return "\n".join("  %d. %s" % (i + 1, describe(r)) for i, r in enumerate(items))


def new_reminder(text, hhmm, days, now=None):
    now = now or datetime.now()
    r = {"text": text, "time": hhmm, "days": days, "last_fired": None}
    if not days:                       # one-off: today if still ahead, else tomorrow
        h, m = map(int, hhmm.split(":"))
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        r["date"] = at.strftime("%Y-%m-%d")
    return r


# ------------------------------ firing ------------------------------

def due_today(r, now):
    """The occurrence of r on now's date as a datetime, or None if it isn't scheduled today."""
    if r.get("days"):
        if now.weekday() not in r["days"]:
            return None
    elif r.get("date") != now.strftime("%Y-%m-%d"):
        return None
    h, m = map(int, r["time"].split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def next_due(now=None):
    """(datetime, text) of the next reminder within a week, or None."""
    now = now or datetime.now()
    best = None
    for r in load():
        for days_ahead in range(8):
            day = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
            at = due_today(r, day)
            if at and at > now:
                if not best or at < best[0]:
                    best = (at, r["text"])
                break
    return best


def check(now=None):
    """Fire reminders whose time has come (within GRACE_MIN). Returns the fired texts."""
    now = now or datetime.now()
    fired, keep = [], []
    with locked():
        items = load()
        for r in items:
            at = due_today(r, now)
            key = at.strftime("%Y-%m-%d %H:%M") if at else None
            if at and timedelta(0) <= now - at <= timedelta(minutes=GRACE_MIN) \
                    and r.get("last_fired") != key:
                r["last_fired"] = key
                fired.append(r["text"])
                if not r.get("days"):
                    continue                       # one-off: done
            elif not r.get("days") and (r["date"] < now.strftime("%Y-%m-%d")
                                        or (at and now - at > timedelta(minutes=GRACE_MIN))):
                render.log("reminder missed (Pi was off?): %s" % r["text"])
                continue                           # one-off whose time passed unseen
            keep.append(r)
        if fired or len(keep) != len(items):
            save(keep)
        if fired:
            raise_alert(fired, now)
    return fired


def raise_alert(texts, now=None):
    """Show texts as an alert (added to any alert already showing)."""
    now = now or datetime.now()
    alert = _load(ALERT_FILE, {"items": []})
    alert["items"] += [{"text": t, "at": now.strftime("%H:%M")} for t in texts]
    _save(ALERT_FILE, alert)
    draw_alert(alert["items"])
    for t in texts:
        render.log("reminder: %s" % t)


def alert_active():
    return os.path.exists(ALERT_FILE) and os.path.exists(ALERT_IMAGE)


def dismiss():
    """Clear the alert. Returns True if there was one."""
    was = alert_active()
    for p in (ALERT_FILE, ALERT_IMAGE):
        try:
            os.remove(p)
        except OSError:
            pass
    return was


def draw_alert(items):
    """White text on black, as large as fits; 'tap to dismiss' at the bottom."""
    import show                                        # (show imports this module)
    w, h = show.canvas_size()
    text = "\n\n".join(it["text"] for it in items)
    margin, footer_h = 70, 60
    best = None
    lo, hi = 20, 300
    while lo <= hi:
        size = (lo + hi) // 2
        font = render.ui_font(size, "semibold")
        lines = show.wrap(text, font, w - 2 * margin)
        lh = int(size * 1.25)
        if lh * len(lines) <= h - 2 * margin - footer_h:
            best, lo = (font, lines, lh), size + 1
        else:
            hi = size - 1
    if best is None:
        small_font = render.ui_font(20)
        best = (small_font, show.wrap(text, small_font, w - 2 * margin), 25)
    font, lines, lh = best
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    y = (h - footer_h - lh * len(lines)) // 2
    for line in lines:
        d.text(((w - d.textlength(line, font=font)) / 2, y), line, font=font, fill=255)
        y += lh
    small = render.ui_font(32)
    foot = "reminder · %s · tap anywhere to dismiss" % items[-1]["at"]
    d.text(((w - d.textlength(foot, font=small)) / 2, h - margin - 32), foot, font=small, fill=255)
    show.finish(img, ALERT_IMAGE)


if __name__ == "__main__":
    if "--check" in sys.argv:
        fired = check()
        import timer                                   # backup: timers that ended (if touch.py
        done = timer.check_done()                      # isn't running to catch them sooner)
        if done:
            with locked():
                raise_alert([done])
            fired = True
        if fired:
            print("fired")
