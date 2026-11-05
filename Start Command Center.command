#!/bin/bash
# Starts the Command Center and opens it in its own Chrome window.
cd "$(dirname "$0")"
DIR="$(pwd)"

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

open -na "Google Chrome" --args --app="http://127.0.0.1:8888/" \
  --user-data-dir="$HOME/Library/Application Support/CommandCenterChrome" \
  --no-first-run --no-default-browser-check
# Close this Terminal window after launching
osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Command Center")' >/dev/null 2>&1 &
exit 0
