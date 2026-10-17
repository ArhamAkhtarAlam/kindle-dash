#!/usr/bin/env python3
"""Spotify now-playing screen with playback buttons (Spotify Web API, standard library only).

One-time setup (the Mac's "kindle spotify setup" does steps 2-3 for you):
  1. developer.spotify.com/dashboard -> Create app, Redirect URI http://127.0.0.1:8888/callback,
     tick "Web API". Copy the Client ID and Client secret.
  2. spotify.py setup CLIENT_ID CLIENT_SECRET   -> prints the login URL
  3. log in, then:  spotify.py code CODE         (CODE from the redirect URL)

Then:  spotify.py status | play | pause | toggle | next | prev | volume N
Controls need Spotify Premium; the now-playing screen works on any account.
Tokens are kept in spotify/auth.json (readable only by you).
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

import render

SP_DIR = os.path.join(render.BASE_DIR, "spotify")
AUTH_FILE = os.path.join(SP_DIR, "auth.json")
ART_DIR = os.path.join(SP_DIR, "art")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "spotify_layout.json")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "user-read-recently-played user-top-read")   # last two are for ~/wrapped
M = 60
RESTART_AFTER_SEC = 3   # ⏮ restarts the song if it's further in than this, else goes back one
POLL_SECONDS = 10       # ask Spotify this often (plus after each button and at each song's end);
                        # in between, the progress bar runs on its own clock
PROGRESS_TICK = 1.0     # how often the progress bar/time is redrawn (seconds)


class SpotifyError(Exception):
    pass


# ------------------------------- auth -------------------------------

def _auth():
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_auth(a):
    os.makedirs(SP_DIR, exist_ok=True)
    with open(AUTH_FILE + ".tmp", "w") as f:
        json.dump(a, f)
    os.chmod(AUTH_FILE + ".tmp", 0o600)
    os.replace(AUTH_FILE + ".tmp", AUTH_FILE)


def configured():
    return bool(_auth().get("refresh_token"))


def login_url(client_id, client_secret):
    _save_auth({"client_id": client_id, "client_secret": client_secret})
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES})


def _token_request(fields):
    a = _auth()
    basic = base64.b64encode(("%s:%s" % (a["client_id"], a["client_secret"])).encode()).decode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                 data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Authorization": "Basic " + basic,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=render.HTTP_TIMEOUT_SEC) as resp:
        tok = json.loads(resp.read())
    a.update(access_token=tok["access_token"], expires=time.time() + tok["expires_in"] - 60)
    if tok.get("refresh_token"):
        a["refresh_token"] = tok["refresh_token"]
    _save_auth(a)


def exchange_code(code):
    _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT})


def _token():
    a = _auth()
    if not a.get("refresh_token"):
        raise SpotifyError("not set up yet: run kindle spotify setup")
    if time.time() >= a.get("expires", 0):
        _token_request({"grant_type": "refresh_token", "refresh_token": a["refresh_token"]})
        a = _auth()
    return a["access_token"]


def api(method, path, params=None):
    """Call the Web API. Returns parsed JSON, or None for empty replies."""
    url = "https://api.spotify.com/v1" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=b"" if method in ("PUT", "POST") else None,
                                 headers={"Authorization": "Bearer " + _token()})
    try:
        with urllib.request.urlopen(req, timeout=render.HTTP_TIMEOUT_SEC) as resp:
            body = resp.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 403 and "PREMIUM" in detail.upper():
            raise SpotifyError("Spotify only allows remote control with Premium")
        if e.code == 404:
            raise SpotifyError("no active Spotify device: start playing on your phone or Mac first")
        raise SpotifyError("Spotify said %d: %s" % (e.code, detail[:200]))


# ----------------------------- playback -----------------------------

def state():
    """Current playback, or None if nothing is playing anywhere."""
    s = api("GET", "/me/player")
    if not s or not s.get("item"):
        return None
    item = s["item"]
    images = (item.get("album") or {}).get("images") or (item.get("show") or {}).get("images") or []
    return {"playing": s.get("is_playing", False),
            "title": item.get("name", ""),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])) or
                      (item.get("show") or {}).get("name", ""),
            "album": (item.get("album") or {}).get("name", ""),
            "art": images[0]["url"] if images else None,
            "progress": (s.get("progress_ms") or 0) / 1000, "duration": item.get("duration_ms", 0) / 1000,
            "volume": (s.get("device") or {}).get("volume_percent"),
            "device": (s.get("device") or {}).get("name", "")}


_last = {"s": None, "at": 0.0}                     # last answer from Spotify, and when


def current(force=False):
    """Now playing, with progress worked out locally: last known position + time since,
    while playing. Asks Spotify only every POLL_SECONDS, when forced, or when the song should
    have ended (to see what's next)."""
    now = time.time()
    s = _last["s"]
    due = force or now - _last["at"] >= POLL_SECONDS
    if s and s["playing"] and s["duration"] and s["progress"] + (now - _last["at"]) >= s["duration"] - 0.3:
        due = True
    if due:
        s = state()
        _last.update(s=s, at=time.time())
    if not s:
        return None
    s = dict(s)
    if s["playing"]:
        s["progress"] = min(s["duration"] or 0, s["progress"] + (time.time() - _last["at"]))
    return s


def control(action, value=None):
    if action == "play":
        api("PUT", "/me/player/play")
    elif action == "pause":
        api("PUT", "/me/player/pause")
    elif action == "toggle":
        s = state()
        api("PUT", "/me/player/pause" if s and s["playing"] else "/me/player/play")
    elif action == "next":
        api("POST", "/me/player/next")
    elif action in ("prev", "previous"):
        # Like a music player's ⏮: more than a few seconds in, go back to the start of this
        # song; press again (now near the start) to go to the previous song.
        s = state()
        if s and s["progress"] > RESTART_AFTER_SEC:
            api("PUT", "/me/player/seek", {"position_ms": 0})
        else:
            api("POST", "/me/player/previous")
    elif action in ("volume", "vol+", "vol-"):
        if value is None:
            s = state()
            cur = (s or {}).get("volume") or 50
            value = cur + (10 if action == "vol+" else -10)
        api("PUT", "/me/player/volume", {"volume_percent": max(0, min(100, int(value)))})
    else:
        raise SpotifyError("unknown action %r" % action)
    time.sleep(0.35)                      # give Spotify a moment before we read it back
    current(force=True)                   # resync the local clock


_COVERS = {}            # the current cover, ready to paste (the updater redraws every 2 s)


def _art(url):
    os.makedirs(ART_DIR, exist_ok=True)
    path = os.path.join(ART_DIR, url.rsplit("/", 1)[-1] + ".jpg")
    if not os.path.exists(path):
        with urllib.request.urlopen(url, timeout=render.HTTP_TIMEOUT_SEC) as resp, open(path, "wb") as f:
            f.write(resp.read())
        for old in sorted(os.listdir(ART_DIR), key=lambda n: os.path.getmtime(os.path.join(ART_DIR, n)))[:-20]:
            os.remove(os.path.join(ART_DIR, old))            # keep the last 20 covers
    return Image.open(path).convert("L")


# ------------------------------ drawing -----------------------------

def _mmss(sec):
    return "%d:%02d" % (sec // 60, sec % 60)


def _icon(d, kind, cx, cy, s, fill):
    """Simple drawn transport icons (no font glyphs needed)."""
    if kind == "play":
        d.polygon([(cx - s * 0.4, cy - s * 0.55), (cx - s * 0.4, cy + s * 0.55), (cx + s * 0.55, cy)], fill=fill)
    elif kind == "pause":
        d.rectangle((cx - s * 0.45, cy - s * 0.5, cx - s * 0.12, cy + s * 0.5), fill=fill)
        d.rectangle((cx + s * 0.12, cy - s * 0.5, cx + s * 0.45, cy + s * 0.5), fill=fill)
    elif kind in ("next", "prev"):
        sign = 1 if kind == "next" else -1
        for off in (-0.45, 0.05):                     # two triangles, then a bar
            x = cx + sign * off * s
            d.polygon([(x, cy - s * 0.45), (x, cy + s * 0.45), (x + sign * s * 0.45, cy)], fill=fill)
        bx = cx + sign * s * 0.55
        d.rectangle((bx - s * 0.06, cy - s * 0.45, bx + s * 0.06, cy + s * 0.45), fill=fill)


def draw(w, h, s, error=None):
    """Now-playing screen. Returns (image, buttons) with buttons = [{"action", "box"}]."""
    import show                                        # (show imports this module)
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    big = render.ui_font(60, "bold")
    mid = render.ui_font(42)
    small = render.ui_font(30)
    if error or not s:
        msg = error or "Nothing playing on Spotify"
        hint = "" if error else "Start something on your phone or Mac, then tap here"
        d.text((M, h / 2 - 80), msg, font=render.ui_font(54, "semibold"), fill=0)
        d.text((M, h / 2), hint, font=small, fill=0)
        return img, [{"action": "refresh", "box": (0, 0, w, h)}]

    art_size = ART
    ay = (h - art_size) // 2
    if s.get("art"):
        try:
            key = (s["art"], art_size, render.DITHER)
            if key not in _COVERS:                     # resize + dither once per cover
                _COVERS.clear()
                cover = _art(s["art"]).resize((art_size, art_size), Image.Resampling.LANCZOS)
                _COVERS[key] = show.to_16_grays(cover)
            img.paste(_COVERS[key], (M, ay))
        except Exception as e:
            render.log("spotify: cover art failed: %s" % e)
    d.rectangle((M, ay, M + art_size, ay + art_size), outline=0, width=3)

    def one_line(text, font, width):
        lines = show.wrap(text, font, width)
        if len(lines) <= 1:
            return text
        line = lines[0]
        while line and d.textlength(line + "…", font=font) > width:
            line = line[:-1]
        return line.rstrip() + "…"

    x0, x1 = M + art_size + 70, w - M
    y = 90
    title = show.wrap(s["title"], big, x1 - x0)
    if len(title) > 2:
        title = title[:1] + [one_line(" ".join(title[1:]), big, x1 - x0)]
    for line in title:
        d.text((x0, y), line, font=big, fill=0)
        y += 74
    y += 8
    d.text((x0, y), one_line(s["artist"], mid, x1 - x0), font=mid, fill=0)
    y += 58
    if s["album"]:
        d.text((x0, y), one_line(s["album"], small, x1 - x0), font=small, fill=90)

    py = PROGRESS_Y
    img.paste(progress_strip(w, s), progress_box(w)[:2])

    buttons = []
    by, bh = 600, 160
    bw = (x1 - x0 - 2 * 30) / 3
    for i, (action, icon) in enumerate((("prev", "prev"), ("toggle", "pause" if s["playing"] else "play"),
                                        ("next", "next"))):
        bx = x0 + i * (bw + 30)
        main = action == "toggle"
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=28, outline=0, width=4,
                            fill=0 if main else 255)
        _icon(d, icon, bx + bw / 2, by + bh / 2, 70, 255 if main else 0)
        buttons.append({"action": action, "box": (bx, by, bx + bw, by + bh)})

    vy = by + bh + 40
    vw = 140
    vol = s.get("volume")
    label = "Volume %s" % ("?" if vol is None else "%d%%" % vol)
    for action, sign, bx in (("vol-", "–", x0), ("vol+", "+", x1 - vw)):
        d.rounded_rectangle((bx, vy, bx + vw, vy + 90), radius=20, outline=0, width=4)
        f = render.ui_font(60, "semibold")
        d.text((bx + (vw - d.textlength(sign, font=f)) / 2, vy + 4), sign, font=f, fill=0)
        buttons.append({"action": action, "box": (bx, vy, bx + vw, vy + 90)})
    d.text(((x0 + x1 - d.textlength(label, font=mid)) / 2, vy + 20), label, font=mid, fill=0)
    if s.get("device"):
        dev = "on " + s["device"]
        d.text((x0, 40), dev, font=small, fill=90)
    return img, buttons


PROGRESS_Y = 470
ART = 600


def art_box(w, h):
    ay = (h - ART) // 2
    return (M, ay, M + ART, ay + ART)


def progress_box(w):
    """Canvas box of the progress bar + times, the strip the 1-second updates replace."""
    x0, x1 = M + ART + 70, w - M
    return (x0 - 12, PROGRESS_Y - 12, x1 + 12, PROGRESS_Y + 76)


def progress_strip(w, s):
    """The progress bar and times as their own small image (pure black/white, so the fast
    e-ink refresh shows it cleanly)."""
    bx0, by0, bx1, by1 = progress_box(w)
    img = Image.new("L", (bx1 - bx0, by1 - by0), 255)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    x0, x1, py = 12, bx1 - bx0 - 12, 12
    small = render.ui_font(30)
    frac = max(0, min(1, s["progress"] / s["duration"])) if s["duration"] else 0
    d.rounded_rectangle((x0, py, x1, py + 14), radius=7, outline=0, width=2)
    if frac > 0.01:
        d.rounded_rectangle((x0, py, x0 + (x1 - x0) * frac, py + 14), radius=7, fill=0)
    d.text((x0, py + 26), _mmss(s["progress"]), font=small, fill=0)
    total = _mmss(s["duration"])
    d.text((x1 - d.textlength(total, font=small), py + 26), total, font=small, fill=0)
    return img


def save_layout(buttons, s=None):
    with open(LAYOUT_FILE, "w") as f:
        json.dump(buttons, f)
    with open(LAYOUT_FILE + ".track", "w") as f:          # which song is on screen
        f.write("" if not s else "%s\n%s" % (s.get("title", ""), s.get("artist", "")))


def hit(cx, cy):
    try:
        with open(LAYOUT_FILE) as f:
            buttons = json.load(f)
    except (OSError, ValueError):
        return None
    for b in buttons:
        x0, y0, x1, y1 = b["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return b["action"]
    return None


def main():
    args = sys.argv[1:]
    try:
        if args[:1] == ["setup"] and len(args) == 3:
            print(login_url(args[1], args[2]))
        elif args[:1] == ["code"] and len(args) == 2:
            exchange_code(args[1])
            print("Spotify connected")
        elif args[:1] == ["status"] or not args:
            s = state()
            print("nothing playing" if not s else "%s %s - %s (%s, volume %s%%)" % (
                "playing" if s["playing"] else "paused", s["title"], s["artist"], s["device"], s["volume"]))
        elif args[0] in ("play", "pause", "toggle", "next", "prev", "previous", "vol+", "vol-"):
            control(args[0])
            print("ok")
        elif args[0] == "volume" and len(args) == 2:
            control("volume", int(args[1].rstrip("%")))
            print("volume %s%%" % args[1].rstrip("%"))
        else:
            print(__doc__)
            return 2
    except SpotifyError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
