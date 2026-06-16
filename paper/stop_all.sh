#!/usr/bin/env bash
# paper/stop_all.sh — Stop all helivex paper trading processes.
#
# Reads PIDs from /tmp/helivex_*.pid, sends SIGTERM, then SIGKILL if needed.
# Falls back to pkill by pattern if PID files are missing.

set -uo pipefail

_stop() {
    local name="$1" pid_file="$2" pattern="$3"
    local pid
    if [[ -f "$pid_file" ]]; then
        pid=$(cat "$pid_file" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "[stop_all] Stopping $name PID=$pid..."
            kill "$pid" 2>/dev/null || true
            local i=0
            while kill -0 "$pid" 2>/dev/null && (( i < 8 )); do sleep 1; ((i++)); done
            if kill -0 "$pid" 2>/dev/null; then
                echo "[stop_all] SIGKILL $name PID=$pid"
                kill -9 "$pid" 2>/dev/null || true
            fi
            echo "[stop_all] $name stopped."
        else
            echo "[stop_all] $name PID=$pid already dead."
        fi
        rm -f "$pid_file"
    else
        # Fallback: pkill by pattern
        if pgrep -f "$pattern" > /dev/null 2>&1; then
            echo "[stop_all] No PID file for $name — pkill '$pattern'..."
            pkill -f "$pattern" 2>/dev/null || true
            sleep 2
        else
            echo "[stop_all] $name not running."
        fi
    fi
}

_stop "paper_node" /tmp/helivex_paper_node.pid "paper/run.py"
_stop "gateway"    /tmp/helivex_gw.pid         "uvicorn gateway.main:app"
_stop "monitor"    /tmp/helivex_monitor.pid     "paper/monitor.py"
_stop "frontend"   /tmp/helivex_web.pid         "next dev -p 3400"

echo "[stop_all] All helivex processes stopped."
