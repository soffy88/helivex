-- 004: OHLCV integrity CHECK constraints + TimescaleDB compression policies.
-- Verified 0 existing violations at write time (2026-06-30), so constraints are
-- added as VALIDATED. Idempotent via duplicate_object guards.

DO $$ BEGIN
  ALTER TABLE market_data.ohlcv_1h ADD CONSTRAINT ohlcv_1h_ok CHECK (
    high >= low AND low >= 0 AND high >= open AND high >= close
    AND low <= open AND low <= close AND volume >= 0
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE market_data.ohlcv_5m ADD CONSTRAINT ohlcv_5m_ok CHECK (
    high >= low AND low >= 0 AND high >= open AND high >= close
    AND low <= open AND low <= close AND volume >= 0
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Compression: OHLCV is append-mostly and only grows (ohlcv_1h ~2M rows). Compress
-- chunks older than 30 days, segmented by series so per-instrument scans stay fast.
ALTER TABLE market_data.ohlcv_1h SET (timescaledb.compress, timescaledb.compress_segmentby = 'instrument, source');
ALTER TABLE market_data.ohlcv_5m SET (timescaledb.compress, timescaledb.compress_segmentby = 'instrument, source');
ALTER TABLE market_data.funding_rates SET (timescaledb.compress);

SELECT add_compression_policy('market_data.ohlcv_1h',      INTERVAL '30 days', if_not_exists => true);
SELECT add_compression_policy('market_data.ohlcv_5m',      INTERVAL '30 days', if_not_exists => true);
SELECT add_compression_policy('market_data.funding_rates', INTERVAL '30 days', if_not_exists => true);
