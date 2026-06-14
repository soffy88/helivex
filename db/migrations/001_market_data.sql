-- migration 001: market_data schema + TimescaleDB hypertables
-- idempotent — safe to re-run

CREATE SCHEMA IF NOT EXISTS market_data;

-- OHLCV 1-hour bars
CREATE TABLE IF NOT EXISTS market_data.ohlcv_1h (
    instrument  TEXT        NOT NULL,
    bar_close_ts TIMESTAMPTZ NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'okx',
    open        NUMERIC     NOT NULL,
    high        NUMERIC     NOT NULL,
    low         NUMERIC     NOT NULL,
    close       NUMERIC     NOT NULL,
    volume      NUMERIC     NOT NULL,
    quote_volume NUMERIC,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ohlcv_1h_pk PRIMARY KEY (instrument, bar_close_ts, source)
);

-- OHLCV 5-minute bars
CREATE TABLE IF NOT EXISTS market_data.ohlcv_5m (
    instrument  TEXT        NOT NULL,
    bar_close_ts TIMESTAMPTZ NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'okx',
    open        NUMERIC     NOT NULL,
    high        NUMERIC     NOT NULL,
    low         NUMERIC     NOT NULL,
    close       NUMERIC     NOT NULL,
    volume      NUMERIC     NOT NULL,
    quote_volume NUMERIC,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ohlcv_5m_pk PRIMARY KEY (instrument, bar_close_ts, source)
);

-- Perpetual funding rates
CREATE TABLE IF NOT EXISTS market_data.funding_rates (
    instrument      TEXT        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    source          TEXT        NOT NULL DEFAULT 'okx',
    funding_rate    NUMERIC     NOT NULL,
    realized_rate   NUMERIC,
    next_funding_time TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT funding_rates_pk PRIMARY KEY (instrument, ts, source)
);

-- TimescaleDB hypertables (idempotent via IF NOT EXISTS in newer TS, use exception block)
DO $$
BEGIN
    PERFORM create_hypertable(
        'market_data.ohlcv_1h', 'bar_close_ts',
        chunk_time_interval => INTERVAL '1 month',
        if_not_exists => TRUE
    );
EXCEPTION WHEN others THEN
    RAISE NOTICE 'ohlcv_1h hypertable already exists or error: %', SQLERRM;
END$$;

DO $$
BEGIN
    PERFORM create_hypertable(
        'market_data.ohlcv_5m', 'bar_close_ts',
        chunk_time_interval => INTERVAL '1 week',
        if_not_exists => TRUE
    );
EXCEPTION WHEN others THEN
    RAISE NOTICE 'ohlcv_5m hypertable already exists or error: %', SQLERRM;
END$$;

DO $$
BEGIN
    PERFORM create_hypertable(
        'market_data.funding_rates', 'ts',
        chunk_time_interval => INTERVAL '1 month',
        if_not_exists => TRUE
    );
EXCEPTION WHEN others THEN
    RAISE NOTICE 'funding_rates hypertable already exists or error: %', SQLERRM;
END$$;

-- Covering index for fast symbol+time range scans
CREATE INDEX IF NOT EXISTS ohlcv_1h_instrument_ts
    ON market_data.ohlcv_1h (instrument, bar_close_ts DESC);

CREATE INDEX IF NOT EXISTS ohlcv_5m_instrument_ts
    ON market_data.ohlcv_5m (instrument, bar_close_ts DESC);

CREATE INDEX IF NOT EXISTS funding_rates_instrument_ts
    ON market_data.funding_rates (instrument, ts DESC);
