#!/bin/sh
# Copy the current screen (weather, or your image/text from show.py) to the Kindle
# over USB networking and display it.
# Exits 0 quietly (one log line) if the Kindle isn't connected.
#   push.sh         normal timer run: skips an unchanged image (re-sent every 30 min)
#   push.sh --now   always send, with a full refresh (used by show.py)
#   push.sh --photo always send, with PHOTO_REFRESHES full refreshes (show.py image)
#   push.sh --quick always send, partial refresh only (to-do taps: fast, no flash)
#   push.sh --alert always send, flashing REMINDER_FLASHES times (reminder shown/dismissed)
# Every run also checks for due reminders (reminders.py --check) and flashes one up.
set -u
cd "$(dirname "$0")"
NOW=0; PHOTO=0; QUICK=0; ALERT=0
case "${1:-}" in
    --now) NOW=1 ;; --photo) NOW=1; PHOTO=1 ;; --quick) NOW=1; QUICK=1 ;; --alert) NOW=1; ALERT=1 ;;
esac

# One push at a time (the timer and show.py can overlap).
mkdir -p out
exec 9>out/.push.lock
flock 9

# A video is playing (video.py): leave the screen alone. Stale pause files (15+ min) are ignored.
# (only while whoever made the marker is still running: a crashed one mustn't freeze the dashboard)
if [ -n "$(find out/.pause -mmin -15 2>/dev/null)" ] && kill -0 "$(cat out/.pause 2>/dev/null)" 2>/dev/null; then exit 0; fi

eval "$(venv/bin/python render.py --print-config)"   # KINDLE_IP, KINDLE_DIR, SSH_OPTS, DISPLAY_TOOL, ...
# A reminder that has just come due takes over the screen, with flashes.
if [ "$(venv/bin/python reminders.py --check)" = fired ]; then NOW=1; ALERT=1; QUICK=0; fi
IMG=$(venv/bin/python show.py --current)              # weather, custom, or alert image
STATUS_CMD=$(venv/bin/python -c 'import kindle; print(kindle.STATUS_CMD)')
REMOTE="$KINDLE_DIR/screen.png"

# USB if it's plugged in, otherwise the Kindle's Wi-Fi address (kindle.py works it out).
KINDLE_IP=$(venv/bin/python kindle.py --ip)
if [ -z "$KINDLE_IP" ]; then
    echo "Kindle not reachable (USB or Wi-Fi), skipping push"
    exit 0
fi

# Don't re-send an identical image every minute (e.g. a photo); refresh it every 30 min anyway.
# Still read the battery every 5 minutes while nothing changes.
SUM=$(sha1sum "$IMG" 2>/dev/null | cut -d' ' -f1)
LAST=$(cat out/.last_push 2>/dev/null || echo "none 0")
if [ $NOW = 0 ] && [ "${LAST%% *}" = "$SUM" ] && [ $(( $(date +%s) - ${LAST##* } )) -lt 1800 ]; then
    if [ -z "$(find out/kindle.json -mmin -5 2>/dev/null)" ]; then
        # shellcheck disable=SC2086
        ssh $SSH_OPTS "root@$KINDLE_IP" "export PATH=/usr/sbin:/sbin:/usr/bin:/bin:\$PATH; $STATUS_CMD" 2>/dev/null \
            | venv/bin/python kindle.py --record-output
    fi
    exit 0
fi

# Full clear (flashes, removes ghosting) every FULL_REFRESH_EVERY pushes, partial otherwise.
COUNT_FILE=out/.push_count
COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
if [ $QUICK = 1 ]; then FULL=0
elif [ $NOW = 1 ] || [ $((COUNT % FULL_REFRESH_EVERY)) -eq 0 ]; then FULL=1; else FULL=0; fi
if [ "$DISPLAY_TOOL" = fbink ]; then
    FB=/mnt/us/libkh/bin/fbink   # FBInk from the WinterBreak hotfix
    if [ $FULL = 1 ]; then SHOW="$FB -q -c -f -g file='$REMOTE'"; else SHOW="$FB -q -g file='$REMOTE'"; fi
else
    if [ $FULL = 1 ]; then SHOW="eips -c && eips -f -g '$REMOTE'"; else SHOW="eips -g '$REMOTE'"; fi
fi
# A new photo gets several full refreshes in a row, to wipe the previous screen's ghosting;
# a reminder (shown or dismissed) flashes the screen several times.
REPEAT=1
[ $PHOTO = 1 ] && REPEAT=$PHOTO_REFRESHES
[ $ALERT = 1 ] && FULL=1 && REPEAT=$REMINDER_FLASHES
if [ "$REPEAT" -gt 1 ]; then
    if [ "$DISPLAY_TOOL" = fbink ]; then ONE="$FB -q -c -f -g file='$REMOTE'"; else ONE="eips -c && eips -f -g '$REMOTE'"; fi
    SHOW="$ONE"; i=1
    # (the Kindle's busybox sleep only takes whole seconds)
    while [ $i -lt "$REPEAT" ]; do SHOW="$SHOW; sleep 1; $ONE"; i=$((i + 1)); done
fi

# Keep the Kindle awake. Set on every push because it resets when the Kindle reboots.
if [ "$PREVENT_SLEEP" = True ]; then
    SHOW="lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null; $SHOW"
else
    SHOW="lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null; $SHOW"
fi

if [ ! -f "$IMG" ]; then
    echo "no $IMG yet, skipping push"
    exit 0
fi

# shellcheck disable=SC2086
ssh $SSH_OPTS "root@$KINDLE_IP" "mkdir -p '$KINDLE_DIR'" || { echo "ssh to Kindle failed"; exit 1; }

# -O = classic scp protocol (the Kindle has no sftp-server). Fall back to cat over ssh.
# shellcheck disable=SC2086
if ! scp -O -q $SSH_OPTS "$IMG" "root@$KINDLE_IP:$REMOTE.tmp" 2>/dev/null; then
    ssh $SSH_OPTS "root@$KINDLE_IP" "cat > '$REMOTE.tmp'" < "$IMG" || { echo "copy to Kindle failed"; exit 1; }
fi

# shellcheck disable=SC2086
# Non-interactive ssh on the Kindle has no /usr/sbin in PATH (where eips lives).
# The same call reads the battery, charging state and front light afterwards.
OUT=$(ssh $SSH_OPTS "root@$KINDLE_IP" "export PATH=/usr/sbin:/sbin:/usr/bin:/bin:\$PATH; mv '$REMOTE.tmp' '$REMOTE' && { $SHOW; } >/dev/null && $STATUS_CMD") \
    || { echo "display command failed on Kindle (see README: try DISPLAY_TOOL = \"fbink\")"; exit 1; }
printf '%s\n' "$OUT" | venv/bin/python kindle.py --record-output
echo $((COUNT + 1)) > "$COUNT_FILE"
echo "$SUM $(date +%s)" > out/.last_push
[ $FULL = 1 ] && MODE=full || MODE=partial
[ "$REPEAT" -gt 1 ] && MODE="$REPEAT x full"
echo "pushed $IMG to $KINDLE_IP:$REMOTE ($MODE refresh)"
