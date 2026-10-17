#!/bin/bash
# Builds DOOM for the Kindle on the Pi: doomgeneric (https://github.com/ozkl/doomgeneric) with
# our backend (doomgeneric_kindle.c), and fetches Freedoom (free, BSD-licensed game data) if
# there's no game data yet. Nothing is installed system-wide; everything stays in this folder.
#   doom/build.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d doomgeneric ]; then
    echo "downloading doomgeneric..."
    git clone -q --depth 1 https://github.com/ozkl/doomgeneric.git
fi

echo "compiling (a minute or two)..."
SRC=doomgeneric/doomgeneric
OBJS=$(sed -n 's/^SRC_DOOM = //p' "$SRC/Makefile" | tr ' ' '\n' | grep -v '^doomgeneric_xlib.o$' | sed 's/\.o$/.c/')
mkdir -p build
pids=()
for c in $OBJS; do
    o="build/${c%.c}.o"
    if [ ! -f "$o" ] || [ "$SRC/$c" -nt "$o" ]; then
        gcc -O2 -w -DNORMALUNIX -DLINUX -D_DEFAULT_SOURCE -DDOOMGENERIC_RESX=320 -DDOOMGENERIC_RESY=200 \
            -c "$SRC/$c" -o "$o" &
        pids+=($!)
        if [ ${#pids[@]} -ge 4 ]; then wait "${pids[0]}"; pids=("${pids[@]:1}"); fi
    fi
done
for p in "${pids[@]}"; do wait "$p"; done
gcc -O2 -Wall -I"$SRC" -DDOOMGENERIC_RESX=320 -DDOOMGENERIC_RESY=200 -c doomgeneric_kindle.c -o build/kindle.o
gcc -o doom-kindle build/*.o -lm
echo "built $(pwd)/doom-kindle"

mkdir -p wads
if ! ls wads/*.wad >/dev/null 2>&1; then
    echo "downloading Freedoom (free DOOM-compatible game data)..."
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/freedoom.zip" https://github.com/freedoom/freedoom/releases/download/v0.13.0/freedoom-0.13.0.zip
    python3 -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); [open('wads/'+n.split('/')[-1],'wb').write(z.read(n)) for n in z.namelist() if n.endswith('.wad')]" "$tmp/freedoom.zip"
    rm -rf "$tmp"
fi
ls wads
