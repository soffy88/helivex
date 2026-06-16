#!/usr/bin/env bash
# paper/start_all.sh — Start all helivex paper trading processes in background.
#
# Processes:
#   1. paper node   (paper/run.py)          → PID: /tmp/helivex_paper_node.pid
#   2. gateway      (uvicorn gateway:8765)   → PID: /tmp/helivex_gw.pid
#   3. monitor      (paper/monitor.py)       → PID: /tmp/helivex_monitor.pid
#   4. frontend     (next dev :3400)         → PID: /tmp/helivex_web.pid
#
# .env is loaded BEFORE every process so no config is ever missing on restart.
#
# Usage:
#   cd /home/soffy/projects/helivex
#   bash paper/start_all.sh
#
# Logs:
#   ~/helivex_paper.log   — paper node
#   ~/helivex_gw.log      — gateway
#   ~/helivex_monitor.log — monitor
#   ~/helivex_web.log     — frontend

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/venv/bin"

echo "[start_all] Root: $ROOT"

# ── Load .env ─────────────────────────────────────────────────────────────────
# Source into shell AND export so child processes inherit all vars.
if [[ -f "$ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ROOT/.env"
    set +a
    echo "[start_all] .env loaded ($(grep -c '=' "$ROOT/.env") vars)"
else
    echo "[start_all] WARNING: $ROOT/.env not found — proceeding without it"
fi

# ── Kill stale processes from previous run ────────────────────────────────────
_kill_pid_file() {
    local pid_file="$1"
    if [[ -f "$pid_file" ]]; then
        local pid; pid=$(cat "$pid_file" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "[start_all] Stopping stale PID=$pid ($pid_file)"
            kill "$pid" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$pid_file"
    fi
}

_kill_pid_file /tmp/helivex_paper_node.pid
_kill_pid_file /tmp/helivex_gw.pid
_kill_pid_file /tmp/helivex_monitor.pid
_kill_pid_file /tmp/helivex_web.pid

# Allow ports to free up
sleep 1

# ── 1. Paper node ─────────────────────────────────────────────────────────────
echo "[start_all] Starting paper node..."
cd "$ROOT"
nohup "$VENV/python" "$ROOT/paper/run.py" > ~/helivex_paper.log 2>&1 &
NODE_PID=$!
echo "$NODE_PID" > /tmp/helivex_paper_node.pid
echo "[start_all] Paper node PID=$NODE_PID  log=~/helivex_paper.log"

# ── 2. Gateway ───────────────────────────────────────────────────────────────
echo "[start_all] Starting gateway..."
# env vars already exported; uvicorn inherits them — HELIVEX_AUDIT_PUBLIC_KEY_B64 included
nohup "$VENV/uvicorn" gateway.main:app --host 0.0.0.0 --port 8765 \
    > ~/helivex_gw.log 2>&1 &
GW_PID=$!
echo "$GW_PID" > /tmp/helivex_gw.pid
echo "[start_all] Gateway PID=$GW_PID  log=~/helivex_gw.log"

# ── 3. Monitor ───────────────────────────────────────────────────────────────
echo "[start_all] Starting health monitor..."
nohup "$VENV/python" "$ROOT/paper/monitor.py" > ~/helivex_monitor.log 2>&1 &
MON_PID=$!
echo "$MON_PID" > /tmp/helivex_monitor.pid
echo "[start_all] Monitor PID=$MON_PID  log=~/helivex_monitor.log"

# ── 4. Frontend ──────────────────────────────────────────────────────────────
echo "[start_all] Starting frontend (port 3400)..."
cd "$ROOT/helivex-web"
nohup node_modules/.bin/next dev -p 3400 > ~/helivex_web.log 2>&1 &
WEB_PID=$!
echo "$WEB_PID" > /tmp/helivex_web.pid
echo "[start_all] Frontend PID=$WEB_PID  log=~/helivex_web.log"
cd "$ROOT"

# ── Summary ───────────────────────────────────────────────────────────────────
sleep 3
echo ""
echo "[start_all] ── Process status ─────────────────────────────────────"
for entry in \
    "paper_node:/tmp/helivex_paper_node.pid" \
    "gateway:/tmp/helivex_gw.pid" \
    "monitor:/tmp/helivex_monitor.pid" \
    "frontend:/tmp/helivex_web.pid"; do
    name="${entry%%:*}"; pidfile="${entry##*:}"
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "  ✓ $name  PID=$pid"
    else
        echo "  ✗ $name  DEAD (check log)"
    fi
done
echo "[start_all] Frontend → http://localhost:3400"
echo "[start_all] Gateway  → http://localhost:8765/health"
echo "[start_all] Done."
