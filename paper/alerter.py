"""paper.alerter — AlerterEngine wired for helivex paper trading health.

Evaluators (oprim):
  eval_node_alive     — paper/run.py PID alive
  eval_ws_tick_flow   — OKX WS producing ticks (via signal recency)
  eval_audit_chain    — Ed25519 sig_b64 populated (GOLD tier)
  eval_on_bar_trigger — on_bar fired within lag budget after bar close

Channels:
  log_channel  — always active (Python logger → console/file)
  tg_channel   — active if TG_BOT_TOKEN + TG_CHAT_ID env vars are set

Usage (from paper/monitor.py):
  from paper.alerter import build_alerter
  engine = build_alerter()
  engine.run()  # blocks; wrap in threading.Thread for background
"""
from __future__ import annotations

import logging
import os

from oservi.engines.alerter import AlerterEngine

from paper.evaluators import (
    eval_audit_chain,
    eval_backup_freshness,
    eval_deadman_heartbeat,
    eval_gateway_alive,
    eval_l2_recorder_flow,
    eval_node_alive,
    eval_on_bar_trigger,
    eval_web_alive,
    eval_write_freshness,
    eval_ws_tick_flow,
)
from paper.risk import eval_daily_loss, eval_portfolio_drawdown

log = logging.getLogger(__name__)


# ── channels ──────────────────────────────────────────────────────────────────

def log_channel(*, text: str, **_: object) -> None:
    """Always-on channel: emit alert to Python logger."""
    log.warning("[PAPER ALERT] %s", text)


def tg_channel(*, text: str, bot_token: str = "", chat_id: str = "", **_: object) -> None:
    """Telegram channel — active only when bot_token + chat_id are provided.

    The AlerterEngine invokes channels from within its running event loop, so
    asyncio.run() here raised "cannot be called from a running event loop" and
    every alert silently failed to send. Run the send in a dedicated thread with
    its own loop — works whether or not the caller has a running loop.
    """
    if not bot_token or not chat_id:
        return
    import asyncio
    import threading
    from obase.notify.telegram import TelegramRequest, telegram_send

    async def _send():
        req = TelegramRequest(bot_token=bot_token, chat_id=chat_id, text=text)
        result = await telegram_send(req)
        if not result.ok:
            log.warning("TG send failed: %s", result.error)

    def _runner():
        try:
            asyncio.run(_send())
        except Exception as e:  # never let a channel error break the alerter loop
            log.warning("TG send error: %s", e)

    t = threading.Thread(target=_runner, daemon=True, name="tg-send")
    t.start()
    t.join(timeout=10)


# ── factory ───────────────────────────────────────────────────────────────────

def build_alerter() -> AlerterEngine:
    """Build and return a configured AlerterEngine for helivex paper trading."""
    tg_bot   = os.environ.get("TG_BOT_TOKEN", "")
    tg_chat  = os.environ.get("TG_CHAT_ID", "")

    channels = [log_channel]
    if tg_bot and tg_chat:
        channels.append(tg_channel)
        log.info("TG channel active (chat_id=%s)", tg_chat)
    else:
        log.info("TG channel disabled (set TG_BOT_TOKEN + TG_CHAT_ID to enable)")

    engine = AlerterEngine(
        name="helivex-paper-health",
        evaluators=[
            eval_node_alive,
            eval_ws_tick_flow,
            eval_audit_chain,
            eval_on_bar_trigger,
            eval_gateway_alive,
            eval_web_alive,
            eval_portfolio_drawdown,
            eval_daily_loss,
            eval_l2_recorder_flow,
            eval_write_freshness,
            eval_backup_freshness,
            eval_deadman_heartbeat,
        ],
        channels=channels,
        trigger={"on_interval": 120},  # check every 2 minutes
        config={
            "throttle_seconds":      600,   # same alert max once per 10 min
            "dedup_bucket_seconds":  3600,  # same alert max once per hour
            "channel_configs": {
                "tg_channel": {
                    "bot_token": tg_bot,
                    "chat_id":   tg_chat,
                },
            },
            "evaluator_configs": {
                "eval_node_alive": {
                    "pid_file": "/tmp/helivex_paper_node.pid",
                },
                "eval_ws_tick_flow": {
                    "stale_seconds": 70 * 60,  # 70 min (1H bar + buffer)
                },
                "eval_audit_chain": {
                    "verify_n": 5,
                },
                "eval_on_bar_trigger": {
                    "lag_budget_seconds": 5 * 60,  # 5 min after bar close
                },
                "eval_gateway_alive": {
                    "port": 8765,
                    "timeout": 5.0,
                },
                "eval_web_alive": {
                    "port": 3400,
                    "timeout": 5.0,
                },
                "eval_portfolio_drawdown": {
                    "max_drawdown_pct": 15.0,
                },
                "eval_daily_loss": {
                    "daily_loss_limit_usd": 250.0,
                },
                "eval_l2_recorder_flow": {
                    "stale_seconds": 5 * 60,
                },
                "eval_write_freshness": {
                    "stale_seconds": 15 * 60,  # 3× the 5-min scalp signal cadence
                    "pid_file": "/tmp/helivex_paper_node.pid",
                },
                "eval_backup_freshness": {
                    "max_age_hours": 26,
                },
            },
        },
    )
    return engine
