#!/usr/bin/env python3
"""Play a video on the Kindle (e.g. Bad Apple).

  video.py prepare FILE [NAME]   ffmpeg -> PNG frames at FPS, copied once to the Kindle
  video.py play NAME             play it (prints START when the first frame is up)
  video.py stop                  stop a video that's playing
  video.py list

E-ink can't do 30 fps: frames are shown with FBInk's fast black/white A2 waveform, and the
player on the Kindle keeps to the clock by skipping frames when it falls behind (expect
roughly 4-8 frames a second). The dashboard is paused while a video plays.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import kindle
import render

FPS = 10
VIDEO_DIR = os.path.join(render.BASE_DIR, "video")           # frames on the Pi
KINDLE_VIDEO_DIR = render.KINDLE_DIR + "/video"              # frames on the Kindle
PAUSE_FILE = os.path.join(render.OUT_DIR, ".pause")          # push.sh skips while this is fresh
FBINK = "/mnt/us/libkh/bin/fbink"

# Runs on the Kindle: show frame (elapsed * FPS) each time round, so it stays in time
# with the soundtrack even when the panel can't keep up.
PLAYER = r'''#!/bin/sh
DIR="$1"; FPS="$2"; N="$3"; FB=%(fbink)s
centis() { cut -d' ' -f1 /proc/uptime | tr -d .; }
$FB -q -c -f
echo START
start=$(centis); last=-1; shown=0
while :; do
    i=$(( ($(centis) - start) * FPS / 100 ))
    [ "$i" -ge "$N" ] && break
    if [ "$i" -ne "$last" ]; then
        $FB -q -W A2 -g file="$(printf '%%s/%%05d.png' "$DIR" $((i + 1)))"
        last=$i; shown=$((shown + 1))
    else
        usleep 5000 2>/dev/null || sleep 0
    fi
done
secs=$(( ($(centis) - start) / 100 ))
echo "DONE shown $shown of $N frames in ${secs}s"
'''


def kindle_ip():
    ip, via = kindle.address()
    if not ip:
        raise SystemExit("Kindle not reachable (USB or Wi-Fi)")
    if via == "wifi":
        print("(over Wi-Fi: the first copy is slower, and playback may stutter more)", flush=True)
    return ip


def clips():
    out = {}
    if os.path.isdir(VIDEO_DIR):
        for name in sorted(os.listdir(VIDEO_DIR)):
            meta = os.path.join(VIDEO_DIR, name, "meta.json")
            if os.path.exists(meta):
                with open(meta) as f:
                    out[name] = json.load(f)
    return out


def prepare(src, name, upload_after=True):
    """Frames for the Kindle's screen (same orientation as everything else)."""
    out = os.path.join(VIDEO_DIR, name)
    frames = os.path.join(out, "frames")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(frames)
    w, h = (render.HEIGHT, render.WIDTH) if render.ROTATE else (render.WIDTH, render.HEIGHT)
    vf = ("fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,format=gray,"
          "lut=y='if(gt(val,127),255,0)'" % (FPS, w, h, w, h))     # pure black/white (A2 shows only that)
    if render.ROTATE:      # PIL rotate(90) is counter-clockwise = ffmpeg transpose=2
        vf += ",transpose=%d" % (2 if render.ROTATE_DEGREES == 90 else 1)
    print("converting to %d fps frames (this takes a minute)..." % FPS, flush=True)
    vf += ",format=monob"                               # 1-bit PNGs: small, quick to decode
    subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vf", vf, "-start_number", "1",
                    os.path.join(frames, "%05d.png")], check=True)
    n = len(os.listdir(frames))
    size = sum(os.path.getsize(os.path.join(frames, f)) for f in os.listdir(frames))
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump({"frames": n, "fps": FPS, "uploaded": False}, f)
    print("%d frames, %.1f MB" % (n, size / 1e6), flush=True)
    if upload_after:
        upload(name)


def upload(name):
    """Copy the frames to the Kindle once (tar over ssh)."""
    out = os.path.join(VIDEO_DIR, name)
    print("copying frames to the Kindle...", flush=True)
    tar = subprocess.Popen(["tar", "-C", out, "-cf", "-", "frames"], stdout=subprocess.PIPE)
    ssh = subprocess.run(["ssh"] + render.ssh_options() + [
        "root@" + kindle_ip(),
        "rm -rf '%s/%s' && mkdir -p '%s/%s' && tar -x -C '%s/%s'" % (
            (KINDLE_VIDEO_DIR, name) * 3)], stdin=tar.stdout)
    tar.wait()
    if ssh.returncode != 0:
        raise SystemExit("copying to the Kindle failed (is it connected?)")
    meta_path = os.path.join(out, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)
    meta["uploaded"] = True
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    print("copied", flush=True)


def play(name):
    meta = clips().get(name)
    if not meta:
        raise SystemExit("no video called %r (prepare it first)" % name)
    if not meta.get("uploaded"):
        upload(name)
    # Write the player to the Kindle (via stdin, so quoting stays simple).
    p = subprocess.run(["ssh"] + render.ssh_options() + [
        "root@" + kindle_ip(), "mkdir -p '%s' && cat > '%s/play.sh'" % (KINDLE_VIDEO_DIR, KINDLE_VIDEO_DIR)],
        input=(PLAYER % {"fbink": FBINK}).encode())
    if p.returncode != 0:
        raise SystemExit("couldn't reach the Kindle")
    with open(PAUSE_FILE, "w") as f:                    # dashboard: hands off (and how to stop us)
        f.write(str(os.getpid()))

    def interrupted(signum, frame):                     # Ctrl+C on the Mac / ssh closed
        raise KeyboardInterrupt
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        subprocess.run(["ssh"] + render.ssh_options() + [
            "root@" + kindle_ip(),
            "export PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH; sh '%s/play.sh' '%s/%s/frames' %d %d"
            % (KINDLE_VIDEO_DIR, KINDLE_VIDEO_DIR, name, meta["fps"], meta["frames"])])
    except KeyboardInterrupt:
        kindle.run("pkill -f video/play.sh; true")     # stop the player on the Kindle too
    finally:
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass
        import show                                     # put the dashboard back
        show.refresh_screen(force=True)
        show.push_now()


def stop():
    code, _ = kindle.run("pkill -f video/play.sh; true")
    print("stopped" if code == 0 else "couldn't reach the Kindle")


def main():
    args = sys.argv[1:]
    sys.stdout.reconfigure(line_buffering=True)
    if not args or args[0] == "list":
        for name, m in clips().items():
            print("  %s  (%d frames at %d fps%s)" % (name, m["frames"], m["fps"],
                                                     "" if m.get("uploaded") else ", not on the Kindle yet"))
        return 0
    if args[0] == "prepare" and len(args) >= 2:
        name = args[2] if len(args) > 2 else os.path.splitext(os.path.basename(args[1]))[0]
        prepare(args[1], name)
        return 0
    if args[0] == "play" and len(args) == 2:
        play(args[1])
        return 0
    if args[0] == "stop":
        stop()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
