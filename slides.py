"""Slideshow: images uploaded from the Mac, shown one at a time; swipe left/right on the
Kindle to move between them.

Images live in slides/ (as uploaded PNGs); slides/slides.json keeps their names, fit/fill
mode and which one is current.
"""

import json
import os
import shutil
import uuid

import render

SLIDES_DIR = os.path.join(render.BASE_DIR, "slides")
INDEX_FILE = os.path.join(SLIDES_DIR, "slides.json")
MODES = ("fit", "fill")


def load():
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("items", [])
    data.setdefault("index", 0)
    if data["items"]:
        data["index"] %= len(data["items"])
    else:
        data["index"] = 0
    return data


def save(data):
    os.makedirs(SLIDES_DIR, exist_ok=True)
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX_FILE)


def add(data, src_path, name, mode):
    """Copy src_path into slides/ and append it. Returns the name used (made unique)."""
    os.makedirs(SLIDES_DIR, exist_ok=True)
    taken = {it["name"].casefold() for it in data["items"]}
    base, n = name, 2
    while name.casefold() in taken:
        name = "%s %d" % (base, n)
        n += 1
    stored = uuid.uuid4().hex + ".png"
    shutil.copyfile(src_path, os.path.join(SLIDES_DIR, stored))
    data["items"].append({"name": name, "file": stored, "mode": mode})
    return name


def remove(data, i):
    item = data["items"].pop(i)
    try:
        os.remove(os.path.join(SLIDES_DIR, item["file"]))
    except OSError:
        pass
    if data["items"]:
        if i < data["index"] or data["index"] >= len(data["items"]):
            data["index"] = max(0, data["index"] - 1)
    else:
        data["index"] = 0
    return item


def path_of(item):
    return os.path.join(SLIDES_DIR, item["file"])


def format_list(data):
    if not data["items"]:
        return "  (no slides yet)"
    return "\n".join("  %d. %s  (%s)%s" % (i + 1, it["name"], it["mode"],
                                           "  <- current" if i == data["index"] else "")
                     for i, it in enumerate(data["items"]))
