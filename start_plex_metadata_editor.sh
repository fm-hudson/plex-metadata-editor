#!/usr/bin/env bash
#
# start_plex_metadata_editor.sh
#
# PURPOSE:
#   One-click launcher for plex_metadata_editor.py (the local web UI for
#   viewing Plex metadata and saving edits to media files). It:
#     1. Reuses the editor if it's already running on the port, otherwise
#        starts it in the background (keeps running after the terminal
#        closes).
#     2. Waits until the UI actually answers (up to 60s -- startup includes
#        indexing Plex).
#     3. Opens the UI in a NEW BROWSER WINDOW (never a tab in an existing
#        window). Chrome opens it as a standalone app window (no tabs or
#        address bar); Firefox opens a new window.
#   If there is no graphical display (e.g. run over SSH), it prints the URL
#   and an SSH tunnel command instead of opening a browser.
#
# USAGE:
#   start_plex_metadata_editor.sh [--port N] [--browser chrome|firefox] [--stop]
#
# ARGUMENTS:
#   --port N            Port for the editor's web server. Default: 8765.
#   --browser NAME      chrome (default) or firefox.
#   --stop              Stop the editor server running on --port, then exit.
#   -h, --help          Show this help.
#
# EXAMPLES:
#   ~/myScripts/start_plex_metadata_editor.sh
#   ~/myScripts/start_plex_metadata_editor.sh --browser firefox
#   ~/myScripts/start_plex_metadata_editor.sh --port 9000
#   ~/myScripts/start_plex_metadata_editor.sh --stop
#
# OUTPUT:
#   Log:      ~/logs/start_plex_metadata_editor_YYYY_MM_DD.log
#             (launcher steps + the server's console output)
#   PID file: ~/scripts_meta_data/plex_metadata_editor_<port>.pid
#   The editor itself also writes its own log/settings -- see the usage
#   header in plex_metadata_editor.py.
#
# SIDE EFFECTS:
#   Starts a background python3 process (the editor server) that keeps
#   running until stopped with --stop. Opens a browser window.
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDITOR_PY="$SCRIPT_DIR/plex_metadata_editor.py"
PYTHON=/usr/bin/python3
LOG_DIR="$HOME/logs"
DATA_DIR="$HOME/scripts_meta_data"
LOG_FILE="$LOG_DIR/start_plex_metadata_editor_$(date +%Y_%m_%d).log"
STARTUP_TIMEOUT=60

PORT=8765
BROWSER=chrome
STOP=0

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)    PORT="${2:-}"; shift 2 ;;
        --browser) BROWSER="${2:-}"; shift 2 ;;
        --stop)    STOP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)"; exit 2 ;;
    esac
done

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo "--port must be a number, got '$PORT'"; exit 2
fi
case "$BROWSER" in chrome|firefox) ;; *) echo "--browser must be chrome or firefox, got '$BROWSER'"; exit 2 ;; esac

mkdir -p "$LOG_DIR" "$DATA_DIR"
PID_FILE="$DATA_DIR/plex_metadata_editor_${PORT}.pid"
URL="http://127.0.0.1:${PORT}/"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# Is OUR editor answering on this port? (checks the page title, not just "something is listening")
editor_up() { curl -s --max-time 2 "$URL" 2>/dev/null | grep -q "<title>Plex Metadata Editor</title>"; }
port_busy() { curl -s -o /dev/null --max-time 2 "$URL" 2>/dev/null; }

server_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid; pid=$(cat "$PID_FILE")
        if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE "plex_metadata_editor\.py|plex metadata web editor \(port $PORT\)"; then
            echo "$pid"; return
        fi
    fi
    pgrep -f "plex_metadata_editor\.py --port $PORT( |$)|plex metadata web editor \(port $PORT\)" | head -1
}

# ---- --stop ----
if [ "$STOP" -eq 1 ]; then
    pid=$(server_pid)
    if [ -z "$pid" ]; then
        log "No editor server found on port $PORT -- nothing to stop."
        rm -f "$PID_FILE"; exit 0
    fi
    log "Stopping editor server (PID $pid, port $PORT)..."
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
    log "Editor already running on port $PORT -- reusing it."
elif port_busy; then
    log "ERROR: port $PORT is in use by something other than the editor. Pick another with --port."
    exit 1
else
    if [ ! -f "$EDITOR_PY" ]; then
        log "ERROR: editor script not found at $EDITOR_PY"; exit 1
    fi
    log "Starting editor server on port $PORT..."
    cd "$SCRIPT_DIR" || exit 1
    setsid nohup "$PYTHON" "$EDITOR_PY" --port "$PORT" >> "$LOG_FILE" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
    started_pid=$!

    waited=0
    until editor_up; do
        if ! kill -0 "$started_pid" 2>/dev/null; then
            log "ERROR: the editor server exited during startup. Last log lines:"
            tail -n 15 "$LOG_FILE"
            rm -f "$PID_FILE"
            exit 1
        fi
        if [ "$waited" -ge "$STARTUP_TIMEOUT" ]; then
            log "ERROR: editor didn't answer within ${STARTUP_TIMEOUT}s (still running as PID $started_pid)."
            log "It may be slow indexing Plex -- try $URL in a minute, or check $LOG_FILE"
            exit 1
        fi
        sleep 1; waited=$((waited + 1))
    done
    log "Editor is up (PID $started_pid) after ${waited}s."
fi

# ---- open a new browser window ----
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    log "No graphical display in this session -- not opening a browser."
    echo "Open $URL on this machine, or from another computer run:"
    echo "  ssh -L ${PORT}:127.0.0.1:${PORT} $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "then open $URL there."
    exit 0
fi

if [ "$BROWSER" = chrome ]; then
    BIN=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || command -v chromium-browser)
    ARGS=(--new-window "--app=$URL")
else
    BIN=$(command -v firefox)
    ARGS=(--new-window "$URL")
fi
if [ -z "$BIN" ]; then
    log "ERROR: $BROWSER not found. Open $URL manually, or use --browser $( [ "$BROWSER" = chrome ] && echo firefox || echo chrome )."
    exit 1
fi

log "Opening new $BROWSER window: $URL"
setsid nohup "$BIN" "${ARGS[@]}" > /dev/null 2>&1 < /dev/null &
log "Done. Stop the server later with: $0 --stop$( [ "$PORT" != 8765 ] && echo " --port $PORT")"
