#!/usr/bin/env bash

# start_plex_reviewer_qt.sh
# One-click launcher for the PyQt5 native reviewer app.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QT_APP="$SCRIPT_DIR/plex_reviewer_qt.py"
PYTHON=/usr/bin/python3
LOG_DIR="$HOME/logs"
LOG_FILE="$LOG_DIR/start_plex_reviewer_qt_$(date +%Y_%m_%d).log"

mkdir -p "$LOG_DIR"

if [ ! -f "$QT_APP" ]; then
    echo "ERROR: Qt script not found at $QT_APP"
    exit 1
fi

export DISPLAY="${DISPLAY:-:0}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Plex Metadata Reviewer (Native Qt)..." | tee -a "$LOG_FILE"
cd "$SCRIPT_DIR" || exit 1
setsid nohup "$PYTHON" "$QT_APP" >> "$LOG_FILE" 2>&1 < /dev/null &
echo "Started (PID $!). Logs at: $LOG_FILE"
