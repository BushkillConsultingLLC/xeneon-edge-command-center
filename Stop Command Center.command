#!/bin/bash
# Stops the Command Center server and its Chrome window.
DIR="$(cd "$(dirname "$0")" && pwd)"
pkill -f "$DIR/server.py" 2>/dev/null
pkill -f "user-data-dir=$HOME/Library/Application Support/CommandCenterChrome" 2>/dev/null
echo "Command Center stopped."
