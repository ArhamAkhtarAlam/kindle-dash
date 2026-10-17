"""Hackers: today's greatest hits from the honeypot (~/honeypot on the Pi). The funniest
password a bot tried today in huge type, today's numbers, the most-tried passwords and the
last thing typed into the fake shell. Redrawn every REDRAW_MINUTES while it's showing.

Reads ~/honeypot/honeypot.db (written by the honeypot's trap.py and dash.py) read-only.
"""

import os
import random
import sqlite3
import time
from datetime import datetime

from PIL import Image, ImageDraw

import render

DB_FILE = os.path.expanduser("~/honeypot/honeypot.db")
REDRAW_MINUTES = 5
M = 48

# Passwords too common to be "funny": they're every bot's first ten guesses.
BORING = {"123456", "1234", "12345", "123", "password", "admin", "root", "toor", "test", "guest",
          "user", "qwerty", "111111", "000000", "1qaz2wsx", "123456789", "12345678", "abc123",
          "admin123", "root123", "pass", "default", "ubnt", "support", "raspberry", "", "1", "oracle",
          "password1", "letmein", "changeme", "P@ssw0rd", "123123", "1234567", "admin@123", "Passw0rd"}


def _connect():
    return sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True, timeout=5)


def _score(pw, n):
    """Higher = funnier: wordy, unusual, tried only a few times."""
    if pw in BORING or pw.lower() in BORING or not (5 <= len(pw) <= 32):
        return 0
    letters = sum(c.isalpha() for c in pw)
    if letters < 3:
        return 0
    s = min(len(pw), 20) + letters
    s += 8 if " " in pw else 0
    s += 5 if any(c in "!?@#$_-." for c in pw) else 0
    s -= 3 * min(n, 5)                              # the less common, the funnier
    return max(s, 1)


def todays_hits(now=None):
    """Everything the screen shows, from midnight today. None if the honeypot isn't there."""
    now = now or datetime.now()
    day = datetime(now.year, now.month, now.day).timestamp()
    try:
        con = _connect()
    except sqlite3.Error:
        return None
    try:
        one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
        logins = "kind IN ('login_failed','login_success')"
        pws = con.execute("""SELECT password, COUNT(*), MIN(ip) FROM ssh WHERE %s AND ts >= ?
                             AND password IS NOT NULL GROUP BY password
                             UNION ALL SELECT password, COUNT(*), MIN(ip) FROM web WHERE ts >= ?
                             AND password IS NOT NULL GROUP BY password""" % logins, (day, day)).fetchall()
        merged = {}
        for pw, n, ip in pws:
            m = merged.setdefault(pw, [0, ip])
            m[0] += n
        top = sorted(merged.items(), key=lambda kv: -kv[1][0])[:8]
        funny = sorted(((_score(pw, n), pw, n, ip) for pw, (n, ip) in merged.items()), reverse=True)
        funny = [f for f in funny if f[0] > 0][:15]
        pick = random.Random(now.strftime("%Y%m%d")).choice(funny) if funny else None
        country = None
        if pick:
            r = con.execute("SELECT country FROM geo WHERE ip=?", (pick[3],)).fetchone()
            country = r[0] if r else None
        last_cmd = con.execute("SELECT detail, ts FROM ssh WHERE kind='command' ORDER BY id DESC LIMIT 1").fetchone()
        boss = con.execute("""SELECT g.country, COUNT(*) AS n FROM ssh s LEFT JOIN geo g ON g.ip=s.ip
                              WHERE s.%s AND s.ts >= ? GROUP BY s.ip ORDER BY n DESC LIMIT 1""" % logins,
                           (day,)).fetchone()
        return {
            "attempts": one("SELECT COUNT(*) FROM ssh WHERE %s AND ts >= ?" % logins, day),
            "web": one("SELECT COUNT(*) FROM web WHERE ts >= ?", day),
            "countries": one("""SELECT COUNT(DISTINCT g.cc) FROM geo g WHERE g.cc != '' AND g.ip IN
                                (SELECT ip FROM ssh WHERE ts >= ? UNION SELECT ip FROM web WHERE ts >= ?)""", day, day),
            "wasted": one("SELECT COALESCE(SUM(seconds), 0) FROM tarpit"),
            "top": [(pw, n) for pw, (n, _) in top],
            "pick": {"pw": pick[1], "n": pick[2], "country": country} if pick else None,
            "last_cmd": last_cmd,
            "boss": boss,
        }
    except sqlite3.Error as e:
        render.log("hackers: %s" % e)
        return None
    finally:
        con.close()


def _fit(d, text, size, weight, width, smallest=24):
    f = render.ui_font(size, weight)
    while d.textlength(text, font=f) > width and size > smallest:
        size -= 4
        f = render.ui_font(size, weight)
    return f


def _shorten(d, text, font, width):
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _dur(s):
    s = int(s)
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh %02dm" % (s // 3600, s % 3600 // 60)


def draw(w, h, now=None):
    now = now or datetime.now()
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    data = todays_hits(now)

    d.text((M, 36), "Hackers' greatest hits", font=render.ui_font(56, "bold"), fill=0)
    stamp = now.strftime("%A %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    small = render.ui_font(30)
    d.text((w - M - d.textlength(stamp, font=small), 56), stamp, font=small, fill=90)
    d.line((M, 120, w - M, 120), fill=0, width=3)

    if data is None or (not data["attempts"] and not data["web"] and not data["top"]):
        msg = "No hackers yet today" if data is not None else "The honeypot isn't set up"
        sub = "The honeypot is waiting. Bots usually show up within minutes." if data is not None \
            else "Run the honeypot setup on the Pi first."
        f, fs = render.ui_font(72, "bold"), render.ui_font(36)
        d.text(((w - d.textlength(msg, font=f)) / 2, h / 2 - 80), msg, font=f, fill=0)
        d.text(((w - d.textlength(sub, font=fs)) / 2, h / 2 + 20), sub, font=fs, fill=90)
        return img

    split = int(w * 0.60)
    lw = split - 2 * M

    # ---- left: password of the day
    y = 160
    label = render.ui_font(30, "bold")
    d.text((M, y), "PASSWORD OF THE DAY", font=label, fill=90)
    y += 56
    pick = data["pick"]
    if pick:
        text = "“%s”" % pick["pw"]
        f = _fit(d, text, 150, "bold", lw, smallest=44)
        top_, bottom = d.textbbox((0, 0), text, font=f)[1::2]
        d.text((M, y - top_), text, font=f, fill=0)
        y += bottom - top_ + 30
        who = "tried by a bot%s" % (" from " + pick["country"] if pick["country"] and pick["country"] != "Local network" else "")
        if pick["n"] > 1:
            who += ", %d times" % pick["n"]
        d.text((M, y), _shorten(d, who, render.ui_font(36), lw), font=render.ui_font(36), fill=0)
        y += 70
    else:
        d.text((M, y), "Only boring ones so far (123456, admin…)", font=render.ui_font(44, "semibold"), fill=0)
        y += 90

    # ---- left: today's numbers
    y = max(y, int(h * 0.55))
    boxes = [(str(data["attempts"]), "SSH tries"), (str(data["web"]), "web probes"),
             (str(data["countries"]), "countries"), (_dur(data["wasted"]), "time wasted")]
    bw = (lw - 3 * 20) / 4
    for i, (big, lab) in enumerate(boxes):
        x0 = M + i * (bw + 20)
        d.rounded_rectangle((x0, y, x0 + bw, y + 150), radius=20, outline=0, width=3)
        fb = _fit(d, big, 60, "bold", bw - 30)
        d.text((x0 + 18, y + 20), big, font=fb, fill=0)
        d.text((x0 + 18, y + 100), _shorten(d, lab, render.ui_font(26), bw - 30), font=render.ui_font(26), fill=0)
    y += 190

    # ---- left: last command / most persistent
    if data["last_cmd"]:
        cmd, ts = data["last_cmd"]
        d.text((M, y), "LAST THING TYPED IN THE FAKE SHELL", font=label, fill=90)
        mono = render.ui_font(38, "semibold")
        d.text((M, y + 46), _shorten(d, "$ " + cmd, mono, lw), font=mono, fill=0)
        y += 120
    if data["boss"] and data["boss"][1] > 1:
        country, n = data["boss"]
        line = "Most stubborn bot today: %s, %d tries" % (country or "somewhere", n)
        d.text((M, y), _shorten(d, line, render.ui_font(32), lw), font=render.ui_font(32), fill=0)

    # ---- right: most tried passwords today
    d.line((split, 150, split, h - 50), fill=0, width=2)
    x = split + M
    rw = w - x - M
    d.text((x, 160), "MOST TRIED TODAY", font=label, fill=90)
    y = 216
    row = render.ui_font(38, "semibold")
    num = render.ui_font(34)
    for i, (pw, n) in enumerate(data["top"]):
        if y > h - 80:
            break
        count = "%d×" % n
        cw = d.textlength(count, font=num)
        d.text((x, y), "%d" % (i + 1), font=num, fill=90)
        d.text((x + 50, y - 3), _shorten(d, pw or "(empty)", row, rw - 70 - cw), font=row, fill=0)
        d.text((w - M - cw, y), count, font=num, fill=0)
        y += 64
    return img
