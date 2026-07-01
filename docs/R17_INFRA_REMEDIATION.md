# R17 — Infra Audit Remediation (2026-06-30 → 07-01)

Full remediation following a 6-subsystem audit of the helivex paper-trading stack.
**19 audit items (P0–P2) + 3 follow-up batches (A/B/C) + ledger closure**, all
implemented and verified live. Merged via **PR #1** (engineering) and **PR #2**
(ledger). Tests 33 → 56; services 6/6 + timers 6/6 healthy; gate ledger 13 → 16.

---

## 0. The audit

Six parallel deep-dives graded each subsystem against best-in-class and produced a
prioritized backlog. Scores at audit time:

| Subsystem | Score | Headline finding |
|---|:--:|---|
| Strategy research | 7/10 | Honest discipline (13/13 FAIL) but "DSR/PBO/CPCV" labels overstated the math |
| Web dashboard | 7/10 | ~100% live data, disciplined states; polling-only, thin auth |
| Gateway | 5/10 | Working audit chain, but **zero auth on a 0.0.0.0-bound port** |
| Paper node | 5/10 | Real fills + signing, but **persistence silently dead**; risk notional ≠ actual |
| Data infra | 4/10 | TimescaleDB bones, but **ingestion 15d stale, L2 recorder silently stalled** |
| Ops / reliability | 4/10 | Auto-restart + alerts, but single-host SPOF, plaintext secrets, local backups |

### Live incidents caught during the audit (both root-caused)
Two subsystems were **silently dropping every DB write** — same root cause: a
one-shot asyncpg pool created once at boot with no reconnect, guarded by
`if self._db_pool:`, so a transient Postgres race left the pool `None` forever
while trading/recording continued blind.
- **Paper node**: `paper.signals`/`fills` frozen ~11h.
- **L2 recorder**: `orderbook_features` frozen ~8h.

---

## 1. Root-cause fix — resilient DB pool `[240e022]`

`paper/db_pool.py::ResilientPool` — retries on the boot race, self-heals on a
mid-run connection loss, and **fails loud** (never silently skips). Adopted by all
4 strategies, the L2 recorder, and (non-fatally) the gateway startup. Verified: both
frozen feeds recovered within seconds of restart; all 9 strategy pools reconnected.

---

## 2. The 19 audit items

### P0 — stop the bleeding / security / integrity
| # | Item | Commit | Evidence |
|---|---|---|---|
| 1 | Resilient DB pool + reconnect | `240e022` | both incidents recovered; unit tests |
| 2 | Write-freshness breaker (+ backup, heartbeat) | `8cf52d1` | fired CRITICAL on the 705-min gap, cleared on recovery |
| 3 | Gateway token-auth + bind `127.0.0.1` + CORS | `8a55bfd`,`52a022e` | no-token→401, token→200; bound localhost |
| 4 | Validate `limit` + global exception handler | `8a55bfd` | `?limit=-5` → 422 (was 500 leak) |
| 5 | `.env` → `0600`; `DASH_PASS` rotated out of unit | `14e5ec7` | perms verified; public login re-tested |
| 6 | Durable backups (timer + integrity + off-host + restore drill) | `14e5ec7` | **restore drill passed** (row counts match) |
| 7 | Paper log rotation + tick_probe quieted | `240e022`,`14e5ec7` | 552MB → rotated; tick spam ~thousands → ≤3/inst |

### P1 — correctness / reliability
| # | Item | Commit | Evidence |
|---|---|---|---|
| 8 | Risk exposure reconciled to actual fill notional | `240e022` | registers price×qty on fill |
| 9 | Perp funding carry in backtests | `1b9739e` | validated; all Sharpes drop |
| 10 | Honest metric labels + real Deflated Sharpe | `1b9739e` | relabeled; real DSR added non-gating |
| 11 | Ingestion timers + `ohlcv_5m` fix | `14e5ec7`,`358a49a` | data 15d-stale → hourly; ohlcv_5m 0 → 1.94M |
| 12 | L2 auto-restart on stall (+ watchdog note) | `8cf52d1` | monitor restarts recorder on stale |
| 13 | Web restart-confirm + 2s risk poll + visibility-pause | `52a022e` | build green, verified |
| 14 | All 6 units in git + `install.sh` + `/gate/run` offload | `14e5ec7`,`8a55bfd` | 11 units tracked |
| 15 | Position rehydrate on restart + HWM drawdown | `240e022`,`8cf52d1` | **recovered 5 real venue positions** on first run |

### P2 — completeness / observability / hygiene
| # | Item | Commit | Evidence |
|---|---|---|---|
| 16 | `/metrics` + structured logging; real `/trades` & `/stats` | `8a55bfd` | 381 real round-trips (was `[]`); P&L self-consistent |
| 17 | OHLCV CHECK constraints + compression + gap/reconcile | `1b9739e` | constraint rejects bad row; 3 hypertables compressed |
| 18 | Dead-man's-switch heartbeat + DR runbook | `8cf52d1`,`14e5ec7` | `HELIVEX_DEADMAN_URL` hook + `ops/RUNBOOK.md` |
| 19 | Web a11y (chart aria-labels) + viewport | `52a022e` | build green |

---

## 3. Follow-up batches

### Batch A — data cleanup `[358a49a, 3c32133]`
- **4H series** was frozen at 2026-06-15 (history-only backfill); added forward-refresh
  + `helivex-ingest-ohlcv-4h` timer → current.
- **5m/1h split**: repointed all 6 consumers to `ohlcv_5m`, then dropped **1,931,565**
  duplicate `okx_swap_5m` rows from `ohlcv_1h` (migration 005; DML-on-compressed).
  Superset safety proven per-instrument before deletion.
- Fixed `backfill_ohlcv.py` "0 rows" log bug; corrected the `funding_rates` "Coinalyze"
  → "okx" label.

### Batch B — funding + ledger re-run `[0c03ffb]`
- Fetched full **ETH/SOL** binance funding (BTC 7457 / ETH 7223 / SOL 6424); ingest
  timer now covers all 3 perps.
- Re-ran SWAP gates funding-off vs on (`ops/scripts/funding_rerun_compare.py`, non-mutating):
  funding lowers every perp Sharpe (net-long-biased pay funding), **verdicts unchanged**.
- **Caught & fixed an A2 regression**: `strategy_gate._fetch_ohlcv` still read `ohlcv_1h`
  for `okx_swap_5m` configs (the `--include=*.py` grep missed the YAML `db_source`) —
  broke the vwap/spot gates + gateway `/backtest`. Now routes 5m → `ohlcv_5m`.

### Batch C — the four big items `[29d1e87, 69b4281, 4b5fd58, 09d2f54]`
- **Unrealized MTM** (`29d1e87`): `nav_and_drawdown` marks open positions (from
  `paper.fills`) to fresh L2 mids — the drawdown breaker now sees open-position losses.
- **WatchdogSec** (`69b4281`): `paper/sdwatchdog.py` NT actor pings `WATCHDOG=1` on the
  engine clock (paper 120s / l2rec 90s, `Type=simple`, no-op outside systemd); a wedged
  loop stops pinging → systemd restarts. Canary-verified on L2 first, then paper.
- **SPOF** (`4b5fd58`): backups daily → **every 6h** (≤6h data-loss window);
  `ops/bootstrap_new_host.sh` guided rebuild. Residual compute-SPOF needs a 2nd machine
  (documented; `platform-postgres` is shared → no WAL archiving here).
- **Real CSCV-PBO + purged CV** (`09d2f54`): genuine López de Prado CSCV (S=8, C(8,4)=70
  splits, IS-best OOS-rank logit) + label purging. Single-config gate → block-bootstrap
  surrogate candidates (honestly documented limitation). Gates add-only (no false PASS).

---

## 4. Ledger closure — trials #14-16 `[PR #2]`

Re-ran the 3 SWAP configs through the fully upgraded gate (funding + real CSCV-PBO +
purged CV), appended as trials #14-16 (DSR bar now 1.74–1.80):

| # | config | verdict | CSCV-PBO (per instrument) |
|---|---|---|---|
| 14 | trend_dual | FAIL | 0.09 / 0.14 / 0.49 |
| 15 | vwap_mr_1h | FAIL | 0.61 / 0.84 |
| 16 | spot_trend_1d | FAIL | **0.80** / 0.57 / 0.43 |

**16/16 FAIL.** The real CSCV-PBO earned its keep: `spot_trend` BTC has gross Sharpe
**1.20** but CSCV-PBO **0.80** — an 80% probability of backtest overfitting the old
heuristic-only gate under-penalized.

---

## 5. Tests

33 → 56. New: `test_round_trips` (FIFO P&L: long/short/partial/flip-through-zero),
`test_gateway_auth`, `test_db_pool` (failure paths + real-DB self-heal),
`test_evaluators` (backup/write freshness), `test_strategy_gate_pbo` (CSCV bounds,
determinism, purge, luck-discrimination).

---

## 6. Residuals (documented, need a decision or time)

- **Compute SPOF** — needs a second machine (hardware/provisioning decision). Mitigated
  to ≤6h data-loss + guided rebuild.
- **No validated alpha** — 16/16 FAIL. All obvious levers (directional/regime/vol/MR/trend)
  exhausted. The one forward-investment (L2 microstructure) has only ~3 days of history;
  revisit that lever in ~1 month.
- The gate's CSCV-PBO uses block-bootstrap surrogates (no real parameter grid to rank);
  a true model-selection PBO would need a strategy grid.

**The engineering backlog is empty.** The platform is now a reliable, secure,
observable, research-honest harness — waiting on an actual edge.
