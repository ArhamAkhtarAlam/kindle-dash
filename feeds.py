"""RSS/Atom feeds screen.

Each feed shows either a list of headlines (tap one to read its summary, tap again to go
back) or, for "image" feeds like xkcd, the latest item's picture with its title and caption.
Swipe left/right on the Kindle to change feed. The feed on screen is re-fetched every
CHECK_MINUTES; others are fetched when you switch to them.

feeds/feeds.json  the list of feeds and which one is current
feeds/cache/      parsed items per feed (+ downloaded pictures)
"""

import hashlib
import html
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from PIL import Image, ImageDraw

import render

FEEDS_DIR = os.path.join(render.BASE_DIR, "feeds")
INDEX_FILE = os.path.join(FEEDS_DIR, "feeds.json")
CACHE_DIR = os.path.join(FEEDS_DIR, "cache")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "feeds_layout.json")
CHECK_MINUTES = 30
STYLES = ("headlines", "image")
UA = {"User-Agent": "Mozilla/5.0 (kindle-dash RSS reader)"}
DEFAULT = [{"name": "xkcd", "url": "https://xkcd.com/rss.xml", "style": "image"}]
MARGIN = 50


# ------------------------------ storage -----------------------------

def load():
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"items": list(DEFAULT)}
    data.setdefault("items", [])
    data.setdefault("index", 0)
    data.setdefault("detail", None)            # open article (index in the feed), or None
    data["index"] = data["index"] % len(data["items"]) if data["items"] else 0
    return data


def save(data):
    os.makedirs(FEEDS_DIR, exist_ok=True)
    with open(INDEX_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(INDEX_FILE + ".tmp", INDEX_FILE)


def _key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def format_list(data):
    if not data["items"]:
        return "  (no feeds)"
    return "\n".join("  %d. %s  (%s) %s%s" % (i + 1, f["name"], f["style"], f["url"],
                                              "  <- current" if i == data["index"] else "")
                     for i, f in enumerate(data["items"]))


# ------------------------------ parsing -----------------------------

class _Text(HTMLParser):
    """HTML -> plain text, remembering the first <img> (src + title/alt)."""

    def __init__(self):
        super().__init__()
        self.parts, self.img, self.img_text = [], None, ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img" and not self.img and a.get("src"):
            self.img, self.img_text = a["src"], a.get("title") or a.get("alt") or ""
        if tag in ("p", "br", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        t = html.unescape("".join(self.parts))
        return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.attrib["_ns"] = el.tag.split("}")[0][1:]
            el.tag = el.tag.split("}", 1)[1]
    return root


def _date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s).timestamp()                 # RSS: RFC 822
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()   # Atom: ISO 8601
    except ValueError:
        return None


def parse(xml_bytes):
    """(feed title, [{"title", "link", "date", "text", "img", "img_text"}]) from RSS or Atom."""
    root = _strip_ns(ET.fromstring(xml_bytes))
    entries = root.findall("./channel/item") or root.findall(".//item") or root.findall("./entry")
    title = (root.findtext("./channel/title") or root.findtext("./title") or "").strip()
    items = []
    for e in entries[:30]:
        link = e.findtext("link") or ""
        if not link.strip():
            for l in e.findall("link"):
                if l.get("rel", "alternate") == "alternate" and l.get("href"):
                    link = l.get("href")
                    break
        body = (e.findtext("description") or e.findtext("summary") or e.findtext("content")
                or e.findtext("encoded") or "")
        p = _Text()
        p.feed(body)
        img = p.img
        for m in e.findall("content") + e.findall("thumbnail") + e.findall("enclosure"):
            if not img and m.get("url") and (m.get("medium") == "image" or
                                             (m.get("type") or "").startswith("image") or m.tag == "thumbnail"):
                img = m.get("url")
        items.append({"title": html.unescape((e.findtext("title") or "").strip()),
                      "link": link.strip(),
                      "date": _date(e.findtext("pubDate") or e.findtext("updated")
                                    or e.findtext("published") or e.findtext("date")),
                      "text": p.text(), "img": img, "img_text": html.unescape(p.img_text)})
    return title, items


def guess_style(items):
    """"image" when entries are pictures with little text (comics), else "headlines"."""
    if items and items[0].get("img") and len(items[0].get("text", "")) < 40:
        return "image"
    return "headlines"


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=render.HTTP_TIMEOUT_SEC) as resp:
        return resp.read()


def fetch(feed, force=False):
    """Cached items for feed, re-fetching after CHECK_MINUTES. Returns (items, changed).
    On failure keeps the cached copy."""
    path = os.path.join(CACHE_DIR, _key(feed["url"]) + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = None
    if cache and not force and time.time() - cache["fetched"] < CHECK_MINUTES * 60:
        return cache["items"], False
    try:
        _, items = parse(_get(feed["url"]))
    except Exception as e:
        render.log("feed %s: fetch failed, keeping cached copy: %s" % (feed["name"], e))
        return (cache or {}).get("items", []), False
    changed = not cache or [i["link"] or i["title"] for i in items] != \
        [i["link"] or i["title"] for i in cache["items"]]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"fetched": time.time(), "items": items}, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    if changed:
        render.log("feed %s: %d items" % (feed["name"], len(items)))
    return items, changed


def picture(url):
    """Downloaded picture for an image feed (cached), as an L-mode image, or None."""
    path = os.path.join(CACHE_DIR, _key(url) + ".img")
    if not os.path.exists(path):
        try:
            data = _get(url)
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            render.log("feed picture download failed: %s" % e)
            return None
    try:
        im = Image.open(path)
        im.seek(0)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        return im.convert("L")
    except Exception:
        return None


# ------------------------------ drawing -----------------------------

def _ago(ts):
    if not ts:
        return ""
    mins = (time.time() - ts) / 60
    if mins < 60:
        return "%dm ago" % max(1, mins)
    if mins < 48 * 60:
        return "%dh ago" % (mins / 60)
    return datetime.fromtimestamp(ts).strftime("%-d %b")


def _header(d, w, feed, pos, total, right_text=""):
    title = render.ui_font(54, "bold")
    small = render.ui_font(28)
    d.text((MARGIN, 30), feed["name"], font=title, fill=0)
    tag = "%d / %d  ·  swipe for other feeds" % (pos + 1, total) if total > 1 else ""
    tag = (right_text + ("  ·  " if tag and right_text else "") + tag).strip()
    d.text((w - MARGIN - d.textlength(tag, font=small), 50), tag, font=small, fill=0)
    d.line((MARGIN, 108, w - MARGIN, 108), fill=0, width=3)
    return 128


def _fit_lines(show, text, font, max_w, max_lines):
    lines = show.wrap(text, font, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and render_len(font, last + "…") > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def render_len(font, text):
    return font.getlength(text)


def draw(w, h, data, items):
    """The current feed: headline list, article detail, or picture. Returns (image, rows)
    where rows are tap targets {"index", "y0", "y1"} in canvas coordinates."""
    import show                                         # (show imports this module)
    feed = data["items"][data["index"]]
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    if not items:
        top = _header(d, w, feed, data["index"], len(data["items"]))
        f = render.ui_font(48, "medium")
        msg = "Nothing here yet (no internet, or not an RSS/Atom feed?)"
        d.text((MARGIN, top + 60), msg, font=f, fill=0)
        return img, []

    if feed["style"] == "image":                        # latest picture, like xkcd
        it = items[0]
        top = _header(d, w, feed, data["index"], len(data["items"]), _ago(it["date"]))
        tf = render.ui_font(42, "semibold")
        d.text((MARGIN, top), it["title"], font=tf, fill=0)
        top += 64
        cap_font = render.ui_font(29)
        caption = it.get("img_text") or it.get("text", "")
        cap = _fit_lines(show, caption, cap_font, w - 2 * MARGIN, 4) if caption else []
        cap_h = int(cap_font.size * 1.3) * len(cap)
        bottom = h - MARGIN - cap_h - (16 if cap else 0)
        pic = picture(it["img"]) if it.get("img") else None
        if pic:
            box_w, box_h = w - 2 * MARGIN, bottom - top - 10
            scale = min(box_w / pic.width, box_h / pic.height, 3.0)
            pic = pic.resize((max(1, int(pic.width * scale)), max(1, int(pic.height * scale))),
                             Image.Resampling.LANCZOS)
            img.paste(show.to_16_grays(pic), (int((w - pic.width) / 2), int(top + (box_h - pic.height) / 2)))
        y = h - MARGIN - cap_h
        for line in cap:
            d.text((MARGIN, y), line, font=cap_font, fill=0)
            y += int(cap_font.size * 1.3)
        return img, []

    detail = data.get("detail")
    if detail is not None and detail < len(items):      # one article's summary
        it = items[detail]
        top = _header(d, w, feed, data["index"], len(data["items"]), "tap anywhere to go back")
        tf = render.ui_font(56, "bold")
        title = _fit_lines(show, it["title"], tf, w - 2 * MARGIN, 3)
        for line in title:
            d.text((MARGIN, top), line, font=tf, fill=0)
            top += int(tf.size * 1.2)
        meta = render.ui_font(28)
        d.text((MARGIN, top + 6), _ago(it["date"]), font=meta, fill=0)
        top += 60
        body = render.ui_font(38)
        lh = int(body.size * 1.4)
        max_lines = max(1, (h - MARGIN - top) // lh)
        for line in _fit_lines(show, it["text"] or "(no summary in the feed)", body,
                               w - 2 * MARGIN, max_lines):
            d.text((MARGIN, top), line, font=body, fill=0)
            top += lh
        return img, [{"index": "back", "y0": 0, "y1": h}]

    top = _header(d, w, feed, data["index"], len(data["items"]), "tap a headline to read")
    tf = render.ui_font(38, "semibold")
    sf = render.ui_font(27)
    row_h = 2 * int(tf.size * 1.2) + int(sf.size * 1.3) + 26
    rows = []
    y = top + 6
    for i, it in enumerate(items):
        if y + row_h > h - 20:
            break
        tl = _fit_lines(show, it["title"] or "(untitled)", tf, w - 2 * MARGIN, 2)
        yy = y
        for line in tl:
            d.text((MARGIN, yy), line, font=tf, fill=0)
            yy += int(tf.size * 1.2)
        meta = " · ".join(p for p in (_ago(it["date"]), (it["text"] or "").replace("\n", " ")) if p)
        meta = _fit_lines(show, meta, sf, w - 2 * MARGIN, 1)[0] if meta else ""
        d.text((MARGIN, yy + 4), meta, font=sf, fill=60)
        end = yy + 4 + int(sf.size * 1.3) + 10
        rows.append({"index": i, "y0": y - 6, "y1": end + 6})
        d.line((MARGIN, end + 6, w - MARGIN, end + 6), fill=190, width=1)
        y = end + 16
    return img, rows


def save_layout(rows):
    with open(LAYOUT_FILE, "w") as f:
        json.dump({"rows": rows}, f)


def row_at(cy):
    try:
        with open(LAYOUT_FILE) as f:
            rows = json.load(f)["rows"]
    except (OSError, ValueError, KeyError):
        return None
    for r in rows:
        if r["y0"] <= cy <= r["y1"]:
            return r["index"]
    return None
