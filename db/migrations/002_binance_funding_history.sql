-- migration 002: Binance single-exchange historical funding (REST backfill)
--
-- Real 8h funding settlements from Binance USDⓈ-M perp (/fapi/v1/fundingRate),
-- pulled via the SG egress (helios-proxy / sing-box) to bypass CN 451.
--
-- Kept SEPARATE from market_data.funding_rates (the Coinalyze cross-exchange
-- aggregate, ~3-month history). This table is explicitly SINGLE-EXCHANGE
-- (source='binance_rest') so backtest metadata can distinguish Binance-only
-- funding from cross-exchange aggregates ("不漏" — provenance is explicit).

CREATE TABLE IF NOT EXISTS market_data.binance_funding_history (
    symbol        TEXT             NOT NULL,
    funding_time  TIMESTAMPTZ      NOT NULL,   -- settlement instant (0/8/16 UTC)
    funding_rate  DOUBLE PRECISION NOT NULL,   -- realized 8h rate, decimal
    source        TEXT             NOT NULL DEFAULT 'binance_rest',
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT binance_funding_history_pk PRIMARY KEY (symbol, funding_time)
);

CREATE INDEX IF NOT EXISTS binance_funding_history_symbol_time
    ON market_data.binance_funding_history (symbol, funding_time DESC);
