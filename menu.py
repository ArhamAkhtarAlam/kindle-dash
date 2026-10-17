"""Pull-down menu: swipe down from the top of the Kindle to open it, tap a screen to
switch to it, tap along the Light bar to set the front light, tap below it (or swipe up)
to close. Closes by itself after MENU_TIMEOUT_SEC.

The menu is drawn over a faded copy of the current screen and saved as out/menu.png;
out/menu.json records when it opened and where each button is (canvas coordinates).
"""

import json
import os
import time

from PIL import Image, ImageDraw

import render

MENU_FILE = os.path.join(render.OUT_DIR, "menu.json")
MENU_IMAGE = os.path.join(render.OUT_DIR, "menu.png")
MENU_TIMEOUT_SEC = 60

# (screen, label, the command that does the same from the Mac), in rows
ROWS = [[("weather", "Weather", None), ("clock", "Clock", "clock"),
         ("calendar", "Calendar", "calendar"), ("timer", "Timer", "timer")],
        [("todo", "To-do", "todo"), ("slides", "Slides", "slides"), ("feeds", "Feeds", "feeds"),
         ("spotify", "Spotify", "spotify")],
        [("home", "Home", "home"), ("games", "Games", "games"), ("draw", "Draw", "draw"), ("stats", "Pi stats", "stats")],
        [("hackers", "Hackers", "hackers")]]
# The Weather button opens this page of choices instead of switching straight away.
WEATHER = [("wttr", "Forecast", "wttr"), ("wttr2", "Graph", "wttr2"), ("wttrmoon", "Moon", "wttrmoon"),
           ("wmap", "Map", "weathermap")]
WEATHER_SCREENS = {name: text for name, text, _ in WEATHER}
ITEMS = [item for row in ROWS for item in row]
PAD = 36
HEAD_H = 80             # "Switch screen" line
BUTTON_H = 150
GAP = 22
SLIDER_H = 110          # front light slider row
PANEL_H = HEAD_H + len(ROWS) * (BUTTON_H + GAP) + SLIDER_H + PAD
MENU_BASE = os.path.join(render.OUT_DIR, "menu_base.png")   # faded screen under the menu


def _layout():
    import show                                        # (show imports this module)
    w, h = show.canvas_size()
    return {"canvas": [w, h], "rotate": render.ROTATE, "degrees": render.ROTATE_DEGREES}


def is_open():
    """True while the menu is up; a menu left open past the timeout closes itself."""
    try:
        with open(MENU_FILE) as f:
            opened = json.load(f)["opened"]
    except (OSError, ValueError, KeyError):
        return False
    if time.time() - opened > MENU_TIMEOUT_SEC or not os.path.exists(MENU_IMAGE):
        close()
        return False
    return True


def close():
    was = os.path.exists(MENU_FILE)
    for p in (MENU_FILE, MENU_IMAGE, MENU_BASE):
        try:
            os.remove(p)
        except OSError:
            pass
    return was


def open_menu(current_png, current_screen):
    """Draw the menu over current_png (the screen being shown) and make it current."""
    w, h = _layout()["canvas"]
    under = Image.open(current_png).convert("L")
    if render.ROTATE:                                  # back to canvas orientation
        under = under.rotate(-render.ROTATE_DEGREES, expand=True)
    if render.dark_mode():                             # it gets inverted again when saved
        from PIL import ImageOps
        under = ImageOps.invert(under)
    Image.blend(under, Image.new("L", (w, h), 255), 0.75).save(MENU_BASE)   # fade what's below
    redraw(current_screen)


def redraw(current_screen, page="main"):
    """(Re)draw the menu panel over the saved faded screen, e.g. after the light changed.
    page "weather" shows the weather choices instead of the main buttons."""
    import kindle
    import show
    lay = _layout()
    w, h = lay["canvas"]
    img = Image.open(MENU_BASE).convert("L")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, PANEL_H), fill=255)
    d.line((0, PANEL_H, w, PANEL_H), fill=0, width=4)

    head = render.ui_font(30, "semibold")
    d.text((PAD, 22), "Weather" if page == "weather" else "Switch screen", font=head, fill=0)
    dark = render.dark_mode()
    dbox = (w - PAD - 230, 12, w - PAD, 68)
    d.rounded_rectangle(dbox, radius=16, outline=0, width=3, fill=0 if dark else 255)
    dl = "Dark: on" if dark else "Dark: off"
    d.text((dbox[0] + (230 - d.textlength(dl, font=head)) / 2, 22), dl, font=head, fill=255 if dark else 0)
    hint = "tap below or swipe up to close"
    d.text((dbox[0] - 30 - d.textlength(hint, font=head), 22), hint, font=head, fill=90)

    label = render.ui_font(40, "semibold")
    sub = render.ui_font(25)
    per_row = max(len(r) for r in ROWS)
    bw = (w - 2 * PAD - GAP * (per_row - 1)) / per_row
    buttons = []
    if page == "weather":                              # one row of tall buttons + Back
        items = WEATHER + [("back", "‹ Back", None)]
        top, bottom = HEAD_H, HEAD_H + len(ROWS) * (BUTTON_H + GAP) - GAP
        big = render.ui_font(48, "bold")
        bw = (w - 2 * PAD - GAP * (len(items) - 1)) / len(items)     # (this row has its own count)
        for i, (name, text, cmd) in enumerate(items):
            x0 = PAD + i * (bw + GAP)
            x1 = x0 + bw
            on = name == current_screen
            d.rounded_rectangle((x0, top, x1, bottom), radius=26, outline=0, width=4, fill=0 if on else 255)
            fg = 255 if on else 0
            cy = (top + bottom) / 2
            d.text(((x0 + x1 - d.textlength(text, font=big)) / 2, cy - 50), text, font=big, fill=fg)
            if cmd:
                tag = "showing" if on else "kindle " + cmd
                d.text(((x0 + x1 - d.textlength(tag, font=sub)) / 2, cy + 26), tag, font=sub, fill=fg)
            buttons.append({"name": name, "x0": x0, "y0": top, "x1": x1, "y1": bottom})
    for r, row in enumerate(ROWS if page == "main" else []):
        top = HEAD_H + r * (BUTTON_H + GAP)
        bottom = top + BUTTON_H
        for i, (name, text, cmd) in enumerate(row):
            x0 = PAD + i * (bw + GAP)
            x1 = x0 + bw
            on = name == current_screen or (name == "weather" and current_screen in WEATHER_SCREENS)
            d.rounded_rectangle((x0, top, x1, bottom), radius=22, outline=0, width=4,
                                fill=0 if on else 255)
            fg = 255 if on else 0
            cy = (top + bottom) / 2
            d.text(((x0 + x1 - d.textlength(text, font=label)) / 2, cy - 42), text, font=label, fill=fg)
            if name == "weather":
                tag = ("showing: " + WEATHER_SCREENS[current_screen]) if on else "Forecast, Graph, Moon ›"
            else:
                tag = "showing" if on else "kindle " + cmd
            d.text(((x0 + x1 - d.textlength(tag, font=sub)) / 2, cy + 16), tag, font=sub, fill=fg)
            buttons.append({"name": name, "x0": x0, "y0": top, "x1": x1, "y1": bottom})

    # Front light slider: tap anywhere along the bar to set it.
    sy0 = HEAD_H + len(ROWS) * (BUTTON_H + GAP)
    cy = sy0 + SLIDER_H / 2
    lab = render.ui_font(34, "semibold")
    d.text((PAD, cy - 22), "Light", font=lab, fill=0)
    pct = kindle.light_percent()
    val = "?" if pct is None else "%d%%" % pct
    wifi_w = 330                                       # Wi-Fi button at the right end
    wx0 = w - PAD - wifi_w
    d.text((wx0 - 40 - d.textlength("100%", font=lab), cy - 22), val, font=lab, fill=0)
    bx0, bx1 = PAD + 130, wx0 - 40 - d.textlength("100%", font=lab) - 40
    d.rounded_rectangle((bx0, cy - 14, bx1, cy + 14), radius=14, outline=0, width=3)
    if pct is not None:
        kx = bx0 + (bx1 - bx0) * pct / 100
        if kx - bx0 > 10:
            d.rounded_rectangle((bx0, cy - 14, kx, cy + 14), radius=14, fill=0)
        d.ellipse((kx - 26, cy - 26, kx + 26, cy + 26), fill=255, outline=0, width=4)
    slider = {"x0": bx0, "x1": bx1, "y0": sy0, "y1": sy0 + SLIDER_H}

    # Wi-Fi: shows the network, tap to switch it on/off (not off while it's the Pi's only link)
    st = kindle.status()
    on = st.get("wifi")
    via = kindle.link_via()
    label = ("Wi-Fi: " + (st.get("ssid") or "on")) if on else "Wi-Fi: off" if on is False else "Wi-Fi: ?"
    sub = "connected over Wi-Fi" if via == "wifi" else "tap to turn %s" % ("off" if on else "on")
    wbox = (wx0, sy0 + 12, w - PAD, sy0 + SLIDER_H - 12)
    d.rounded_rectangle(wbox, radius=18, outline=0, width=3, fill=0 if on else 255)
    lf, sf = render.ui_font(28, "semibold"), render.ui_font(20)
    fg = 255 if on else 0
    while d.textlength(label, font=lf) > wifi_w - 30 and len(label) > 8:
        label = label[:-2].rstrip() + "…" if not label.endswith("…") else label[:-2].rstrip() + "…"
    d.text((wx0 + (wifi_w - d.textlength(label, font=lf)) / 2, sy0 + 22), label, font=lf, fill=fg)
    d.text((wx0 + (wifi_w - d.textlength(sub, font=sf)) / 2, sy0 + 60), sub, font=sf, fill=fg)
    wifi_btn = {"x0": wbox[0], "y0": wbox[1], "x1": wbox[2], "y1": wbox[3]}

    show.finish(img, MENU_IMAGE)
    opened = time.time()
    with open(MENU_FILE, "w") as f:
        json.dump(dict(lay, opened=opened, panel_h=PANEL_H, buttons=buttons, slider=slider,
                       wifi=wifi_btn, dark=list(dbox), screen=current_screen, page=page), f)


def hit(cx, cy):
    """What a tap at canvas (cx, cy) means: a screen name, ("light", percent), ("wifi",),
    "outside" (below the panel), or None (on the panel but not on a button)."""
    try:
        with open(MENU_FILE) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return "outside"
    for b in m["buttons"]:
        if b["x0"] <= cx <= b["x1"] and b["y0"] <= cy <= b["y1"]:
            return b["name"]
    db = m.get("dark")
    if db and db[0] <= cx <= db[2] and db[1] <= cy <= db[3]:
        return ("dark",)
    wb = m.get("wifi")
    if wb and wb["x0"] <= cx <= wb["x1"] and wb["y0"] <= cy <= wb["y1"]:
        return ("wifi",)
    sl = m.get("slider")
    if sl and sl["y0"] <= cy <= sl["y1"] and sl["x0"] - 60 <= cx <= sl["x1"] + 60:
        pct = (cx - sl["x0"]) / (sl["x1"] - sl["x0"]) * 100
        return ("light", max(0, min(100, round(pct / 5) * 5)))      # snap to 5%
    return "outside" if cy > m["panel_h"] else None
