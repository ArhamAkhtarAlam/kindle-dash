"""Home: the default screen. A big clock and today's weather, what's coming up (calendar,
reminders, a running timer) and your to-dos on the left; the Pi's vital signs and the Kindle's
battery on the right. Redrawn every minute; tap a part to open its full screen.

  out/home_layout.json   where each part is, for taps
"""

import json
import os
import time
from datetime import datetime

from PIL import Image, ImageDraw

import render

LAYOUT_FILE = os.path.join(render.OUT_DIR, "home_layout.json")
M = 48
SPLIT = 0.60                    # the left column's share of the width


def _fit(d, text, size, weight, width):
    f = render.ui_font(size, weight)
    while d.textlength(text, font=f) > width and size > 16:
        size -= 2
        f = render.ui_font(size, weight)
    return f


def _shorten(d, text, font, width):
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _pi_now():
    """CPU % and network speed measured over a quarter of a second, plus the slower readings."""
    import health
    c0, n0, t0 = health._cpu_times()["cpu"], health._net_bytes(), time.time()
    time.sleep(0.25)
    c1, n1, t1 = health._cpu_times()["cpu"], health._net_bytes(), time.time()
    cpu = 100.0 * (c1[0] - c0[0]) / max(1, c1[1] - c0[1])
    dt = max(0.05, t1 - t0)
    down, up = (n1[0] - n0[0]) * 8 / dt, (n1[1] - n0[1]) * 8 / dt
    used, total = health.memory()
    st = os.statvfs("/")
    disk = 1 - st.f_bavail / st.f_blocks
    try:
        with open(health.HISTORY_FILE) as f:
            hist = [h for h in json.load(f) if time.time() - h[0] <= 3600]
    except (OSError, ValueError):
        hist = []
    return {"cpu": cpu, "temp": health.temperature(), "mem": (used, total), "disk": disk,
            "up": health.uptime(), "down_bps": down, "up_bps": up, "hist": hist,
            "throttled": health._slow("throttled", health.throttled)}


def _coming_up(now):
    """[(when, text)] for the next calendar event and reminder, soonest first."""
    import reminders
    out = []
    r = reminders.next_due(now)
    if r:
        out.append(r)
    try:
        import cal
        ev = cal.next_event(now)
        if ev:
            out.append((ev["start"], ev["title"]))
    except Exception as e:
        render.log("home: calendar unavailable: %s" % e)
    return sorted(out)[:2]


def _forecast():
    """[(day, high, low, words, rain %)] for the next 3 days, from the clock's weather download."""
    import clock
    try:
        with open(clock.J1_CACHE) as f:
            data = json.load(f)
        out = []
        for k, day in enumerate(data["weather"][:3]):
            when = datetime.strptime(day["date"], "%Y-%m-%d")
            noon = day["hourly"][len(day["hourly"]) // 2]
            out.append(("Today" if k == 0 else when.strftime("%A"), day["maxtempC"], day["mintempC"],
                        noon["weatherDesc"][0]["value"].strip(), max(int(h_["chanceofrain"]) for h_ in day["hourly"])))
        return out
    except (OSError, ValueError, KeyError, IndexError):
        return []


def _now_playing():
    """What Spotify is playing (or has paused), or None; never slows Home down for long."""
    try:
        import spotify
        if not spotify.configured():
            return None
        m = spotify.current()
        return m if m and m.get("title") else None
    except Exception as e:
        render.log("home: Spotify unavailable: %s" % e)
        return None


def _ago(ts):
    mins = (time.time() - ts) / 60
    return "now" if mins < 1 else "%dm ago" % mins if mins < 60 else "%dh ago" % (mins // 60) if mins < 1440 \
        else "%dd ago" % (mins // 1440)


def _feeds(img, d, y, bottom_y, lw, now, targets):
    """The newest feed items in the space left: a picture if there's lots of room and the
    newest item has one, else headlines. Returns the new y."""
    if bottom_y - y < 96:
        return y
    try:
        import feeds
        latest = []
        for f in feeds.load()["items"]:
            for it in feeds.fetch(f)[0][:6]:
                latest.append((it.get("date") or 0, f["name"], it))
    except Exception as e:
        render.log("home: feeds unavailable: %s" % e)
        return y
    if not latest:
        return y
    latest.sort(key=lambda x: -x[0])
    top = y
    head = render.ui_font(30, "bold")
    small = render.ui_font(30)
    d.text((M, y), "LATEST", font=head, fill=0)
    y += 46
    when, name, it = latest[0]
    room = bottom_y - y
    pic = None
    if it.get("img") and room >= 300:
        try:
            import feeds
            pic = feeds.picture(it["img"])
        except Exception:
            pic = None
    if pic:
        import show
        cap = "%s · %s%s" % (name, it["title"], "  ·  " + _ago(when) if when else "")
        d.text((M, y), _shorten(d, cap, small, lw), font=small, fill=0)
        y += 42
        box_w, box_h = lw, bottom_y - y
        pic = pic.copy()
        pic.thumbnail((box_w, box_h), Image.LANCZOS)
        img.paste(show.to_16_grays(pic), (M, y))
        d.rectangle((M, y, M + pic.width, y + pic.height), outline=0, width=2)
        sx = M + pic.width + 24                        # the next headlines beside the picture
        if M + lw - sx >= 220:
            sy, sf, nf = y, render.ui_font(28, "semibold"), render.ui_font(22)
            for when2, name2, it2 in latest[1:]:
                if sy + 66 > y + pic.height:
                    break
                d.text((sx, sy), _shorten(d, it2["title"], sf, M + lw - sx), font=sf, fill=0)
                d.text((sx, sy + 34), "%s%s" % (name2, "  ·  " + _ago(when2) if when2 else ""), font=nf, fill=0)
                sy += 70
        y += pic.height + 10
    else:
        for when, name, it in latest[:max(0, int(room // 76))]:
            d.text((M, y), _shorten(d, it["title"], render.ui_font(32, "semibold"), lw), font=render.ui_font(32, "semibold"), fill=0)
            d.text((M, y + 38), "%s%s" % (name, "  ·  " + _ago(when) if when else ""), font=render.ui_font(24), fill=0)
            y += 76
    targets.append({"action": "feeds", "box": [0, top - 10, M + lw + M, y]})
    return y


def _when(at, now):
    day = "today" if at.date() == now.date() else "tomorrow" if (at.date() - now.date()).days == 1 \
        else at.strftime("%a")
    return "%s %s" % (at.strftime("%-I:%M%p").lower().replace(":00", ""), day)


def _bar(d, x, y, w, frac, h=12):
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, outline=0, width=2)
    if frac > 0.02:
        d.rounded_rectangle((x, y, x + max(h, w * min(1, frac)), y + h), radius=h // 2, fill=0)


def draw(w, h, now=None):
    import clock
    import health
    import kindle
    import timer
    import todo
    now = now or datetime.now()
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    split = int(w * SPLIT)
    lw = split - 2 * M
    targets = []

    # ---- left: clock, date, weather
    big = render.ui_font(230, "medium")
    hhmm, ampm = now.strftime("%-I:%M"), now.strftime("%p").lower()
    top, bottom = d.textbbox((0, 0), hhmm, font=big)[1::2]
    y = 40 - top
    d.text((M, y), hhmm, font=big, fill=0)
    d.text((M + d.textlength(hhmm, font=big) + 18, y + bottom - 72), ampm, font=render.ui_font(70, "medium"), fill=0)
    targets.append({"action": "clock", "box": [0, 0, split, 40 + bottom - top + 10]})
    y = 40 + bottom - top + 24
    date = now.strftime("%A, %-d %B")
    d.text((M, y), date, font=render.ui_font(46, "semibold"), fill=0)
    y += 70
    line = clock.forecast_line(now)
    if line:
        d.text((M, y), line, font=_fit(d, line, 40, "regular", lw), fill=0)
        targets.append({"action": "wttr", "box": [0, y - 10, split, y + 60]})
        y += 66
    y += 20
    d.line((M, y, split - M, y), fill=0, width=3)
    y += 28

    # ---- left: whatever matters now (timer, music, coming up, to-dos), then feeds fill the rest
    days = _forecast()
    fy = h - M - 160 if days else h - M                 # the 3-day forecast sits at the bottom
    bottom_y = fy - 26
    head = render.ui_font(30, "bold")
    small = render.ui_font(34)
    semi = render.ui_font(34, "semibold")

    st = timer.load()                                   # 1. a running timer
    if timer.running(st) and bottom_y - y >= 100:
        left = int(timer.remaining(st) + 0.999)
        ends = datetime.fromtimestamp(st["ends"]).strftime("%-I:%M %p").lower()
        d.text((M, y), "TIMER", font=head, fill=0)
        t = "%d:%02d left" % (left // 60, left % 60) if left < 3600 else "%dh %02dm left" % (left // 3600, left % 3600 // 60)
        d.text((M, y + 40), t, font=render.ui_font(54, "bold"), fill=0)
        d.text((M + lw, y + 58), "done at " + ends, font=small, fill=0, anchor="ra")
        targets.append({"action": "timer", "box": [0, y - 10, split, y + 110]})
        y += 124

    music = _now_playing()                              # 2. music playing on Spotify
    if music and bottom_y - y >= 170:
        art = 150
        try:
            import show
            import spotify
            cover = spotify._art(music["art"]) if music.get("art") else None
            if cover:
                cover = show.to_16_grays(cover.resize((art, art), Image.LANCZOS))
                img.paste(cover, (M, y))
                d.rectangle((M, y, M + art, y + art), outline=0, width=2)
        except Exception as e:
            render.log("home: album art unavailable: %s" % e)
            cover = None
        tx = M + (art + 24 if cover else 0)
        tw = split - M - tx
        d.text((tx, y), "NOW PLAYING" + ("" if music["playing"] else "  ·  paused"), font=head, fill=0)
        d.text((tx, y + 42), _shorten(d, music["title"], semi, tw), font=semi, fill=0)
        d.text((tx, y + 88), _shorten(d, music["artist"], small, tw), font=small, fill=0)
        if music.get("duration"):
            frac = min(1, music["progress"] / music["duration"])
            _bar(d, tx, y + 136, tw - 110, frac)
            d.text((tx + tw, y + 128), "%d:%02d" % divmod(int(music["duration"] - music["progress"]), 60),
                   font=render.ui_font(24), fill=0, anchor="ra")
        targets.append({"action": "spotify", "box": [0, y - 10, split, y + art + 10]})
        y += art + 30

    items = [(_when(at, now), text) for at, text in _coming_up(now)]     # 3. coming up
    rows = min(len(items), max(0, int((bottom_y - y - 46) // 50)))
    if rows:
        d.text((M, y), "COMING UP", font=head, fill=0)
        y += 46
        for when, text in items[:rows]:
            d.text((M, y), when, font=semi, fill=0)
            wx = M + max(190, d.textlength(when, font=semi) + 24)
            d.text((wx, y), _shorten(d, text, small, split - M - wx), font=small, fill=0)
            y += 50
        y += 22

    todos = [t for t in todo.load() if not t.get("done")]               # 4. to-dos, if any
    rows = min(len(todos), max(0, int((bottom_y - y - 46) // 50)))
    if rows:
        ty = y
        d.text((M, y), "TO-DO  ·  %d left" % len(todos), font=head, fill=0)
        y += 46
        for t in todos[:rows]:
            d.rectangle((M, y + 6, M + 28, y + 34), outline=0, width=3)
            d.text((M + 46, y), _shorten(d, t["text"], small, lw - 46), font=small, fill=0)
            y += 50
        targets.append({"action": "todo", "box": [0, ty - 10, split, y]})
        y += 22

    y = _feeds(img, d, y, bottom_y, lw, now, targets)                  # 5. feeds fill what's left

    if days:
        d.line((M, fy - 16, split - M, fy - 16), fill=0, width=3)
        cw = lw / len(days)
        for k, (name, hi, lo, desc, rain) in enumerate(days):
            cx = M + k * cw
            d.text((cx, fy + 4), name, font=render.ui_font(30, "bold"), fill=0)
            d.text((cx, fy + 44), "%s° / %s°" % (hi, lo), font=render.ui_font(34, "semibold"), fill=0)
            d.text((cx, fy + 88), _shorten(d, desc, render.ui_font(24), cw - 16), font=render.ui_font(24), fill=0)
            d.text((cx, fy + 118), "rain %s%%" % rain, font=render.ui_font(24), fill=0)
        targets.append({"action": "wttr", "box": [0, fy - 16, split, h]})

    # ---- right: the Pi
    x0, x1 = split + 10, w - M
    d.rounded_rectangle((x0, M - 14, x1, h - M + 14), radius=26, outline=0, width=4)
    px, pw = x0 + 30, x1 - x0 - 60
    py = M + 14
    try:
        pi = _pi_now()
    except Exception as e:                          # (e.g. not on the Pi)
        render.log("home: Pi readings unavailable: %s" % e)
        pi = None
    import socket
    d.text((px, py), socket.gethostname(), font=render.ui_font(40, "bold"), fill=0)
    if pi:
        d.text((x1 - 30, py + 8), "up " + pi["up"], font=render.ui_font(28), fill=0, anchor="ra")
        py += 72
        rows = [("CPU", "%.0f%%" % pi["cpu"], pi["cpu"] / 100),
                ("Temperature", "%.0f°C" % pi["temp"] if pi["temp"] is not None else "?",
                 ((pi["temp"] or 0) - 30) / 55),
                ("Memory", "%.1f / %.0f GB" % (pi["mem"][0] / 2 ** 30, pi["mem"][1] / 2 ** 30), pi["mem"][0] / pi["mem"][1]),
                ("Disk", "%.0f%% full" % (pi["disk"] * 100), pi["disk"])]
        lf, vf = render.ui_font(30), render.ui_font(34, "bold")
        for label, value, frac in rows:
            d.text((px, py), label, font=lf, fill=0)
            d.text((px + pw, py - 3), value, font=vf, fill=0, anchor="ra")
            _bar(d, px, py + 44, pw, frac)
            py += 82
        d.text((px, py), "Network now", font=lf, fill=0)
        py += 40
        d.text((px, py), "↓ %s   ↑ %s" % (health.speed(pi["down_bps"]), health.speed(pi["up_bps"])),
               font=render.ui_font(32, "semibold"), fill=0)
        py += 58
        pts = [(t, c) for t, c, *_ in pi["hist"] if c is not None]
        bottom_room = h - M - 60 - 40 - (44 if pi["throttled"] else 0)
        if len(pts) > 2 and bottom_room - py > 120:  # CPU, last hour: real readings only
            gx0, gx1, gy0, gy1 = px, px + pw, py + 34, bottom_room - 10
            d.text((px, py), "CPU, last hour", font=render.ui_font(24), fill=0)
            d.text((gx1, py), "100%", font=render.ui_font(20), fill=0, anchor="ra")
            d.rectangle((gx0, gy0, gx1, gy1), outline=0, width=2)
            span = 3600.0
            run, prev_t = [], None
            for t, c in pts:                        # a gap in the readings is a gap in the line
                if prev_t is not None and t - prev_t > 120:
                    if len(run) > 1:
                        d.line(run, fill=0, width=3)
                    run = []
                run.append((gx1 - (gx1 - gx0) * min(span, time.time() - t) / span,
                            gy1 - (gy1 - gy0) * min(100, c) / 100))
                prev_t = t
            if len(run) > 1:
                d.line(run, fill=0, width=3)
            py = gy1 + 22
        if pi["throttled"]:
            d.text((px, py), "⚠ " + pi["throttled"], font=render.ui_font(28, "bold"), fill=0)
            py += 44
    else:
        py += 72
        d.text((px, py), "Pi readings unavailable", font=render.ui_font(30), fill=0)
    # the Kindle
    ks = kindle.status()
    via = kindle.link_via()
    ky = h - M - 60
    d.line((px, ky - 24, px + pw, ky - 24), fill=0, width=2)
    kt = "Kindle  ·  %s" % ({"usb": "USB", "wifi": "Wi-Fi"}.get(via, "not seen lately"))
    d.text((px, ky), kt, font=render.ui_font(30, "semibold"), fill=0)
    if ks["battery"] is not None:
        kindle.draw_battery(d, px + pw, ky + 2, height=30)
    targets.append({"action": "stats", "box": [x0, 0, w, h]})

    with open(LAYOUT_FILE, "w") as f:
        json.dump(targets, f)
    return img


def hit(cx, cy):
    try:
        with open(LAYOUT_FILE) as f:
            targets = json.load(f)
    except (OSError, ValueError):
        return None
    for t in targets:
        x0, y0, x1, y1 = t["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return t["action"]
    return None
