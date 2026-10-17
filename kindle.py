"""The Kindle's own status (battery, charging, front light, Wi-Fi), how to reach it, and
small helpers to talk to it.

Reaching it: USB first (KINDLE_IP); if the cable is unplugged, its Wi-Fi address. That is
learned automatically: every push records the Kindle's Wi-Fi IP and MAC, and if the router
gives it a new address the Pi finds it again by MAC on the local network. The answer is
cached in out/.kindle_link.json; "kindle.py --ip" prints it for push.sh (empty = not reachable).

push.sh records each push's STATUS output with "kindle.py --record-output". Screens draw the
battery from out/kindle.json.
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import render

STATUS_FILE = os.path.join(render.OUT_DIR, "kindle.json")
SEEN_FILE = os.path.join(render.OUT_DIR, ".kindle_seen")
LIGHT_MAX = 24          # PW3 front light steps (lipc flIntensity 0-24)
LINK_FILE = os.path.join(render.OUT_DIR, ".kindle_link.json")
SWEEP_EVERY_SEC = 600   # at most one LAN search for the Kindle's MAC per 10 minutes
# STATUS battery charging light wifi-enabled wlan-mac wlan-ip, then SSID (may have spaces)
STATUS_CMD = ("echo STATUS $(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null || echo -)"
              " $(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null || echo -)"
              " $(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null || echo -)"
              " $(lipc-get-prop com.lab126.wifid enable 2>/dev/null || echo -)"
              " $(cat /sys/class/net/wlan0/address 2>/dev/null || echo -)"
              " $(ifconfig wlan0 2>/dev/null | sed -n 's/.*inet addr:\\([0-9.]*\\).*/\\1/p' | grep . || echo -);"
              " echo SSID $(wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^ssid=//p')")


# ------------------------------ link --------------------------------

def _ping(ip, wait=1):
    return subprocess.call(["ping", "-c", "1", "-W", str(wait), ip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def _alive(ip, wait=1.5):
    """Is the Kindle's SSH answering at ip? (Its firewall drops pings on Wi-Fi and only
    lets SSH in, so a TCP connect to port 22 is the reliable test on both links.)"""
    try:
        with socket.create_connection((ip, 22), timeout=wait):
            return True
    except OSError:
        return False


def _neigh_ip(mac):
    """IPv4 address the Pi has seen for mac (from its ARP table), or None."""
    out = subprocess.run(["ip", "-4", "neigh"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if mac and mac.lower() in line.lower() and "FAILED" not in line:
            return line.split()[0]
    return None


def _sweep_lan():
    """Ping every address on the Pi's own /24 networks once, to fill the ARP table
    (devices that ignore pings still answer ARP, so they show up in "ip neigh")."""
    out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    hosts = set()
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out):
        net = ipaddress.ip_network("%s/24" % m.group(1), strict=False)
        if not (net.is_private and not net.is_loopback) or str(net).startswith("192.168.15."):
            continue                                   # skip loopback and the USB link
        hosts.update(str(h) for h in net.hosts())
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(lambda ip: _ping(ip, 1), hosts))


def _save_link(ip, via):
    with open(LINK_FILE + ".tmp", "w") as f:
        json.dump({"ip": ip, "via": via, "at": time.time(),
                   "swept": _link().get("swept", 0)}, f)
    os.replace(LINK_FILE + ".tmp", LINK_FILE)


def _link():
    try:
        with open(LINK_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def address():
    """(ip, "usb"|"wifi") for the Kindle right now, or (None, None) if it can't be reached."""
    os.makedirs(render.OUT_DIR, exist_ok=True)
    link = _link()
    tried = []
    def ok(ip, via):
        if ip and ip not in tried:
            tried.append(ip)
            if _alive(ip):
                if (ip, via) != (link.get("ip"), link.get("via")):
                    render.log("Kindle reachable over %s at %s" % (via.upper() if via == "usb" else "Wi-Fi", ip))
                _save_link(ip, via)
                open(SEEN_FILE, "a").close()
                os.utime(SEEN_FILE)
                return True
        return False
    if ok(render.KINDLE_IP, "usb"):
        return render.KINDLE_IP, "usb"
    st = status()
    for ip in (render.KINDLE_WIFI_IP, st.get("wifi_ip"), link.get("ip") if link.get("via") == "wifi" else None,
               _neigh_ip(st.get("wifi_mac"))):
        if ok(ip, "wifi"):
            return ip, "wifi"
    if st.get("wifi_mac") and time.time() - link.get("swept", 0) > SWEEP_EVERY_SEC:
        link["swept"] = time.time()
        with open(LINK_FILE, "w") as f:
            json.dump(link, f)
        _sweep_lan()                                 # maybe the router gave it a new address
        if ok(_neigh_ip(st["wifi_mac"]), "wifi"):
            return tried[-1], "wifi"
    return None, None


def link_via():
    """"usb"/"wifi" if the Kindle answered in the last 2 minutes, else None."""
    link = _link()
    return link.get("via") if time.time() - link.get("at", 0) < 120 else None


def run(command, timeout=20):
    """Run a shell command on the Kindle (over USB or Wi-Fi). Returns (exit code, output)."""
    ip, _ = address()
    if not ip:
        return 1, ""
    cmd = ["ssh"] + render.ssh_options() + [
        "root@" + ip, "export PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH; " + command]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)      # never let a Kindle command read our input
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 1, ""


def status():
    """{"battery", "charging", "light", "updated", "seen"} (values may be None)."""
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    try:
        st["seen"] = os.path.getmtime(SEEN_FILE)
    except OSError:
        st["seen"] = None
    for k in ("battery", "charging", "light", "updated", "wifi", "wifi_mac", "wifi_ip", "ssid"):
        st.setdefault(k, None)
    return st


def record(level, charging, light, wifi=None, mac=None, wifi_ip=None, ssid=None):
    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    st = status()
    st.pop("seen", None)
    st.update(updated=time.time())
    for k, v in (("battery", num(level)), ("charging", num(charging)), ("light", num(light)),
                 ("wifi", num(wifi))):
        if v is not None:
            st[k] = v
    if st.get("charging") is not None:
        st["charging"] = bool(st["charging"])
    if st.get("wifi") is not None:
        st["wifi"] = bool(st["wifi"])
    if mac and re.fullmatch(r"[0-9a-fA-F:]{17}", mac):
        st["wifi_mac"] = mac.lower()
    if wifi is not None:                      # a full status line: IP/SSID may now be empty
        st["wifi_ip"] = wifi_ip if wifi_ip and re.fullmatch(r"[0-9.]+", wifi_ip) else None
        st["ssid"] = ssid or None
    os.makedirs(render.OUT_DIR, exist_ok=True)
    with open(STATUS_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATUS_FILE + ".tmp", STATUS_FILE)


def refresh_status():
    """Ask the Kindle now (used when nothing was pushed for a while)."""
    code, out = run(STATUS_CMD)
    return record_output(out)


def record_output(text):
    """Record the STATUS/SSID lines printed by STATUS_CMD. True if there were any."""
    fields, ssid = None, None
    for line in text.splitlines():
        if line.startswith("STATUS"):
            fields = (line.split()[1:] + ["-"] * 6)[:6]
        elif line.startswith("SSID"):
            ssid = line[5:].strip()
    if not fields:
        return False
    fields = [None if f == "-" else f for f in fields]
    record(*fields, ssid=ssid)
    return True


def set_wifi(on):
    """Turn the Kindle's Wi-Fi on/off. Refuses to switch it off while that's the only link."""
    if not on and link_via() == "wifi":
        return "can't turn Wi-Fi off while the Pi is connected through it"
    code, _ = run("lipc-set-prop com.lab126.wifid enable %d" % (1 if on else 0))
    if code != 0:
        return "couldn't reach the Kindle"
    time.sleep(4 if on else 1)
    refresh_status()
    return None


def set_light(percent):
    """Front light to percent (0-100). Returns the step used, or None if it failed."""
    step = round(max(0, min(100, percent)) * LIGHT_MAX / 100)
    code, _ = run("lipc-set-prop com.lab126.powerd flIntensity %d" % step)
    if code != 0:
        return None
    st = status()
    record(st["battery"], st["charging"], step)
    return step


def light_percent():
    light = status()["light"]
    return None if light is None else round(light * 100 / LIGHT_MAX)


def draw_battery(draw, x1, y, height=28):
    """Battery icon + percentage, right-aligned to x1 at top y. Returns its left edge."""
    st = status()
    if st["battery"] is None:
        return x1
    font = render.ui_font(height * 0.9, "medium")
    text = "%d%%%s" % (st["battery"], " charging" if st["charging"] else "")
    tw = draw.textlength(text, font=font)
    bw, bh = height * 1.9, height * 0.9
    bx1 = x1 - tw - 12
    bx0 = bx1 - bw
    by0 = y + (height - bh) / 2
    draw.rounded_rectangle((bx0, by0, bx1, by0 + bh), radius=4, outline=0, width=3)
    draw.rectangle((bx1, by0 + bh * 0.3, bx1 + 4, by0 + bh * 0.7), fill=0)        # nub
    fill_w = (bw - 10) * max(0, min(100, st["battery"])) / 100
    if fill_w > 0:
        draw.rectangle((bx0 + 5, by0 + 5, bx0 + 5 + fill_w, by0 + bh - 5), fill=0)
    draw.text((x1 - tw, y + (height - font.size) / 2 - 2), text, font=font, fill=0)
    return bx0


def health():
    """What the Mac watcher needs, as JSON."""
    st = status()
    now = time.time()
    return {"battery": st["battery"], "charging": st["charging"], "light": light_percent(),
            "link": link_via(), "wifi": st["wifi"], "ssid": st["ssid"], "wifi_ip": st["wifi_ip"],
            "seen_min_ago": None if st["seen"] is None else round((now - st["seen"]) / 60, 1),
            "status_min_ago": None if st["updated"] is None else round((now - st["updated"]) / 60, 1)}


if __name__ == "__main__":
    if sys.argv[1:2] == ["--record"]:
        record(*(sys.argv[2:] + [None] * 3)[:3])
    elif sys.argv[1:2] == ["--record-output"]:
        record_output(sys.stdin.read())
    elif sys.argv[1:2] == ["--ip"]:
        out, sys.stdout = sys.stdout, sys.stderr   # log lines go to the log, not into $KINDLE_IP
        ip = address()[0]
        sys.stdout = out
        print(ip or "")
    elif sys.argv[1:2] == ["--health"]:
        print(json.dumps(health()))
