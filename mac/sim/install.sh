#!/bin/bash
# Builds "Kindle Sim.app" in ~/Applications (the dashboard on the Mac, for testing) and the
# Python it runs on. Safe to run again: it only rebuilds the app and its own venv.
#   mac/sim/install.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SIM_HOME="$HOME/Library/Application Support/Kindle Sim"
APP="$HOME/Applications/Kindle Sim.app"
# python.org's Python has a working Tk (Homebrew's doesn't, and Apple's is the old Tk 8.5)
PY=""
for p in /Library/Frameworks/Python.framework/Versions/3.1[3-9]/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.1[0-2]/bin/python3; do
    [ -x "$p" ] && "$p" -c 'import tkinter; assert tkinter.TkVersion >= 8.6' 2>/dev/null && { PY="$p"; break; }
done
[ -n "$PY" ] || { echo "needs Python with Tk 8.6 from python.org (https://www.python.org/downloads/macos/)"; exit 1; }

mkdir -p "$SIM_HOME"
if [ ! -x "$SIM_HOME/venv/bin/python" ]; then
    echo "making the simulator's Python ($("$PY" --version))..."
    "$PY" -m venv "$SIM_HOME/venv"
fi
"$SIM_HOME/venv/bin/pip" -q install --disable-pip-version-check pillow numpy icalendar recurring-ical-events

if ! ls "$SIM_HOME/fonts/DejaVuSansMono.ttf" >/dev/null 2>&1; then
    echo "copying the Pi's DejaVu fonts (the weather and chess use them)..."
    mkdir -p "$SIM_HOME/fonts"
    scp -q "${KINDLE_PI:-arham@arham-pi}:/usr/share/fonts/truetype/dejavu/DejaVuSans*.ttf" "$SIM_HOME/fonts/" \
        || echo "  couldn't reach the Pi; the weather screens will be blank until you run this again"
fi

echo "drawing the icon..."
"$SIM_HOME/venv/bin/python" "$HERE/make_icon.py" "$HERE/icon.png"
ICONSET="$(mktemp -d)/sim.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
    sips -z $s $s "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done

echo "building $APP..."
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Kindle Sim</string>
  <key>CFBundleDisplayName</key><string>Kindle Sim</string>
  <key>CFBundleIdentifier</key><string>local.kindle-dash.sim</string>
  <key>CFBundleExecutable</key><string>kindle-sim</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
cat > "$APP/Contents/MacOS/kindle-sim" <<EOF
#!/bin/bash
exec "$SIM_HOME/venv/bin/python" "$HERE/app.py" >>"$SIM_HOME/app.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/kindle-sim"
touch "$APP"
echo "done: open it from ~/Applications (or Spotlight: Kindle Sim), or run: kindle sim"
