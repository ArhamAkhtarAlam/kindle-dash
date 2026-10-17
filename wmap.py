"""Weather map: your state or country with a dot where you are, showing rain, clouds, wind or
temperature for any hour from 6 hours ago to 24 hours ahead (slider at the bottom), so you can
watch weather move across. In the menu under Weather > Map, or `kindle weathermap`.

Data: Open-Meteo (free, no key): one request for a grid of up to MAX_POINTS points across the
view, refreshed every REFRESH_MINUTES while the map is showing (the forecast models behind it
update every 1-6 hours). Borders: Natural Earth (public domain; for many countries its
"point of view" version, with disputed borders drawn as that country does), cut down to your
country once and kept in maps/.

Your place comes from render.LOCATION (looked up once with Open-Meteo's geocoding).

  out/wmap.json          area (state/country), layer, hour offset, what's drawn
  out/wmap_<area>.json   the downloaded grid
  maps/place.json        your location; maps/<CC>.json  borders for your country
"""

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw

import render

MAPS_DIR = os.path.join(render.BASE_DIR, "maps")
SRC_DIR = os.path.join(MAPS_DIR, "src")
PLACE_FILE = os.path.join(MAPS_DIR, "place.json")
STATE_FILE = os.path.join(render.OUT_DIR, "wmap.json")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "wmap_layout.json")
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/%s.geojson"
COUNTRIES = "ne_10m_admin_0_countries"
# Natural Earth also has "point of view" versions drawing disputed borders the way a given country
# officially does; used for these countries (ISO code -> file suffix)
COUNTRY_POV = {"IN": "ind", "PK": "pak", "CN": "chn", "TW": "twn", "IL": "isr", "PS": "pse", "TR": "tur",
               "GR": "grc", "RU": "rus", "UA": "ukr", "AR": "arg", "MA": "mar", "KR": "kor", "JP": "jpn",
               "VN": "vnm", "BD": "bgd", "NP": "npl", "SA": "sau", "EG": "egy", "ID": "idn", "BR": "bra",
               "DE": "deu", "FR": "fra", "GB": "gbr", "US": "usa", "IT": "ita", "ES": "esp", "PT": "prt",
               "NL": "nld", "PL": "pol", "SE": "swe"}
STATES = "ne_10m_admin_1_states_provinces"
MAX_POINTS = 300                               # grid size (each point counts toward Open-Meteo's free limit)
REFRESH_MINUTES = 60
PAST_HOURS, AHEAD_HOURS = 6, 24
LAYERS = [("rain", "Rain"), ("clouds", "Clouds"), ("wind", "Wind"), ("temp", "Temperature")]
AREAS = [("state", "State"), ("country", "Country")]
VARS = "precipitation,cloud_cover,wind_speed_10m,wind_direction_10m,temperature_2m"
TIMEOUT = 40                                   # Open-Meteo can take several seconds from here
PANEL_W, BAR_H = 270, 170
FRAME_SECONDS = 1.2                            # Play: one hour per this many seconds...
PLAY_LOOPS = 3                                 # ...round the whole range this many times, then stop


# ------------------------------ state -------------------------------

def load():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("area", "state")
    st.setdefault("layer", "rain")
    st.setdefault("offset", 0)
    return st


def save(st):
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kindle-dash weather map"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------ place & borders ---------------------

def place():
    """{"name", "lat", "lon", "cc", "admin1"} for render.LOCATION (looked up once)."""
    try:
        with open(PLACE_FILE) as f:
            p = json.load(f)
        if p.get("query") == render.LOCATION:
            return p
    except (OSError, ValueError):
        pass
    r = _get("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(render.LOCATION))
    if not r.get("results"):
        raise RuntimeError("couldn't find %r on the map" % render.LOCATION)
    g = r["results"][0]
    p = {"query": render.LOCATION, "name": g["name"], "lat": g["latitude"], "lon": g["longitude"],
         "cc": g.get("country_code", ""), "admin1": g.get("admin1", ""), "country": g.get("country", "")}
    os.makedirs(MAPS_DIR, exist_ok=True)
    with open(PLACE_FILE, "w") as f:
        json.dump(p, f)
    return p


def _rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [ring for poly in polys for ring in poly]


def _simplify(ring, tol):
    """Douglas-Peucker on a [[lon, lat], ...] ring (tol in degrees). A closed ring starts and
    ends on the same point, so it's split at the point farthest from the start first."""
    if len(ring) < 4:
        return ring
    if ring[0] == ring[-1]:
        x0, y0 = ring[0]
        far = max(range(len(ring)), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
        if 0 < far < len(ring) - 1:
            return _simplify(ring[:far + 1], tol)[:-1] + _simplify(ring[far:], tol)
    return _simplify_open(ring, tol)


def _simplify_open(ring, tol):
    if len(ring) < 3:
        return [[round(x, 3), round(y, 3)] for x, y in ring]
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = ring[a], ring[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = 0.0, None
        for i in range(a + 1, b):
            x, y = ring[i]
            dist = abs(dy * (x - x1) - dx * (y - y1)) / norm
            if dist > best:
                best, idx = dist, i
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [[round(x, 3), round(y, 3)] for (x, y), k in zip(ring, keep) if k]


def _download(name):
    path = os.path.join(SRC_DIR, name + ".geojson")
    if not os.path.exists(path):
        os.makedirs(SRC_DIR, exist_ok=True)
        render.log("weather map: downloading %s (once)" % name)
        req = urllib.request.Request(NE % name, headers={"User-Agent": "kindle-dash"})
        with urllib.request.urlopen(req, timeout=600) as r, open(path + ".tmp", "wb") as f:
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
        os.replace(path + ".tmp", path)
    with open(path) as f:
        return json.load(f)


def borders(cc):
    """{"country": rings, "states": {name: rings}, "neighbours": rings} for country code cc."""
    path = os.path.join(MAPS_DIR, "%s.json" % cc)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    countries = _download(COUNTRIES + ("_" + COUNTRY_POV[cc] if cc in COUNTRY_POV else ""))["features"]
    mine = [f for f in countries if cc in (f["properties"].get("ISO_A2"), f["properties"].get("ISO_A2_EH"))]
    if not mine:
        raise RuntimeError("no outline for country %s" % cc)
    country = [_simplify(r, 0.01) for f in mine for r in _rings(f["geometry"])]
    lons = [x for r in country for x, _ in r]
    lats = [y for r in country for _, y in r]
    box = (min(lons) - 15, min(lats) - 15, max(lons) + 15, max(lats) + 15)
    neighbours = []
    for f in countries:
        if f in mine:
            continue
        for r in _rings(f["geometry"]):
            if any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in r[::10] or r):
                neighbours.append(_simplify(r, 0.02))
    states = {}
    for f in _download(STATES)["features"]:
        if f["properties"].get("iso_a2") == cc:
            states[f["properties"]["name"]] = [_simplify(r, 0.005) for r in _rings(f["geometry"])]
    data = {"country": country, "states": states, "neighbours": neighbours}
    with open(path, "w") as f:
        json.dump(data, f)
    return data


# ------------------------------ view & data -------------------------

def view(area, w, h):
    """(lon0, lat0, lon1, lat1) for the area, widened to the map's shape."""
    p = place()
    b = borders(p["cc"])
    rings = b["states"].get(p["admin1"]) if area == "state" else b["country"]
    rings = rings or b["country"]
    lons = [x for r in rings for x, _ in r]
    lats = [y for r in rings for _, y in r]
    lon0, lon1, lat0, lat1 = min(lons), max(lons), min(lats), max(lats)
    pad = 0.08 if area == "state" else 0.03
    lon0, lon1 = lon0 - (lon1 - lon0) * pad, lon1 + (lon1 - lon0) * pad
    lat0, lat1 = lat0 - (lat1 - lat0) * pad, lat1 + (lat1 - lat0) * pad
    k = math.cos(math.radians((lat0 + lat1) / 2))
    span_x, span_y = (lon1 - lon0) * k, lat1 - lat0
    if span_x / span_y < w / h:                       # widen to the map's aspect ratio
        extra = (span_y * w / h / k - (lon1 - lon0)) / 2
        lon0, lon1 = lon0 - extra, lon1 + extra
    else:
        extra = (span_x * h / w - span_y) / 2
        lat0, lat1 = lat0 - extra, lat1 + extra
    return lon0, lat0, lon1, lat1


def _grid(box, w, h):
    """Cell-centre longitudes/latitudes of an nx x ny grid (nx*ny <= MAX_POINTS) over box."""
    nx = max(4, int(round(math.sqrt(MAX_POINTS * w / h))))
    ny = max(4, MAX_POINTS // nx)
    lon0, lat0, lon1, lat1 = box
    lons = [lon0 + (i + 0.5) * (lon1 - lon0) / nx for i in range(nx)]
    lats = [lat1 - (j + 0.5) * (lat1 - lat0) / ny for j in range(ny)]   # north to south
    return lons, lats


def data_file(area):
    return os.path.join(render.OUT_DIR, "wmap_%s.json" % area)


def cached(area):
    try:
        with open(data_file(area)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stale(area):
    d = cached(area)
    return not d or time.time() - d["fetched"] > REFRESH_MINUTES * 60


def fetch(area, w, h):
    """Download the grid for area (a few seconds). Returns the data."""
    box = view(area, w, h)
    lons, lats = _grid(box, w, h)
    pts = [(la, lo) for la in lats for lo in lons]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&hourly=%s"
           "&past_hours=%d&forecast_hours=%d&timezone=auto&wind_speed_unit=kmh" % (
               ",".join("%.2f" % la for la, _ in pts), ",".join("%.2f" % lo for _, lo in pts), VARS,
               PAST_HOURS, AHEAD_HOURS + 1))
    res = _get(url)
    if isinstance(res, dict):
        res = [res]
    times = res[0]["hourly"]["time"]
    out = {}
    for var in VARS.split(","):
        a = np.array([[v if v is not None else np.nan for v in r["hourly"][var]] for r in res], dtype=float)
        out[var] = a.reshape(len(lats), len(lons), len(times)).tolist()
    d = {"fetched": time.time(), "box": box, "lons": lons, "lats": lats, "times": times, "vars": out,
         "size": [w, h]}
    with open(data_file(area) + ".tmp", "w") as f:
        json.dump(d, f)
    os.replace(data_file(area) + ".tmp", data_file(area))
    render.log("weather map: fetched %d points x %d hours (%s)" % (len(pts), len(times), area))
    return d


_fetching = {}


def refresh_async(area, w, h, done=None):
    """Fetch in the background (once at a time); done() runs afterwards if it worked."""
    if _fetching.get(area):
        return
    _fetching[area] = True

    def run():
        try:
            fetch(area, w, h)
            if done:
                done()
        except Exception as e:
            render.log("weather map: fetch failed: %s" % e)
        finally:
            _fetching[area] = False
    threading.Thread(target=run, daemon=True).start()


# ------------------------------ drawing -----------------------------

def _hour_index(d, offset):
    now = datetime.now().strftime("%Y-%m-%dT%H:00")
    times = d["times"]
    base = times.index(now) if now in times else min(range(len(times)), key=lambda i: abs(
        datetime.fromisoformat(times[i]).timestamp() - time.time()))
    return max(0, min(len(times) - 1, base + offset)), base


def _field(d, var, t, mw, mh):
    a = np.array(d["vars"][var], dtype=np.float32)[:, :, t]
    if np.isnan(a).all():
        return None
    a = np.where(np.isnan(a), np.nanmean(a), a)
    return np.asarray(Image.fromarray(a, "F").resize((mw, mh), Image.BICUBIC))


def _shade(layer, d, t, mw, mh):
    """The weather layer as greys (white = nothing)."""
    if layer == "rain":
        f = _field(d, "precipitation", t, mw, mh)
        g = np.zeros((mh, mw), np.float32) if f is None else \
            np.clip(np.log1p(np.maximum(f, 0) * 4) / np.log1p(4 * 20), 0, 1) * (f >= 0.05)
        grey = 255 - 215 * g
    elif layer == "clouds":
        f = _field(d, "cloud_cover", t, mw, mh)
        grey = 255 - 1.3 * np.clip(f if f is not None else 0, 0, 100)
    elif layer == "temp":
        allv = np.array(d["vars"]["temperature_2m"], dtype=np.float32)
        lo, hi = np.nanmin(allv), np.nanmax(allv)
        f = _field(d, "temperature_2m", t, mw, mh)
        grey = 245 - 185 * np.clip((f - lo) / max(hi - lo, 1), 0, 1) if f is not None else 255
    else:                                             # wind: light shading by speed
        f = _field(d, "wind_speed_10m", t, mw, mh)
        grey = 255 - 90 * np.clip((f if f is not None else 0) / 50, 0, 1)
    return Image.fromarray(np.clip(grey, 0, 255).astype(np.uint8), "L")


def _proj(box, mw, mh):
    lon0, lat0, lon1, lat1 = box
    return lambda lon, lat: ((lon - lon0) / (lon1 - lon0) * mw, (lat1 - lat) / (lat1 - lat0) * mh)


def _outline(d, rings, xy, width, fill=0):
    for r in rings:
        pts = [xy(x, y) for x, y in r]
        if len(pts) > 1:
            d.line(pts + [pts[0]], fill=fill, width=width, joint="curve")


def _arrows(d, data, t, xy, mw, mh):
    spd = np.array(data["vars"]["wind_speed_10m"], dtype=np.float32)[:, :, t]
    dirs = np.array(data["vars"]["wind_direction_10m"], dtype=np.float32)[:, :, t]
    lons, lats = data["lons"], data["lats"]
    step = max(1, int(round(len(lons) / 11)))
    for j in range(step // 2, len(lats), step):
        for i in range(step // 2, len(lons), step):
            s, a = spd[j, i], dirs[j, i]
            if np.isnan(s) or np.isnan(a) or s < 1:
                continue
            cx, cy = xy(lons[i], lats[j])
            ang = math.radians(a + 180)               # direction_10m is where it comes FROM
            ln = 18 + min(s, 50) * 1.3
            dx, dy = math.sin(ang) * ln / 2, -math.cos(ang) * ln / 2
            tip = (cx + dx, cy + dy)
            for col, wd in ((255, 9), (0, 4)):        # white halo, then the black arrow
                d.line((cx - dx, cy - dy, tip[0], tip[1]), fill=col, width=wd)
                for side in (-1, 1):
                    back = ang + math.pi + side * 0.5
                    d.line((tip[0], tip[1], tip[0] + math.sin(back) * 16, tip[1] - math.cos(back) * 16),
                           fill=col, width=wd)


def _temps(d, data, t, xy):
    temp = np.array(data["vars"]["temperature_2m"], dtype=np.float32)[:, :, t]
    lons, lats = data["lons"], data["lats"]
    step = max(1, int(round(len(lons) / 9)))
    f = render.ui_font(26, "semibold")
    for j in range(step // 2, len(lats), step):
        for i in range(step // 2, len(lons), step):
            if not np.isnan(temp[j, i]):
                d.text(xy(lons[i], lats[j]), "%d°" % round(temp[j, i]), font=f, fill=0, anchor="mm",
                       stroke_width=4, stroke_fill=255)


LEGENDS = {"rain": [("light", 0.3), ("moderate", 2), ("heavy", 8), ("very heavy", 20)],
           "clouds": [("clear", 0), ("partly", 40), ("mostly", 75), ("overcast", 100)]}


def _legend(d, layer, data, t, x, y, w):
    f = render.ui_font(22)
    if layer in LEGENDS:
        for n, (label, v) in enumerate(LEGENDS[layer]):
            if layer == "rain":
                g = 255 - 215 * min(1, math.log1p(v * 4) / math.log1p(80))
                text = "%s %g mm/h" % (label, v)
            else:
                g = 255 - 1.3 * v
                text = "%s  %d%%" % (label, v)
            d.rectangle((x, y + n * 38, x + 34, y + n * 38 + 28), fill=int(g), outline=0, width=2)
            d.text((x + 46, y + n * 38 + 14), text, font=f, fill=0, anchor="lm")
    elif layer == "wind":
        spd = np.array(data["vars"]["wind_speed_10m"], dtype=np.float32)[:, :, t]
        d.text((x, y), "Arrows: where the", font=f, fill=0)
        d.text((x, y + 30), "wind blows; longer", font=f, fill=0)
        d.text((x, y + 60), "= stronger. Max here:", font=f, fill=0)
        d.text((x, y + 94), "%d km/h" % round(float(np.nanmax(spd))), font=render.ui_font(30, "bold"), fill=0)
    else:
        allv = np.array(data["vars"]["temperature_2m"], dtype=np.float32)
        d.text((x, y), "Darker = warmer", font=f, fill=0)
        d.text((x, y + 30), "(%d° to %d° over" % (np.nanmin(allv), np.nanmax(allv)), font=f, fill=0)
        d.text((x, y + 60), "the whole 30 hours)", font=f, fill=0)


def _slider_geom(w, h):
    y = h - BAR_H
    return {"play": (16, y + 70, 166, y + 154), "prev": (180, y + 70, 270, y + 154),
            "next": (w - 106, y + 70, w - 16, y + 154), "track": (310, y + 80, w - 150, y + 144)}


def draw(w, h, fetch_if_missing=True):
    """The whole screen. Returns (image, key) where key says what's drawn (for live redraws)."""
    import show
    st = load()
    mw, mh = w - PANEL_W, h - BAR_H
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    p = place()
    b = borders(p["cc"])
    data = cached(st["area"])
    box = view(st["area"], mw, mh)
    if data and [round(v, 4) for v in data["box"]] != [round(v, 4) for v in box]:
        data = None                                    # (made for another view)
    if data is None and fetch_if_missing:
        data = fetch(st["area"], mw, mh)
    xy = _proj(box, mw, mh)
    targets = []
    if data:
        t, now_i = _hour_index(data, st["offset"])
        img.paste(show.to_16_grays(_shade(st["layer"], data, t, mw, mh)), (0, 0))
    _outline(d, b["neighbours"], xy, 2, fill=90)
    for name, rings in b["states"].items():
        _outline(d, rings, xy, 5 if name == p["admin1"] else 2, fill=0 if name == p["admin1"] else 40)
    _outline(d, b["country"], xy, 4)
    if data and st["layer"] == "wind":
        _arrows(d, data, t, xy, mw, mh)
    if data and st["layer"] == "temp":
        _temps(d, data, t, xy)
    x, y = xy(p["lon"], p["lat"])                      # you are here
    d.ellipse((x - 17, y - 17, x + 17, y + 17), fill=255)
    d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=0)
    d.text((x + 24, y), p["name"], font=render.ui_font(30, "bold"), fill=0, anchor="lm",
           stroke_width=5, stroke_fill=255)
    d.text((10, mh - 10), "Weather: Open-Meteo · Borders: Natural Earth", font=render.ui_font(18),
           fill=0, anchor="ld", stroke_width=3, stroke_fill=255)

    # ---- right panel
    px = mw + 20
    d.rectangle((mw, 0, w, h - BAR_H), fill=255)
    d.line((mw, 0, mw, h - BAR_H), fill=0, width=3)
    d.text((px, 22), "Weather map", font=render.ui_font(34, "bold"), fill=0)
    bf = render.ui_font(30, "semibold")
    for n, (key, label) in enumerate(LAYERS):
        box_ = (px, 80 + n * 74, w - 20, 140 + n * 74)
        on = st["layer"] == key
        d.rounded_rectangle(box_, radius=14, outline=0, width=3, fill=0 if on else 255)
        d.text(((box_[0] + box_[2]) / 2, (box_[1] + box_[3]) / 2), label, font=bf, fill=255 if on else 0, anchor="mm")
        targets.append({"action": "layer:" + key, "box": box_})
    aw = (w - 20 - px - 10) / 2
    for n, (key, label) in enumerate(AREAS):
        box_ = (px + n * (aw + 10), 384, px + n * (aw + 10) + aw, 440)
        on = st["area"] == key
        d.rounded_rectangle(box_, radius=14, outline=0, width=3, fill=0 if on else 255)
        d.text(((box_[0] + box_[2]) / 2, (box_[1] + box_[3]) / 2), label, font=render.ui_font(26, "semibold"),
               fill=255 if on else 0, anchor="mm")
        targets.append({"action": "area:" + key, "box": box_})
    if data:
        _legend(d, st["layer"], data, t, px, 470, w - px - 20)
        age = (time.time() - data["fetched"]) / 60
        d.text((px, h - BAR_H - 34), "data %s" % ("just now" if age < 2 else "%d min old" % age),
               font=render.ui_font(22), fill=0)

    # ---- time slider
    g = _slider_geom(w, h)
    d.rectangle((0, h - BAR_H, w, h), fill=255)       # (borders drawn past the map's bottom edge)
    d.line((0, h - BAR_H, w, h - BAR_H), fill=0, width=3)
    if data:
        when = datetime.fromisoformat(data["times"][t])
        rel = t - now_i
        label = when.strftime("%a %-I %p").replace("AM", "am").replace("PM", "pm") + \
            ("  ·  now" if rel == 0 else "  ·  %+d h" % rel)
        d.text((w / 2, h - BAR_H + 38), label, font=render.ui_font(40, "bold"), fill=0, anchor="mm")
        x0, ty0, x1, ty1 = g["track"]
        n = len(data["times"])
        cy = (ty0 + ty1) / 2
        d.line((x0, cy, x1, cy), fill=0, width=4)
        for k in range(n):
            tx = x0 + (x1 - x0) * k / (n - 1)
            tall = 18 if k == now_i else 10 if datetime.fromisoformat(data["times"][k]).hour % 6 == 0 else 5
            d.line((tx, cy - tall, tx, cy + tall), fill=0, width=4 if k == now_i else 2)
        mx = x0 + (x1 - x0) * t / (n - 1)
        d.ellipse((mx - 20, cy - 20, mx + 20, cy + 20), fill=0)
        sf = render.ui_font(20)
        d.text((x0 + (x1 - x0) * now_i / (n - 1), ty1 + 2), "now", font=sf, fill=0, anchor="mt")
        targets.append({"action": "track", "box": g["track"], "hours": n, "now": now_i})
    else:
        d.text((w / 2, h - BAR_H / 2), "Fetching weather data…", font=render.ui_font(40, "bold"), fill=0, anchor="mm")
    playing = st.get("playing")
    d.rounded_rectangle(g["play"], radius=16, outline=0, width=3, fill=0 if playing else 255)
    d.text(((g["play"][0] + g["play"][2]) / 2, (g["play"][1] + g["play"][3]) / 2), "Stop" if playing else "Play",
           font=render.ui_font(36, "bold"), fill=255 if playing else 0, anchor="mm")
    targets.append({"action": "play", "box": g["play"]})
    for key, sym in (("prev", "◀"), ("next", "▶")):
        d.rounded_rectangle(g[key], radius=16, outline=0, width=3)
        d.text(((g[key][0] + g[key][2]) / 2, (g[key][1] + g[key][3]) / 2), sym, font=render.ui_font(40, "bold"),
               fill=0, anchor="mm")
        targets.append({"action": key, "box": g[key]})
    with open(LAYOUT_FILE, "w") as f:
        json.dump(targets, f)
    key = "%s %s %s %s %s" % (st["area"], st["layer"], st["offset"], data and data["fetched"],
                              datetime.now().strftime("%Y-%m-%dT%H"))
    st["drawn"] = key
    save(st)
    return img


def needs_redraw():
    """True when what's on screen is out of date (new data, or the hour moved on)."""
    st = load()
    data = cached(st["area"])
    key = "%s %s %s %s %s" % (st["area"], st["layer"], st["offset"], data and data["fetched"],
                              datetime.now().strftime("%Y-%m-%dT%H"))
    return st.get("drawn") != key


# ------------------------------ input -------------------------------

def hit(cx, cy):
    try:
        with open(LAYOUT_FILE) as f:
            targets = json.load(f)
    except (OSError, ValueError):
        return None
    for t in targets:
        x0, y0, x1, y1 = t["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            if t["action"] == "track":
                k = round((cx - x0) / (x1 - x0) * (t["hours"] - 1))
                return "hour:%d" % (k - t["now"])
            return t["action"]
    return None


def tap(action):
    """Returns what changed, or None."""
    st = load()
    lo, hi = -PAST_HOURS, AHEAD_HOURS
    if action.startswith("layer:"):
        st["layer"] = action[6:]
    elif action.startswith("area:"):
        st["area"] = action[5:]
    elif action == "prev":
        st["offset"] = max(lo, st["offset"] - 1)
    elif action == "next":
        st["offset"] = min(hi, st["offset"] + 1)
    elif action.startswith("hour:"):
        st["offset"] = max(lo, min(hi, int(action[5:])))
    elif action == "now":
        st["offset"] = 0
    elif action == "play":
        st["playing"] = not st.get("playing")
        st["loops"] = 0
        action = "play" if st["playing"] else "stop"
    else:
        return None
    save(st)
    return action


def playing():
    return bool(load().get("playing"))


def stop():
    st = load()
    if st.get("playing"):
        st["playing"] = False
        save(st)


def step_play():
    """Play: move on an hour (round from 24 h ahead back to 6 h ago). False once it's done."""
    st = load()
    if not st.get("playing"):
        return False
    st["offset"] += 1
    if st["offset"] > AHEAD_HOURS:
        st["offset"] = -PAST_HOURS
        st["loops"] = st.get("loops", 0) + 1
        if st["loops"] >= PLAY_LOOPS:
            st.update(playing=False, offset=0)          # done: back to now
    save(st)
    return True
