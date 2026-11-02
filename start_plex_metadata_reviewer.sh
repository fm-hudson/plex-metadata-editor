#!/usr/bin/env bash
#
# start_plex_metadata_reviewer.sh
#
# PURPOSE:
#   One-click launcher for plex_metadata_reviewer.py (the local web UI for
#   reviewing Plex metadata and saving edits to media files in bulk). It:
#     1. Reuses the reviewer if it's already running on the port, otherwise
#        starts it in the background (keeps running after the terminal closes).
#     2. Waits until the UI answers.
#     3. Opens the UI in a NEW BROWSER WINDOW (never a tab in an existing
#        window). Chrome opens it as a standalone app window; Firefox opens a new window.
#   If there is no graphical display (e.g. run over SSH), it prints the URL
#   and an SSH tunnel command instead of opening a browser.
#
# USAGE:
#   start_plex_metadata_reviewer.sh [--stop]
#
# ARGUMENTS:
#   --stop              Stop the reviewer server running, then exit.
#   -h, --help          Show this help.
#
# OUTPUT:
#   Log:      ~/logs/start_plex_metadata_reviewer_YYYY_MM_DD.log
#   PID file: ~/scripts_meta_data/plex_metadata_reviewer_8766.pid

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDITOR_PY="$SCRIPT_DIR/plex_metadata_reviewer.py"
PYTHON=/usr/bin/python3
LOG_DIR="$HOME/logs"
DATA_DIR="$HOME/scripts_meta_data"
LOG_FILE="$LOG_DIR/start_plex_metadata_reviewer_$(date +%Y_%m_%d).log"
STARTUP_TIMEOUT=60

PORT=8766
BROWSER=chrome
STOP=0

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --stop)    STOP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)"; exit 2 ;;
    esac
done

mkdir -p "$LOG_DIR" "$DATA_DIR"
PID_FILE="$DATA_DIR/plex_metadata_reviewer_${PORT}.pid"
URL="http://127.0.0.1:${PORT}/"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# Is OUR reviewer answering on this port?
editor_up() { curl -s --max-time 2 "$URL" 2>/dev/null | grep -q "<title>Plex Metadata Reviewer</title>"; }
port_busy() { curl -s -o /dev/null --max-time 2 "$URL" 2>/dev/null; }

server_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid; pid=$(cat "$PID_FILE")
        if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE "plex_metadata_reviewer\.py|plex metadata web reviewer"; then
            echo "$pid"; return
        fi
    fi
    pgrep -f "plex_metadata_reviewer\.py|plex metadata web reviewer" | head -1
}

# ---- --stop ----
if [ "$STOP" -eq 1 ]; then
    pid=$(server_pid)
    if [ -z "$pid" ]; then
        log "No reviewer server found -- nothing to stop."
        rm -f "$PID_FILE"; exit 0
    fi
    log "Stopping reviewer server (PID $pid)..."
    kill "$pid"
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$pid" 2>/dev/null; then
        log "Server did not stop after 5s -- forcing (kill -9)."
        kill -9 "$pid"
    fi
    rm -f "$PID_FILE"
    log "Stopped."
    exit 0
fi

# ---- start (or reuse) the server ----
if editor_up; then
    log "Reviewer already running on port $PORT -- reusing it."
elif port_busy; then
    log "ERROR: port $PORT is in use by something other than the reviewer."
    exit 1
else
    if [ ! -f "$EDITOR_PY" ]; then
        log "ERROR: reviewer script not found at $EDITOR_PY"; exit 1
    fi
    log "Starting reviewer server on port $PORT..."
    cd "$SCRIPT_DIR" || exit 1
    setsid nohup "$PYTHON" "$EDITOR_PY" >> "$LOG_FILE" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
    started_pid=$!

    waited=0
    until editor_up; do
        if ! kill -0 "$started_pid" 2>/dev/null; then
            log "ERROR: the reviewer server exited during startup. Last log lines:"
            tail -n 15 "$LOG_FILE"
            rm -f "$PID_FILE"
            exit 1
        fi
        if [ "$waited" -ge "$STARTUP_TIMEOUT" ]; then
            log "ERROR: reviewer didn't answer within ${STARTUP_TIMEOUT}s (still running as PID $started_pid)."
            log "Check $LOG_FILE"
            exit 1
        fi
        sleep 1; waited=$((waited + 1))
    done
    log "Reviewer is up (PID $started_pid) after ${waited}s."
fi

# ---- open a new browser window ----
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    log "No graphical display in this session -- not opening a browser."
    echo "Open $URL on this machine, or from another computer run:"
    echo "  ssh -L ${PORT}:127.0.0.1:${PORT} $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "then open $URL there."
    exit 0
fi

BIN=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || command -v chromium-browser || command -v firefox)
if [ -z "$BIN" ]; then
    log "ERROR: browser not found. Open $URL manually."
    exit 1
fi

if [[ "$BIN" == *"chrome"* ]] || [[ "$BIN" == *"chromium"* ]]; then
    ARGS=(--new-window "--app=$URL")
else
    ARGS=(--new-window "$URL")
fi

log "Opening new browser window: $URL"
setsid nohup "$BIN" "${ARGS[@]}" > /dev/null 2>&1 < /dev/null &
log "Done. Stop the server later with: $0 --stop"
