"""Calendar screen from iCal (.ics) links: a month grid on the left (today highlighted,
dots on days with events) and the next 7 days' agenda on the right. Swipe left/right on
the Kindle to move a week forward/back.

Named cal.py, not calendar.py, so it doesn't shadow Python's own calendar module.

calendar/calendars.json  the calendars (name + secret .ics URL) and the week offset
calendar/cache/          the downloaded .ics files, re-fetched every FETCH_MINUTES
"""

import calendar
import hashlib
import json
import os
import time
import urllib.request
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw

import render

CAL_DIR = os.path.join(render.BASE_DIR, "calendar")
INDEX_FILE = os.path.join(CAL_DIR, "calendars.json")
CACHE_DIR = os.path.join(CAL_DIR, "cache")
FETCH_MINUTES = 15
DAYS = 7
UA = {"User-Agent": "Mozilla/5.0 (kindle-dash calendar)"}


def load():
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("items", [])
    data.setdefault("offset", 0)            # weeks from this week
    return data


def save(data):
    os.makedirs(CAL_DIR, exist_ok=True)
    with open(INDEX_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(INDEX_FILE + ".tmp", INDEX_FILE)
    os.chmod(INDEX_FILE, 0o600)             # the .ics links are secret


def normalise(url):
    return "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url


def masked(url):
    """Show where a link points without printing its secret part."""
    host = url.split("/")[2] if "//" in url else url
    return "%s/…%s" % (host, url[-6:])


def format_list(data):
    if not data["items"]:
        return "  (no calendars)"
    return "\n".join("  %d. %s  (%s)" % (i + 1, c["name"], masked(c["url"]))
                     for i, c in enumerate(data["items"]))


def _cache_path(url):
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest()[:16] + ".ics")


def fetch(url, force=False):
    """.ics bytes for url (cached FETCH_MINUTES) and whether they changed. Raises only if
    there's no cached copy and the download fails."""
    path = _cache_path(url)
    try:
        fresh = time.time() - os.path.getmtime(path) < FETCH_MINUTES * 60
        with open(path, "rb") as f:
            old = f.read()
    except OSError:
        fresh, old = False, None
    if old is not None and fresh and not force:
        return old, False
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=render.HTTP_TIMEOUT_SEC) as resp:
            new = resp.read()
        if b"BEGIN:VCALENDAR" not in new[:2000]:
            raise ValueError("not an iCal (.ics) file")
    except Exception as e:
        if old is None:
            raise
        render.log("calendar: fetch failed, using cached copy: %s" % e)
        os.utime(path)                      # don't retry every minute
        return old, False
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(new)
    os.replace(path + ".tmp", path)
    return new, new != old


def refresh_all(force=False):
    """Fetch every calendar; True if any changed."""
    changed = False
    for c in load()["items"]:
        try:
            changed |= fetch(c["url"], force)[1]
        except Exception as e:
            render.log("calendar %s: %s" % (c["name"], e))
    return changed


def events(start, end):
    """Events overlapping [start, end) from all calendars, as dicts
    {"title", "start", "end" (naive local datetimes), "all_day", "calendar"}, sorted."""
    import icalendar
    import recurring_ical_events
    out = []
    for c in load()["items"]:
        path = _cache_path(c["url"])
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                ical = icalendar.Calendar.from_ical(f.read())
            occurrences = recurring_ical_events.of(ical).between(start, end)
        except Exception as e:
            render.log("calendar %s: couldn't read events: %s" % (c["name"], e))
            continue
        for ev in occurrences:
            s = ev.get("DTSTART").dt
            e_ = ev.get("DTEND").dt if ev.get("DTEND") else s
            all_day = not isinstance(s, datetime)
            if all_day:
                s = datetime.combine(s, datetime.min.time())
                e_ = datetime.combine(e_, datetime.min.time()) if not isinstance(e_, datetime) else e_
            else:
                s = s.astimezone().replace(tzinfo=None) if s.tzinfo else s      # to local time
                e_ = e_.astimezone().replace(tzinfo=None) if getattr(e_, "tzinfo", None) else e_
            out.append({"title": str(ev.get("SUMMARY", "(no title)")).strip(), "start": s,
                        "end": e_, "all_day": all_day, "calendar": c["name"]})
    out.sort(key=lambda e: (e["start"], not e["all_day"]))
    return out


def next_event(now=None):
    """The next timed event starting after now (within 7 days), or None."""
    now = now or datetime.now()
    upcoming = [e for e in events(now, now + timedelta(days=7)) if not e["all_day"] and e["start"] > now]
    return upcoming[0] if upcoming else None


# ------------------------------ drawing -----------------------------

def _t(dt):
    return dt.strftime("%-I:%M%p").lower().replace(":00", "")


def draw(w, h, now=None):
    """Month grid + 7-day agenda. Returns the canvas image."""
    import show                                       # (show imports this module)
    now = now or datetime.now()
    data = load()
    today = now.date()
    first = today + timedelta(days=7 * data["offset"])          # agenda's first day
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    M = 50

    # ---- left: month of the agenda's first day
    left_w = 470
    title = render.ui_font(52, "bold")
    d.text((M, 34), first.strftime("%B %Y"), font=title, fill=0)
    month_events = set()
    month_start = first.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1)
    for e in events(datetime.combine(month_start, datetime.min.time()),
                    datetime.combine(month_end, datetime.min.time())):
        dd = e["start"].date()
        stop = max(e["end"].date(), dd + timedelta(days=1))   # end is exclusive
        if not e["all_day"] and e["end"].time() != datetime.min.time():
            stop = max(stop, e["end"].date() + timedelta(days=1))  # timed: include end day
        while dd < stop and dd < month_end:
            month_events.add(dd)
            dd += timedelta(days=1)
    cell = (left_w - M) / 7
    head = render.ui_font(26, "semibold")
    num = render.ui_font(32)
    gy = 130
    for i, name in enumerate(["M", "T", "W", "T", "F", "S", "S"]):
        d.text((M + i * cell + (cell - d.textlength(name, font=head)) / 2, gy), name, font=head, fill=0)
    gy += 50
    for week in calendar.Calendar().monthdatescalendar(first.year, first.month):
        for i, day in enumerate(week):
            if day.month != first.month:
                continue
            cx = M + i * cell + cell / 2
            label = str(day.day)
            if day == today:
                d.ellipse((cx - 28, gy - 4, cx + 28, gy + 52), fill=0)
            in_window = first <= day < first + timedelta(days=DAYS)
            fg = 255 if day == today else (0 if in_window else 110)
            d.text((cx - d.textlength(label, font=num) / 2, gy + 4), label, font=num, fill=fg)
            if day in month_events:
                d.ellipse((cx - 4, gy + 44, cx + 4, gy + 52), fill=255 if day == today else 0)
        gy += 64
    small = render.ui_font(26)
    if data["offset"]:
        hint = "%+d week%s · swipe to move" % (data["offset"], "" if abs(data["offset"]) == 1 else "s")
    else:
        hint = "swipe for other weeks"
    d.text((M, h - M - 30), hint, font=small, fill=0)
    d.line((left_w + 20, M, left_w + 20, h - M), fill=0, width=3)

    # ---- right: agenda
    x0, x1 = left_w + 60, w - M
    y = 40
    day_font = render.ui_font(36, "bold")
    ev_font = render.ui_font(32)
    time_font = render.ui_font(30, "semibold")
    agenda = events(datetime.combine(first, datetime.min.time()),
                    datetime.combine(first + timedelta(days=DAYS), datetime.min.time()))
    imported = bool(load()["items"])
    if not imported:                                  # still the whole screen, plus a note
        note = (x0, y, x1, y + 120)
        d.rounded_rectangle(note, radius=18, outline=0, width=3)
        d.text((x0 + 24, y + 16), "No calendar imported yet", font=render.ui_font(38, "semibold"), fill=0)
        d.text((x0 + 24, y + 70), "On the Mac: kindle calendar add <iCal link>", font=small, fill=0)
        y += 150
    shown_any = False
    for n in range(DAYS):
        day = first + timedelta(days=n)
        todays = [e for e in agenda if e["start"].date() <= day < max(e["end"].date(), e["start"].date() + timedelta(days=1))
                  if e["all_day"] or e["start"].date() == day]
        if not todays and day != today and imported:
            continue
        if y + 50 > h - M:
            break
        label = ("Today" if day == today else "Tomorrow" if day == today + timedelta(days=1)
                 else day.strftime("%A")) + day.strftime("  ·  %-d %b")
        d.text((x0, y), label, font=day_font, fill=0)
        y += 54
        if not todays:
            d.text((x0 + 20, y), "Nothing on", font=ev_font, fill=110)
            y += 48
        for e in todays:
            if y + 44 > h - M:
                more = "…"
                d.text((x0 + 20, y), more, font=ev_font, fill=0)
                y += 44
                break
            past = not e["all_day"] and e["end"] < now
            fg = 140 if past else 0
            when = "all day" if e["all_day"] else _t(e["start"])
            d.text((x0 + 20, y), when, font=time_font, fill=fg)
            title_x = x0 + 190
            text = show.wrap(e["title"], ev_font, x1 - title_x)[0]
            if text != e["title"]:
                text = text.rstrip() + "…"
            d.text((title_x, y), text, font=ev_font, fill=fg)
            if not e["all_day"] and e["start"] <= now < e["end"]:
                d.rectangle((x0, y + 4, x0 + 8, y + 36), fill=0)              # happening now
            y += 46
            shown_any = True
        y += 18
    if imported and not shown_any and y < h - 200:
        d.text((x0, y + 10), "No events in the next %d days" % DAYS, font=ev_font, fill=0)
    return img
