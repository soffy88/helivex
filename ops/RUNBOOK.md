# Helivex Ops Runbook

Single-operator paper-trading stack on one WSL2 host. 6 systemd **user** services
under `helivex.target` + backup/logrotate timers. All config in `ops/` is version-
controlled; install with `bash ops/systemd/install.sh`.

## Services
| unit | role | restart |
|---|---|---|
| helivex-gw | FastAPI gateway, **127.0.0.1:8765** (token-auth on mutating routes) | always |
| helivex-paper | NautilusTrader paper node (OKX Demo) | on-failure |
| helivex-web | Next.js dashboard :3400 (Basic Auth on non-local) | always |
| helivex-l2recorder | L2 order-book recorder | always |
| helivex-monitor | health/circuit-breaker alerter (Telegram) | always |
| helivex-cf | Cloudflare tunnel → btc.uex.hk | always |
| helivex-backup.timer | nightly pg_dump 02:00 (Persistent) | timer |
| helivex-logrotate.timer | log rotation 03:30 | timer |

## Secrets
- `.env` is `0600` and holds OKX keys, the Ed25519 audit key, TG token,
  `HELIVEX_GW_TOKEN`, and `DASH_USER`/`DASH_PASS`. Both gw and web load it via
  `EnvironmentFile`. **Never** put secrets back in a unit file (world-readable).
- Rotate the dashboard password: edit `DASH_PASS` in `.env`, `systemctl --user restart helivex-web`.
- Rotate the gateway token: edit `HELIVEX_GW_TOKEN` in `.env`, restart `helivex-gw` + `helivex-web`.

## Backups & restore
- Nightly `ops/backup/pg_dump_daily.sh`: pg_dump → integrity check (`pg_restore --list`)
  → off-host copy to `/mnt/c/helivex_backups` (Windows volume, survives WSL reset) → prune 7d.
- **Restore drill** (run periodically): `bash ops/backup/restore_drill.sh` — restores
  the latest dump into a scratch DB using the TimescaleDB pre/post-restore procedure
  and compares row counts. A dump that hasn't been restore-tested is not a backup.
- The monitor's `eval_backup_freshness` alerts if the newest dump is >26h old.

## Monitoring & self-healing
- `helivex-monitor` runs evaluators every 120s → Telegram. Key breakers:
  - `eval_write_freshness` — **node alive but DB writes frozen** (the 2026-06-30 silent
    11h gap). Alert-only (a node bounce is a human decision).
  - `eval_l2_recorder_flow` — L2 stalled → **auto-restarts** the (data-only) recorder.
  - `eval_portfolio_drawdown` / `eval_daily_loss` — trip the kill-switch.
  - `eval_deadman_heartbeat` — pings `HELIVEX_DEADMAN_URL` each cycle. **Set this**
    (e.g. a healthchecks.io URL) so monitor death / host-off / network-down is caught
    externally — the one failure the in-process alerter cannot self-report.

## Known single points of failure (accepted / TODO)
- **Whole stack + Postgres + primary backups live on one WSL2 host.** Off-host dump
  copy to `/mnt/c` mitigates data loss, but host loss = full outage. Moving Postgres
  off-box (or a replica) is the next DR step and is **not yet done**.
- The gateway/paper/L2 nodes do not yet emit `sd_notify`, so `WatchdogSec` liveness
  is not wired; supervision is exit-code + monitor-driven. (Hung-but-alive on the L2
  recorder is covered by the staleness auto-restart; the trading node is alert-only.)
