#!/usr/bin/env bash
# paper/start.sh — Start helivex paper trading (node + health monitor)
#
# Process 1 (node):    paper/run.py  → PID written to /tmp/helivex_paper_node.pid
# Process 2 (monitor): paper/monitor.py → AlerterEngine, checks every 2 min
#
# Usage:
#   cd /home/soffy/projects/helivex
#   bash paper/start.sh
#
# Logs:
#   nohup.out          — node stdout (if run via nohup)
#   paper_monitor.log  — monitor log

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/venv/bin/python"

echo "[start.sh] helivex paper trading startup"
echo "[start.sh] Root: $ROOT"

# ── 1. Kill any stale processes ──────────────────────────────────────────────
for pid_file in /tmp/helivex_paper_node.pid /tmp/helivex_paper_monitor.pid; do
    if [[ -f "$pid_file" ]]; then
        stale_pid=$(cat "$pid_file" 2>/dev/null || true)
        if [[ -n "$stale_pid" ]] && kill -0 "$stale_pid" 2>/dev/null; then
            echo "[start.sh] Stopping stale process PID=$stale_pid"
            kill "$stale_pid" 2>/dev/null || true
            sleep 2
        fi
        rm -f "$pid_file"
    fi
done

# ── 2. Start monitor (process 2) ─────────────────────────────────────────────
echo "[start.sh] Starting health monitor..."
nohup "$VENV" "$ROOT/paper/monitor.py" >> "$ROOT/paper_monitor.log" 2>&1 &
MONITOR_PID=$!
echo "$MONITOR_PID" > /tmp/helivex_paper_monitor.pid
echo "[start.sh] Monitor PID=$MONITOR_PID  log=paper_monitor.log"

# ── 3. Start paper node (process 1) ──────────────────────────────────────────
echo "[start.sh] Starting paper node..."
# Run in foreground so terminal shows node output; Ctrl+C stops both.
trap 'echo "[start.sh] Caught signal, stopping monitor..."; kill $MONITOR_PID 2>/dev/null || true' EXIT INT TERM

cd "$ROOT"
"$VENV" "$ROOT/paper/run.py"
