-- 005: Drop the duplicate 5-minute bars from ohlcv_1h.
--
-- 5m data was historically mis-stored in market_data.ohlcv_1h under
-- source='okx_swap_5m'. Migration 003 copied all of it into the proper
-- market_data.ohlcv_5m hypertable, and all 6 reader scripts (gates +
-- run_features) were repointed to ohlcv_5m. ohlcv_5m is a verified superset
-- (per-instrument counts >= the ohlcv_1h copy), so removing the orphaned rows
-- from ohlcv_1h loses nothing. Today's backup + a passing restore drill cover it.
--
-- DML on compressed chunks works on TimescaleDB 2.26 (auto-decompresses only the
-- affected source segments, since compress_segmentby includes source).

DELETE FROM market_data.ohlcv_1h WHERE source = 'okx_swap_5m';
