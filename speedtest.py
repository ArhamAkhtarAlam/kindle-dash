#!/usr/bin/env python3
"""Internet speed test from the Pi. Standard library only.

  ping      median of a few tiny requests to Cloudflare
  download  CONNECTIONS parallel streams of a large public test file (Linode Mumbai, falling
            back to Singapore) for DOWNLOAD_SECONDS
  upload    CONNECTIONS parallel uploads to Cloudflare's speed-test endpoint for UPLOAD_SECONDS

Download and upload are separate tests. They use real data (as much as the line can
move in those seconds: a few hundred MB on a fast connection). Results go to
out/speedtest.json for the Pi stats screen.

  speedtest.py [down|up]   run one test (or both) and print the result
"""

import json
import os
import statistics
import sys
import threading
import time
import urllib.request

import render

RESULT_FILE = os.path.join(render.OUT_DIR, "speedtest.json")
BASE = "https://speed.cloudflare.com"
CONNECTIONS = 4
DOWNLOAD_SECONDS = 10
UPLOAD_SECONDS = 8
WARMUP_SECONDS = 2            # connections speed up first (TCP slow start): left out of the result
# Download: large public test files streamed for DOWNLOAD_SECONDS (Cloudflare caps single
# downloads at under 100 MB and rate-limits repeats, so it can't measure a fast line's maximum).
# Nearest first; the first one that answers is used.
DOWN_SERVERS = [
    ("Linode Mumbai", "https://speedtest.mumbai1.linode.com/100MB-mumbai1.bin"),
    ("Linode Singapore", "https://speedtest.singapore.linode.com/100MB-singapore.bin"),
    ("Hetzner Singapore", "https://sin-speed.hetzner.com/1GB.bin"),
]
UP_CHUNK = 4_000_000          # bytes per upload request
UA = {"User-Agent": "kindle-dash speedtest"}


def _ping():
    times = []
    for _ in range(5):
        t = time.time()
        req = urllib.request.Request(BASE + "/__down?bytes=0", headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        times.append((time.time() - t) * 1000)
    return statistics.median(times)


class Limited(Exception):
    pass


def _run_parallel(worker, seconds):
    """Run worker(stop, counts, errors, k) in CONNECTIONS threads for `seconds`. Returns
    (steady, best): bits/second after the first WARMUP_SECONDS (how speed tests usually measure:
    the ramp-up isn't your line's speed), and the best single second.
    Raises Limited if the server refused (HTTP 429), RuntimeError if nothing got through."""
    stop = threading.Event()
    counts, errors = [0] * CONNECTIONS, []
    threads = [threading.Thread(target=worker, args=(stop, counts, errors, k), daemon=True)
               for k in range(CONNECTIONS)]
    start = time.time()
    for t in threads:
        t.start()
    samples = [(0.0, 0)]                                  # (seconds in, bytes so far), 4 a second
    while time.time() - start < seconds:
        time.sleep(0.25)
        samples.append((time.time() - start, sum(counts)))
    stop.set()
    elapsed = time.time() - start
    for t in threads:
        t.join(15)
    if not sum(counts):
        if any("429" in e for e in errors):
            raise Limited("the test server is limiting tests from your connection; try again in a few minutes")
        raise RuntimeError(errors[0] if errors else "no data got through")
    warm = next((s for s in samples if s[0] >= WARMUP_SECONDS), samples[0])
    end = samples[-1]
    steady = (end[1] - warm[1]) * 8 / max(end[0] - warm[0], 0.1) if end[0] > warm[0] else sum(counts) * 8 / elapsed
    best = max(((b[1] - a[1]) * 8 / (b[0] - a[0]) for a, b in zip(samples, samples[4:]) if b[0] > a[0]),
               default=steady)                             # any 1-second stretch
    return steady, best


_down_url = [None]


def _pick_server():
    """First download server that answers. Returns (name, url)."""
    problems = []
    for name, url in DOWN_SERVERS:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                r.read(65536)
            return name, url
        except Exception as e:
            problems.append("%s: %s" % (name, str(e)[:40]))
    raise RuntimeError("no download server answered (" + "; ".join(problems) + ")")


def _download_worker(stop, counts, errors, k):
    """One long download per connection, read until time is up (restarted if the file ends
    first), so a test is only a handful of requests."""
    while not stop.is_set():
        try:
            req = urllib.request.Request(_down_url[0], headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                while not stop.is_set():
                    block = r.read(65536)
                    if not block:
                        break
                    counts[k] += len(block)
        except Exception as e:
            errors.append(str(e))
            if "429" in str(e):
                return
            time.sleep(0.5)


def _upload_worker(stop, counts, errors, k):
    body = os.urandom(UP_CHUNK)                       # random, so nothing can compress it
    while not stop.is_set():
        try:
            req = urllib.request.Request(BASE + "/__up", data=body, headers=dict(UA, **{
                "Content-Type": "application/octet-stream"}), method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            counts[k] += len(body)
        except Exception as e:
            errors.append(str(e))
            if "429" in str(e):
                return
            time.sleep(0.5)


def load():
    """{"down": {"bps", "ping", "at"}, "up": {"bps", "at"}, "running": "down"|"up"|None,
    "started": t, "error": {"down"|"up": message}}"""
    try:
        with open(RESULT_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if "bps" not in data.get("down", {}) and isinstance(data.get("down"), (int, float)):
        data = {}                                     # old single-test format
    data.setdefault("error", {})
    return data


def _save(data):
    with open(RESULT_FILE + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(RESULT_FILE + ".tmp", RESULT_FILE)


def running():
    """"down" or "up" while a test runs, else None."""
    st = load()
    if st.get("running") and time.time() - st.get("started", 0) < 60:
        return st["running"]
    return None


def run(kind):
    """Test "down" (with ping) or "up" (~10 s) and save it. A failed or refused test keeps
    the previous result and records the reason instead. Returns the saved data."""
    data = load()
    data.update(running=kind, started=time.time())
    data["error"].pop(kind, None)
    _save(data)
    try:
        if kind == "down":
            ping = _ping()
            server, _down_url[0] = _pick_server()
            bps, best = _run_parallel(_download_worker, DOWNLOAD_SECONDS)
            data["down"] = {"bps": bps, "best": best, "ping": ping, "at": time.time(), "server": server}
        else:
            bps, best = _run_parallel(_upload_worker, UPLOAD_SECONDS)
            data["up"] = {"bps": bps, "best": best, "at": time.time()}
    except Exception as e:
        data["error"][kind] = str(e)[:120]
    data["running"] = None
    _save(data)
    return data


if __name__ == "__main__":
    import health
    kinds = [k for k in sys.argv[1:] if k in ("down", "up")] or ["down", "up"]
    failed = False
    for kind in kinds:
        print("testing %s (about 10 seconds)..." % ("download" if kind == "down" else "upload"), flush=True)
        r = run(kind)
        if r["error"].get(kind):
            print("  %s test failed: %s" % (kind, r["error"][kind]))
            failed = True
        elif kind == "down":
            print("  download %s  ·  ping %.0f ms" % (health.speed(r["down"]["bps"]), r["down"]["ping"]))
        else:
            print("  upload %s" % health.speed(r["up"]["bps"]))
    sys.exit(1 if failed else 0)
