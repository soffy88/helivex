#!/usr/bin/env bash
# paper/start_all.sh — Start / restart the helivex paper trading stack.
#
# Processes managed by systemd user services (auto-restart on death):
#   helivex-paper.service   — NautilusTrader paper node
#   helivex-gw.service      — FastAPI gateway :8765  (Restart=always)
#   helivex-monitor.service — AlerterEngine health watchdog
#   helivex-web.service     — Next.js frontend :3400
#
# PID files: /tmp/helivex_{paper_node,gw,monitor,web}.pid
# Logs:      ~/helivex_{paper,gw,monitor,web}.log
#
# Usage:
#   bash paper/start_all.sh            # start / restart all 4
#   bash paper/start_all.sh stop       # graceful stop all 4
#   bash paper/start_all.sh status     # show systemd status
#   bash paper/start_all.sh restart gw # restart a single unit (paper|gw|monitor|web)
#
# Prerequisites (one-time, already done):
#   loginctl enable-linger soffy
#   systemctl --user enable helivex.target

set -euo pipefail

UNITS="helivex-paper.service helivex-gw.service helivex-monitor.service helivex-web.service"
CMD="${1:-start}"
TARGET="${2:-}"

_unit() {
    case "$TARGET" in
        paper)   echo "helivex-paper.service" ;;
        gw)      echo "helivex-gw.service" ;;
        monitor) echo "helivex-monitor.service" ;;
        web)     echo "helivex-web.service" ;;
        *)       echo "$UNITS" ;;
    esac
}

case "$CMD" in
    stop)
        echo "[start_all] Stopping $(_unit)..."
        systemctl --user stop $(_unit)
        echo "[start_all] Done."
        ;;
    status)
        systemctl --user status $UNITS --no-pager -l
        ;;
    restart)
        echo "[start_all] Restarting $(_unit)..."
        systemctl --user restart $(_unit)
        sleep 2
        systemctl --user status $(_unit) --no-pager -l
        ;;
    start|*)
        echo "[start_all] Starting helivex stack via systemd..."
        systemctl --user daemon-reload
        systemctl --user start $UNITS
        sleep 4
        echo ""
        echo "[start_all] ── Process status ─────────────────────────────────────"
        for unit in $UNITS; do
            name="${unit%.service}"
            state=$(systemctl --user is-active "$unit" 2>/dev/null || echo "failed")
            pid=$(systemctl --user show -p MainPID --value "$unit" 2>/dev/null || echo "?")
            if [[ "$state" == "active" ]]; then
                echo "  ✓ $name  PID=$pid"
            else
                echo "  ✗ $name  state=$state (check: journalctl --user -u $unit -n 20)"
            fi
        done
        echo "[start_all] Frontend → http://localhost:3400"
        echo "[start_all] Gateway  → http://localhost:8765/health"
        echo "[start_all] Logs     → ~/helivex_{paper,gw,monitor,web}.log"
        echo "[start_all] Done."
        ;;
esac
