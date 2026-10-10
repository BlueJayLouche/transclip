#!/usr/bin/env bash
# Builds dist/Transclip.app:  ./package-macos.sh [version]
#
# The app is the two Python programs, the page, its icon (drawn here by packaging/icon.py) and a
# copy of uv, which on first launch fetches Python and the libraries the lock files name.
# Nothing else has to be installed.
# The speech model is downloaded the first time something is transcribed.
# It is signed ad hoc, not by a registered developer: a Mac that downloaded it will refuse
# to open it until told to (see README).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
version=${1:-0.0.0}
app="$root/dist/Transclip.app"
uv=$(command -v uv) || { echo "uv is needed to build: https://docs.astral.sh/uv/" >&2; exit 1; }

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/app"
cp "$root/packaging/launcher.sh" "$app/Contents/MacOS/Transclip"
chmod +x "$app/Contents/MacOS/Transclip"
cp "$uv" "$app/Contents/Resources/uv"   # the Mac it is built on decides the chip it runs on
cp "$root"/app.py "$root"/app.py.lock "$root"/transcribe.py "$root"/transcribe.py.lock "$app/Contents/Resources/app/"
cp -R "$root/static" "$app/Contents/Resources/app/"
"$uv" run --quiet --script "$root/packaging/icon.py" "$app/Contents/Resources/AppIcon.icns"
sed "s/__VERSION__/$version/g" "$root/packaging/Info.plist.tmpl" > "$app/Contents/Info.plist"

codesign --force --deep -s - "$app"
codesign --verify --deep --strict "$app"
echo "Packaged: $app ($version, $(file -b "$app/Contents/Resources/uv" | sed 's/.*executable //'))"
