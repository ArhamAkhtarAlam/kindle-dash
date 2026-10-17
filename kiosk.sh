#!/bin/sh
# Pause or resume the Kindle's own UI, so taps only reach the dashboard.
#   kiosk.sh on      stop the Kindle UI (reader, home screen), then redraw the dashboard
#   kiosk.sh off     start the Kindle UI again (you can read books again)
#   kiosk.sh status
# A Kindle restart (hold power ~40 s) also brings the UI back.
set -u
cd "$(dirname "$0")"
eval "$(venv/bin/python render.py --print-config)"   # SSH_OPTS
KINDLE_IP=$(venv/bin/python kindle.py --ip)          # USB, or Wi-Fi if unplugged

case "${1:-status}" in
    on)     CMD="initctl stop lab126_gui 2>/dev/null || /etc/init.d/framework stop; initctl stop webreader 2>/dev/null; echo 'Kindle UI stopped'" ;;
    off)    CMD="initctl start lab126_gui 2>/dev/null || /etc/init.d/framework start; echo 'Kindle UI starting'" ;;
    status) CMD="initctl status lab126_gui 2>/dev/null || echo 'lab126_gui job not found'" ;;
    *)      echo "usage: kiosk.sh on|off|status"; exit 2 ;;
esac

if [ -z "$KINDLE_IP" ]; then
    echo "Kindle not reachable (USB or Wi-Fi)"
    exit 1
fi
# shellcheck disable=SC2086
ssh $SSH_OPTS "root@$KINDLE_IP" "export PATH=/usr/sbin:/sbin:/usr/bin:/bin:\$PATH; $CMD"
# The UI may have drawn over the screen while stopping; put the dashboard back.
[ "$1" = on ] && sleep 2 && ./push.sh --now
exit 0
