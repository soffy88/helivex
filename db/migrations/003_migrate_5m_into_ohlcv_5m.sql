-- migration 003: route 5-minute bars into market_data.ohlcv_5m (where they belong)
--
-- Bug fixed: 5m OKX-swap bars were written into market_data.ohlcv_1h under
-- source='okx_swap_5m' (a schema/semantic break). The dedicated ohlcv_5m
-- hypertable existed but was EMPTY, so `SELECT ... FROM market_data.ohlcv_5m`
-- silently returned nothing.
--
-- ohlcv_1h and ohlcv_5m have IDENTICAL columns + PK (instrument, bar_close_ts,
-- source), so the copy is a verbatim INSERT...SELECT. Idempotent
-- (ON CONFLICT DO NOTHING) — safe to re-run.
--
-- NOTE: the okx_swap_5m rows are NOT deleted from ohlcv_1h here. Several
-- research/gate scripts (ops/scripts/*_gate*.py, helivex/scripts/run_features.py)
-- still read ohlcv_1h WHERE source='okx_swap_5m'. They keep working off the copy
-- that remains in ohlcv_1h. Going forward, ops/scripts/backfill_5m.py writes ONLY
-- to ohlcv_5m. Follow-up: migrate those consumers to ohlcv_5m, then DELETE the
-- okx_swap_5m rows from ohlcv_1h.

INSERT INTO market_data.ohlcv_5m
    (instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume, created_at)
SELECT
    instrument, bar_close_ts, source, open, high, low, close, volume, quote_volume, created_at
FROM market_data.ohlcv_1h
WHERE source = 'okx_swap_5m'
ON CONFLICT (instrument, bar_close_ts, source) DO NOTHING;
