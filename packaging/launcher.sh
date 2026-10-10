#!/bin/bash
# What runs when Transclip.app is opened. It has uv put Python and the app's libraries in place
# (downloaded on the first launch and after an update; otherwise there is nothing to do), then
# turns into that Python. Turning into it, rather than starting it, is what makes the window
# belong to this app as far as macOS is concerned: Transclip's icon in the Dock, one copy running.
res="$(cd "$(dirname "$0")/../Resources" && pwd)"
mkdir -p "$HOME/Library/Logs"
exec >> "$HOME/Library/Logs/Transclip.log" 2>&1
export UV="$res/uv"               # app.py starts the transcriber with it
export PYTHONDONTWRITEBYTECODE=1  # nothing may be written into the app itself

# If that takes more than a few seconds, say why nothing is on screen yet; the notice goes when it is done.
if [ "$1" != "--no-open" ]; then
  { sleep 4 && exec osascript -e 'display dialog "Transclip is getting ready. The first launch downloads what it needs, which can take a few minutes. Its window opens when that is done." with title "Transclip" buttons {"OK"} default button 1 giving up after 900' > /dev/null; } &
  notice=$!
fi
# --frozen: exactly what the lock file beside app.py names, and never a write into the app.
if ! "$UV" sync --quiet --frozen --script "$res/app/app.py"; then
  [ -n "$notice" ] && kill "$notice" && osascript -e 'display alert "Transclip could not get ready" message "The first launch needs the internet. The details are in Library/Logs/Transclip.log in your home folder."' > /dev/null
  exit 1
fi
[ -n "$notice" ] && kill "$notice"
exec "$("$UV" python find --script "$res/app/app.py")" "$res/app/app.py" "$@"
