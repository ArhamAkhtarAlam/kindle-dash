"""Clock screen: an analogue face and/or the big digital time, the date, a one-line forecast
and the next reminder. Redrawn every minute by render.py while it's showing.

Styles (out/clock_style.json): "both" (face + digital, the default), "analog", "digital".
Tap the clock or run `kindle clock analog|digital|both` to change it."""

import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageDraw

import kindle
import reminders
import render

J1_CACHE = os.path.join(render.OUT_DIR, "weather_j1.json")
STYLE_FILE = os.path.join(render.OUT_DIR, "clock_style.json")
STYLES = ("both", "analog", "digital")


def style():
    try:
        with open(STYLE_FILE) as f:
            s = json.load(f).get("style")
        return s if s in STYLES else STYLES[0]
    except (OSError, ValueError, AttributeError):
        return STYLES[0]


def set_style(s):
    if s == "next":
        s = STYLES[(STYLES.index(style()) + 1) % len(STYLES)]
    if s not in STYLES:
        return None
    with open(STYLE_FILE + ".tmp", "w") as f:
        json.dump({"style": s}, f)
    os.replace(STYLE_FILE + ".tmp", STYLE_FILE)
    return s


def face(radius, now):
    """An analogue clock face (hour + minute hands) as a 2*radius square image, drawn at 3x
    and scaled down so the edges are smooth on the Kindle."""
    k = 3
    size = 2 * radius * k
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    c, r = size / 2, radius * k
    d.ellipse((c - r + 6 * k, c - r + 6 * k, c + r - 6 * k, c + r - 6 * k), outline=0, width=9 * k)
    for i in range(60):
        a = math.radians(i * 6)
        hour = i % 5 == 0
        r0 = r - (52 if i % 15 == 0 else 40 if hour else 26) * k
        r1 = r - 22 * k
        width = (11 if i % 15 == 0 else 7 if hour else 3) * k
        d.line((c + r0 * math.sin(a), c - r0 * math.cos(a), c + r1 * math.sin(a), c - r1 * math.cos(a)),
               fill=0, width=int(width))
    num = render.ui_font(int(r * 0.15), "semibold")
    for n in range(1, 13):
        a = math.radians(n * 30)
        rr = r * 0.72
        d.text((c + rr * math.sin(a), c - rr * math.cos(a)), str(n), font=num, fill=0, anchor="mm")

    def hand(angle, length, width, tail):
        a = math.radians(angle)
        sx, sy = math.sin(a), -math.cos(a)                 # along the hand
        px, py = -sy, sx                                   # across it
        tip = (c + sx * length, c + sy * length)
        back = (c - sx * tail, c - sy * tail)
        d.polygon([(back[0] + px * width / 2, back[1] + py * width / 2),
                   (tip[0] + px * width / 5, tip[1] + py * width / 5),
                   (tip[0] - px * width / 5, tip[1] - py * width / 5),
                   (back[0] - px * width / 2, back[1] - py * width / 2)], fill=0)

    minutes = now.minute + now.second / 60
    hand((now.hour % 12 + minutes / 60) * 30, r * 0.50, 30 * k, r * 0.10)
    hand(minutes * 6, r * 0.80, 20 * k, r * 0.12)
    d.ellipse((c - 20 * k, c - 20 * k, c + 20 * k, c + 20 * k), fill=0)
    d.ellipse((c - 7 * k, c - 7 * k, c + 7 * k, c + 7 * k), fill=255)
    return img.resize((2 * radius, 2 * radius), Image.LANCZOS)


def forecast_line(now=None):
    """'31° Partly cloudy · rain 46% tonight' from wttr.in's JSON (cached like the weather)."""
    now = now or datetime.now()
    data = None
    try:
        if time.time() - os.path.getmtime(J1_CACHE) < render.WEATHER_CACHE_MINUTES * 60:
            with open(J1_CACHE) as f:
                data = json.load(f)
    except (OSError, ValueError):
        pass
    if data is None:
        try:
            url = "https://wttr.in/%s?format=j1" % urllib.parse.quote(render.LOCATION)
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=render.HTTP_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            with open(J1_CACHE + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(J1_CACHE + ".tmp", J1_CACHE)
        except Exception as e:
            render.log("clock: forecast fetch failed: %s" % e)
            try:
                with open(J1_CACHE) as f:                  # stale copy is better than none
                    data = json.load(f)
            except (OSError, ValueError):
                return ""
    try:
        cur = data["current_condition"][0]
        line = "%s° %s" % (cur["temp_C"], cur["weatherDesc"][0]["value"].strip())
        hourly = data["weather"][0]["hourly"]
        later = [int(hr["chanceofrain"]) for hr in hourly if int(hr["time"]) // 100 >= now.hour - 2]
        if later:
            line += " · rain %d%% %s" % (max(later), "tonight" if now.hour >= 17 else "today")
        return line
    except (KeyError, IndexError, ValueError):
        return ""


def fit(d, text, size, weight, width):
    """The UI font at size, made smaller until text fits width."""
    f = render.ui_font(size, weight)
    while d.textlength(text, font=f) > width and size > 20:
        size -= 2
        f = render.ui_font(size, weight)
    return f


def show_lines(text, font, width, d):
    """Split text into lines that fit width (at the " · " breaks first, then words)."""
    out = []
    for part in text.split(" · "):
        cur = ""
        for word in part.split():
            trial = (cur + " " + word).strip()
            if cur and d.textlength(trial, font=font) > width:
                out.append(cur)
                cur = word
            else:
                cur = trial
        out.append(cur)
    return out


def draw(w, h, now=None):
    now = now or datetime.now()
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)

    kind = style()
    date = now.strftime("%A, %-d %B")
    hhmm = now.strftime("%-I:%M")
    ampm = now.strftime("%p").lower()
    line = forecast_line(now)
    base = h - 110

    if kind == "digital":
        date_font = render.ui_font(58, "medium")
        d.text(((w - d.textlength(date, font=date_font)) / 2, 90), date, font=date_font, fill=0)
        big = render.ui_font(400, "medium")
        ampm_font = render.ui_font(96, "medium")
        tw, aw = d.textlength(hhmm, font=big), d.textlength(ampm, font=ampm_font)
        x = (w - tw - 24 - aw) / 2
        top, bottom = d.textbbox((0, 0), hhmm, font=big)[1::2]
        y = 470 - (top + bottom) / 2                     # digits centred on y=470
        d.text((x, y), hhmm, font=big, fill=0)
        d.text((x + tw + 24, y + bottom - ampm_font.size * 1.02), ampm, font=ampm_font, fill=0)
        if line:
            f = render.ui_font(56)
            d.text(((w - d.textlength(line, font=f)) / 2, 760), line, font=f, fill=0)
    elif kind == "analog":
        radius = 420
        cx = w // 2
        img.paste(face(radius, now), (cx - radius, (base - 2 * radius) // 2))
        room = cx - radius - 60 + 40                     # the corners beside the round face

        def corner(lines, x, y, right=False):
            for text, size, weight in lines:
                f = fit(d, text, size, weight, room)
                d.text((x - d.textlength(text, font=f) if right else x, y), text, font=f, fill=0)
                y += size + 18

        corner([(now.strftime("%A"), 48, "semibold"), (now.strftime("%-d %B"), 48, "medium")], 60, 50)
        corner([(hhmm + " " + ampm, 64, "medium")], w - 60, 50, right=True)
        if line:
            parts = line.split(" · ")
            corner([(p_, 40, "regular") for p_ in parts[:2]], 60, base - 30 - len(parts[:2]) * 58)
    else:                                                # both: face left, digital right
        radius = 400
        fx = 50
        img.paste(face(radius, now), (fx, (base - 2 * radius) // 2))
        x0 = fx + 2 * radius + 60
        col = w - 50 - x0
        date_font = fit(d, date, 50, "medium", col)
        big = render.ui_font(200, "medium")
        ampm_font = render.ui_font(64, "medium")
        while d.textlength(hhmm, font=big) + 16 + d.textlength(ampm, font=ampm_font) > col and big.size > 80:
            big = render.ui_font(big.size - 10, "medium")
        top, bottom = d.textbbox((0, 0), hhmm, font=big)[1::2]
        y = base // 2 - (top + bottom) / 2 - 40          # digits a bit above the face's centre
        d.text((x0, y + top - 90), date, font=date_font, fill=0)
        d.text((x0, y), hhmm, font=big, fill=0)
        d.text((x0 + d.textlength(hhmm, font=big) + 16, y + bottom - ampm_font.size * 1.02), ampm,
               font=ampm_font, fill=0)
        if line:
            f = render.ui_font(44)
            for n, text in enumerate(show_lines(line, f, col, d)[:3]):
                d.text((x0, y + bottom + 50 + n * 58), text, font=f, fill=0)
    d.line((60, base, w - 60, base), fill=0, width=3)
    nxt = reminders.next_due(now)
    try:                                          # or the next calendar event, if sooner
        import cal
        ev = cal.next_event(now)
        if ev and (not nxt or ev["start"] < nxt[0]):
            nxt = (ev["start"], ev["title"])
    except Exception as e:
        render.log("clock: calendar unavailable: %s" % e)
    small = render.ui_font(40)
    if nxt:
        at, text = nxt
        day = ("today" if at.date() == now.date() else
               "tomorrow" if (at.date() - now.date()).days == 1 else at.strftime("%a"))
        label = "Next: %s · %s %s" % (text, at.strftime("%-I:%M%p").lower(), day)
        while d.textlength(label, font=small) > w - 520 and len(text) > 4:
            text = text[:-2]
            label = "Next: %s… · %s %s" % (text.rstrip(), at.strftime("%-I:%M%p").lower(), day)
        d.text((60, base + 30), label, font=small, fill=0)
    kindle.draw_battery(d, w - 60, base + 34, height=34)
    return img
