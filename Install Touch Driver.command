#!/bin/bash
# Installs MacXeneonEdgeTouchDriver (MIT, github.com/ajvwhite/MacXeneonEdgeTouchDriver)
# at a pinned, reviewed commit so taps on the Xeneon Edge become clicks.
# No admin password needed. Re-run any time; it's safe to repeat.
set -euo pipefail

REPO="https://github.com/ajvwhite/MacXeneonEdgeTouchDriver.git"
COMMIT="52dac0e2227802597ea422a384204a246312bbb6"
SRC="$HOME/Library/Application Support/CommandCenter/touch-driver"
BIN="$HOME/Library/Application Support/MacXeneonEdgeTouchDriver/bin/MacXeneonEdgeTouchDriver"

echo ""
echo "=== Xeneon Edge touch driver ==="
echo ""

if ! command -v swift >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  echo "Apple's developer tools are needed. A window will ask to install them."
  echo "When that finishes, double-click this installer again."
  xcode-select --install 2>/dev/null || true
  read -r -p "Press Return to close."
  exit 1
fi

mkdir -p "$(dirname "$SRC")"
if [ ! -d "$SRC/.git" ]; then
  echo "Downloading the driver..."
  git clone -q "$REPO" "$SRC"
fi
cd "$SRC"
git cat-file -e "$COMMIT^{commit}" 2>/dev/null || git fetch -q origin
git checkout -q --detach "$COMMIT"
git reset -q --hard "$COMMIT"
git clean -qfdx
[ "$(git rev-parse HEAD)" = "$COMMIT" ] || { echo "Driver isn't at the reviewed version. Stopping."; exit 1; }

echo "Building (this takes a minute or two the first time)..."
./Scripts/install.sh

echo ""
echo "==================================================================="
echo " Almost done: two switches to turn on"
echo "==================================================================="
echo ""
echo " System Settings is opening. In BOTH of these, click +, press"
echo " Command+Shift+G, paste the path below, and turn the driver on:"
echo ""
echo "   1. Privacy & Security > Accessibility"
echo "   2. Privacy & Security > Input Monitoring"
echo ""
echo " Path:"
echo "   $BIN"
echo ""
printf '%s' "$BIN" | pbcopy
echo " (The path is already copied. Just paste it.)"
echo ""
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
read -r -p "When Accessibility is done, press Return to open Input Monitoring..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
read -r -p "When Input Monitoring is done, press Return to start touch..."

launchctl kickstart -k "gui/$(id -u)/com.ajvwhite.MacXeneonEdgeTouchDriver" 2>/dev/null || true
sleep 2
if "$BIN" --check-permissions >/dev/null 2>&1; then
  echo ""
  echo "Touch is on. Tap something on the Edge to try it."
else
  echo ""
  echo "One of the two switches isn't on yet. Check both lists, then run this again."
fi
read -r -p "Press Return to close."
