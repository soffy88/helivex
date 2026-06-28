# R16 — L2 Order-Book Recorder (microstructure capture)

> Modules: `paper/orderbook_recorder.py` (Actor + data-only node), `paper/run_recorder.py`
> (runner), `ops/systemd/helivex-l2recorder.service`. Monitor: `eval_l2_recorder_flow`.
> Table: `market_data.orderbook_features`. **Status: LIVE under systemd, accumulating.**

## Why (and why this shape)

R15 found L2 is the one genuinely-new, never-tested data class for helivex, but it has
**zero history anywhere** — helixa collects Binance top-10 to Redis (10s TTL) and never
persists ("数据量太大"), and its CCXT/REST path can't reach an exchange from here
(Binance 451, OKX REST 403 even via proxy). The **only** OKX path that works in this
environment is NautilusTrader's WS client through the Clash proxy — exactly what the
paper node uses. So we capture L2 in helivex via a standalone NT node.

This is a **forward investment**: no backtest exists until history accrues. Run it now,
let it build, probe later (mirrors the discipline of every other direction here).

## Design

- **Standalone data-only NT node** (`HELIVEX-L2REC-001`): OKX Demo data client + proxy,
  **no exec client** — it never trades, fully isolated from the live paper node.
- **OrderBookRecorder Actor**: `subscribe_order_book_at_interval(L2_MBP, depth=0,
  interval_ms=1000)` for BTC/ETH/SOL SWAP. `depth=0` = OKX public `books` channel (400
  levels, no VIP); depth 50/400 require VIP4+ login and are rejected.
- **Compact features, not raw books** — the volume concern that made helixa skip
  persistence. Per snapshot, throttled to **every 10s** per instrument:
  best_bid/ask, mid, microprice (size-weighted), spread, spread_bps, L1 sizes,
  top-5 depth each side, L1 & L5 order-book imbalance → `market_data.orderbook_features`.
  ~18 rows/min total; months of history cost almost nothing.

### Two bugs found and fixed during bring-up (NT v1.228 specifics)
1. `BookLevel.size` / `.price` are **methods**, not attributes — must call `l.size()`.
2. The interval-snapshot callback fires on a **Rust timer thread** with no asyncio loop,
   so `asyncio.ensure_future` raised "no current event loop". Fixed by capturing the
   main loop in `_init_db` and scheduling DB writes with `run_coroutine_threadsafe`.

## Operations

```
systemctl --user {status|restart|stop} helivex-l2recorder
tail -f ~/helivex_l2recorder.log
```
- `Restart=always` (pure collector — always up). Unit also in `ops/systemd/` for repro.
- `eval_l2_recorder_flow` (monitor, every 120s): alerts if no new row in 5 min — catches
  a **silent WS stall** (process up, data stopped) that `Restart=always` can't see.

## Verified

Live: connected `wss://wspap.okx.com:8443` via proxy, subscribed depth=0 (400-level
books), persisting every 10s. Sample features sane (BTC spread ~0.02bps, SOL ~7bps,
imbalances in [-1,1], microprice between bid/ask). Rows growing ~18/min under systemd.

## Next (when history accrues)
Probe order-book imbalance / microprice as short-horizon predictors (microstructure
alpha is the thesis). Same probe-before-gate discipline: build an IC/persistence probe
once there are weeks of data, escalate to a CPCV/DSR/PBO gate only if it earns it.
