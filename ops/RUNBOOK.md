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
| helivex-monitor | health/circuit-breaker alerter (Telegram) | always |
| helivex-cf | Cloudflare tunnel → btc.uex.hk | always |
| helivex-backup.timer | pg_dump every 6h (Persistent) | timer |
| helivex-logrotate.timer | log rotation 03:30 | timer |
| helivex-ohlcv-md-adapter.timer | OHLCV 4H/5m (okx_swap) + 1H (okx spot) from iris/md (DB→DB) | timer |
| helivex-funding-md-adapter.timer | funding from iris/md (DB→DB) | timer |
| helivex-orderbook-md-adapter.timer | L2 features from iris/md, OKX LIVE (DB→DB) | timer |
| helivex-cvar-risk-adapter.timer | CVaR 组合权重 + 3 层动态仓位上限(3O omodul,Stage A 仅观察,不接实盘)| timer |

`helivex-l2recorder` (NautilusTrader L2 recorder, OKX DEMO) is **retired** —
superseded by `helivex-orderbook-md-adapter.timer` above. Stopped via `docker stop`
and removed from `install.sh`'s enable loop; its unit file stays on disk for
rollback only. If it still shows enabled on this host (`systemctl --user
is-enabled helivex-l2recorder`), disable it — re-enabling it mixes OKX DEMO data
back into the same `market_data.orderbook_features` table the adapter now feeds
from OKX LIVE.

`helivex-ingest-ohlcv-{1h,4h,5m}` and `helivex-ingest-funding-binance` (direct
OKX/Binance REST collectors) are all **retired** — superseded by
`helivex-ohlcv-md-adapter.timer` / `helivex-funding-md-adapter.timer` above.
`install.sh` excludes their `.timer` files from its enable loop (`RETIRED_TIMERS`)
so a re-install can't silently resurrect them; the `.service`/`.timer` files stay
on disk for rollback only.

## Secrets
- `.env` is `0600` and holds OKX keys, the Ed25519 audit key, TG token,
  `HELIVEX_GW_TOKEN`, and `DASH_USER`/`DASH_PASS`. Both gw and web load it via
  `EnvironmentFile`. **Never** put secrets back in a unit file (world-readable).
- Rotate the dashboard password: edit `DASH_PASS` in `.env`, `systemctl --user restart helivex-web`.
- Rotate the gateway token: edit `HELIVEX_GW_TOKEN` in `.env`, restart `helivex-gw` + `helivex-web`.

## Backups & restore
- Every 6h `ops/backup/pg_dump_daily.sh`: pg_dump → integrity check (`pg_restore --list`)
  → off-host copy to `/mnt/c/helivex_backups` (Windows volume, survives WSL reset) → prune 7d.
  6h cadence bounds data-loss of the irreplaceable forward-collected tables to ≤6h.
- **Restore drill** (run periodically): `bash ops/backup/restore_drill.sh` — restores
  the latest dump into a scratch DB using the TimescaleDB pre/post-restore procedure
  and compares row counts. A dump that hasn't been restore-tested is not a backup.
- The monitor's `eval_backup_freshness` alerts if the newest dump is >26h old.

## Monitoring & self-healing
- `helivex-monitor` runs evaluators every 120s → Telegram. Key breakers:
  - `eval_write_freshness` — **node alive but DB writes frozen** (the 2026-06-30 silent
    11h gap). Alert-only (a node bounce is a human decision).
  - `eval_l2_recorder_flow` — L2 features stalled → **auto-restarts** the
    (data-only) `helivex-orderbook-md-adapter` timer, not the retired recorder.
  - `eval_portfolio_drawdown` / `eval_daily_loss` — trip the kill-switch.
  - `eval_deadman_heartbeat` — pings `HELIVEX_DEADMAN_URL` each cycle. **Set this**
    (e.g. a healthchecks.io URL) so monitor death / host-off / network-down is caught
    externally — the one failure the in-process alerter cannot self-report.

## Watchdog (wedge detection)
- The paper node (WatchdogSec=120) runs an in-process sd_notify actor
  (`paper/sdwatchdog.py`) that pings `WATCHDOG=1` every 30s **on the NT engine's
  clock timer** — so a wedged/deadlocked event loop stops pinging and systemd
  restarts it. Works with `Type=simple` (no READY handshake), no-op outside systemd.
  Complements the monitor's data-staleness checks (which catch "alive but no data").
  (The retired L2 recorder also had a WatchdogSec=90 actor; not applicable now that
  L2 features come from the orderbook-md-adapter timer.)

## Known single point of failure (residual — needs hardware)
- **The whole stack + Postgres still live on one WSL2 host.** Mitigated: data-loss is
  bounded to ≤6h by the backup cadence + the off-host `/mnt/c` copy, and a full host
  rebuild is a guided ~30-min procedure (`ops/bootstrap_new_host.sh` + `install.sh` +
  `restore_drill.sh`). **Not eliminable in software** — true HA needs Postgres + a node
  on a second machine (a provisioning decision). `platform-postgres` is shared with
  helios, so WAL archiving / replication must be coordinated at that container, not here.
