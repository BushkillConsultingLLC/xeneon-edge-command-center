#!/bin/bash
# Starts the Command Center and opens it full screen on the Xeneon Edge.
cd "$(dirname "$0")"
DIR="$(pwd)"
PROFILE="$HOME/Library/Application Support/CommandCenterChrome"

if ! curl -s -o /dev/null --max-time 1 http://127.0.0.1:8888/health; then
  nohup /usr/bin/python3 "$DIR/server.py" > "$HOME/Library/Logs/CommandCenter.log" 2>&1 &
  disown
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    curl -s -o /dev/null --max-time 1 http://127.0.0.1:8888/health && break
  done
fi

if ! curl -s -o /dev/null --max-time 1 http://127.0.0.1:8888/health; then
  echo ""
  echo "The Command Center didn't start. If macOS asked to install developer tools, finish that, then run this again."
  echo "Log: $HOME/Library/Logs/CommandCenter.log"
  read -r -p "Press Return to close."
  exit 1
fi

# Find the Edge: a screen named XENEON, else the shortest 32:9 screen that isn't the main one.
# Prints x,y,width,height in the top-left coordinates Chrome uses, or nothing.
EDGE="$(osascript -l JavaScript <<'JXA' 2>/dev/null
ObjC.import("AppKit");
var screens = $.NSScreen.screens, main = screens.objectAtIndex(0).frame, best = null;
for (var i = 0; i < screens.count; i++) {
  var s = screens.objectAtIndex(i), f = s.frame;
  var name = "";
  try { name = ObjC.unwrap(s.localizedName) || ""; } catch (e) {}
  var ratio = f.size.width / f.size.height;
  var score = /xeneon/i.test(name) ? 3 : (i > 0 && ratio > 3.3 && ratio < 3.8 ? 2 : 0);
  if (score && (!best || score > best.score || (score === best.score && f.size.height < best.f.size.height)))
    best = { score: score, f: f };
}
var out = "";
if (best) {
  var f = best.f;
  out = [Math.round(f.origin.x), Math.round(main.size.height - (f.origin.y + f.size.height)),
         Math.round(f.size.width), Math.round(f.size.height)].join(",");
}
out;
JXA
)"

# Close an old dashboard window so the new one opens in the right place
pkill -f "user-data-dir=$PROFILE" 2>/dev/null && sleep 1

ARGS=(--app="http://127.0.0.1:8888/" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check)
# Full screen unless switched off in settings
FULL="$(/usr/bin/python3 -c 'import json,sys
try: print("no" if json.load(open(sys.argv[1])).get("prefs",{}).get("fullscreen") is False else "yes")
except Exception: print("yes")' "$DIR/config.json")"
if [ -n "$EDGE" ] && [ "$FULL" = "yes" ]; then
  IFS=, read -r EX EY EW EH <<< "$EDGE"
  # Kiosk = full screen on the display the window starts on, with no title bar.
  # Quit it with Command+Q, or with Stop Command Center.
  ARGS+=(--window-position="$EX,$EY" --window-size="$EW,$EH" --kiosk)
elif [ -n "$EDGE" ]; then
  IFS=, read -r EX EY EW EH <<< "$EDGE"
  ARGS+=(--window-position="$EX,$EY" --window-size="$EW,$EH")
fi
open -na "Google Chrome" --args "${ARGS[@]}"

# Close this Terminal window after launching
osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Command Center")' >/dev/null 2>&1 &
exit 0
