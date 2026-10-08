#!/bin/bash
# What runs when Transclip.app is double-clicked. It starts the app in the background and
# leaves at once, so that every later double-click runs again: app.py then finds the one
# already running and just shows it in the browser.
res="$(cd "$(dirname "$0")/../Resources" && pwd)"
mkdir -p "$HOME/Library/Logs"
export PYTHONDONTWRITEBYTECODE=1  # nothing may be written into the app itself
nohup "$res/uv" run --quiet --script "$res/app/app.py" "$@" >> "$HOME/Library/Logs/Transclip.log" 2>&1 &

# The first launch downloads Python and the app's libraries before anything can be shown.
# ponytail: looks at the usual port only, and only to decide whether to say "please wait".
port="${TRANSCLIP_PORT:-8790}"
for _ in 1 2 3 4 5 6 7 8; do
  curl -s -m 1 -o /dev/null "http://127.0.0.1:$port/api/ping" && exit 0
  sleep 0.5
done
[ "$1" = "--no-open" ] || osascript -e 'display dialog "Transclip is starting. The first launch downloads what it needs, which can take a few minutes. Your browser will open when it is ready." with title "Transclip" buttons {"OK"} default button 1 giving up after 30' > /dev/null 2>&1 &
