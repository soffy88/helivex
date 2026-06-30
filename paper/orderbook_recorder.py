"""paper.orderbook_recorder — durable L2 microstructure capture from OKX.

WHY THIS EXISTS (see docs/R15 + R16):
  helixa collects L2 but only to Redis (10s TTL) — no history. Its CCXT/REST path
  can't even reach an exchange from here (Binance 451, OKX REST 403). The ONLY OKX
  path that works in this environment is NautilusTrader's WS client via the Clash
  proxy — exactly what the paper node uses. So we capture L2 here, in a standalone
  data-only NT node, and persist COMPACT microstructure features (not raw books) to
  bound volume — the very concern that made helixa skip persistence ("数据量太大").

What it stores, per snapshot (throttled, default every 10s × BTC/ETH/SOL):
  best_bid/ask, mid, microprice (size-weighted), spread, spread_bps,
  L1 sizes, top-5 depth each side, L1 & L5 order-book imbalance.
~18 rows/min total → trivial for postgres; months of history accumulate cheaply.

This is the one genuinely-new, never-tested data class for helivex. It is a FORWARD
INVESTMENT: no backtest exists until history accrues. Run it, let it build, probe later.
"""
from __future__ import annotations

import asyncio
import os

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId

from paper.db import DB_DSN
from paper.db_pool import ResilientPool

OBF_DDL = """
CREATE SCHEMA IF NOT EXISTS market_data;
CREATE TABLE IF NOT EXISTS market_data.orderbook_features (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    book_ts_ns  BIGINT,
    instrument  TEXT NOT NULL,
    best_bid    DOUBLE PRECISION,
    best_ask    DOUBLE PRECISION,
    mid         DOUBLE PRECISION,
    microprice  DOUBLE PRECISION,
    spread      DOUBLE PRECISION,
    spread_bps  DOUBLE PRECISION,
    bid_sz1     DOUBLE PRECISION,
    ask_sz1     DOUBLE PRECISION,
    bid_depth5  DOUBLE PRECISION,
    ask_depth5  DOUBLE PRECISION,
    imbalance1  DOUBLE PRECISION,
    imbalance5  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_obf_instr_ts
    ON market_data.orderbook_features (instrument, ts DESC);
"""


def _f(x) -> float | None:
    """Coerce a NT Price/Quantity (or None) to float."""
    if x is None:
        return None
    if hasattr(x, "as_double"):
        return float(x.as_double())
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(str(x))


class OrderBookRecorderConfig(ActorConfig, frozen=True):
    instrument_ids:    tuple[str, ...]
    book_type:         str   = "L2_MBP"
    depth:             int   = 0       # 0 = public books channel (400 lvls, no VIP); 50/400 need VIP login
    interval_ms:       int   = 1000    # book snapshot cadence from the engine
    persist_interval_s: float = 10.0   # throttle DB writes (volume control)


class OrderBookRecorder(Actor):
    """Subscribes to L2 order books, persists compact microstructure features."""

    def __init__(self, config: OrderBookRecorderConfig) -> None:
        super().__init__(config)
        self._ids = [InstrumentId.from_str(s) for s in config.instrument_ids]
        self._db: ResilientPool | None = None
        self._loop = None
        self._last_persist_ns: dict[str, int] = {}
        self._n_persisted = 0

    async def _init_db(self) -> None:
        # Resilient pool: retries the boot race + self-heals on container restart
        # (the prior one-shot create_pool silently died on a postgres bounce).
        self._db = ResilientPool(DB_DSN, OBF_DDL, name="l2rec", logger=self.log)
        await self._db.ensure()
        # the interval-snapshot callback fires on a Rust timer thread (no asyncio
        # loop there); capture the main loop now to schedule DB writes thread-safely.
        self._loop = asyncio.get_running_loop()
        self.log.info("[l2rec] DB pool ready, schema ensured")

    def on_start(self) -> None:
        bt = BookType.L2_MBP if self.config.book_type == "L2_MBP" else BookType.L1_MBP
        for iid in self._ids:
            self.subscribe_order_book_at_interval(
                iid, book_type=bt, depth=self.config.depth,
                interval_ms=self.config.interval_ms,
            )
            self.log.info(f"[l2rec] subscribed L2 {iid} depth={self.config.depth}")
        asyncio.ensure_future(self._init_db())

    def on_order_book(self, book: OrderBook) -> None:
        instr = str(book.instrument_id)
        now = self.clock.timestamp_ns()
        last = self._last_persist_ns.get(instr, 0)
        if now - last < self.config.persist_interval_s * 1e9:
            return
        if self._db is None or self._loop is None:
            return

        feat = self._features(book)
        if feat is None:
            return
        self._last_persist_ns[instr] = now

        async def _store() -> None:
            try:
                await self._db.execute(lambda conn: conn.execute(
                    """INSERT INTO market_data.orderbook_features
                       (book_ts_ns, instrument, best_bid, best_ask, mid, microprice,
                        spread, spread_bps, bid_sz1, ask_sz1, bid_depth5, ask_depth5,
                        imbalance1, imbalance5)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
                    book.ts_last, instr, feat["best_bid"], feat["best_ask"],
                    feat["mid"], feat["microprice"], feat["spread"], feat["spread_bps"],
                    feat["bid_sz1"], feat["ask_sz1"], feat["bid_depth5"],
                    feat["ask_depth5"], feat["imbalance1"], feat["imbalance5"],
                ))
                self._n_persisted += 1
                if self._n_persisted <= 3 or self._n_persisted % 100 == 0:
                    self.log.info(f"[l2rec] persisted n={self._n_persisted} "
                                  f"last={instr} mid={feat['mid']} imb1={feat['imbalance1']:.3f}")
            except Exception as e:
                self.log.error(f"[l2rec] persist error: {e}")

        asyncio.run_coroutine_threadsafe(_store(), self._loop)

    def _features(self, book: OrderBook) -> dict | None:
        bb = _f(book.best_bid_price()); ba = _f(book.best_ask_price())
        bbs = _f(book.best_bid_size());  bas = _f(book.best_ask_size())
        if not (bb and ba and bbs and bas) or bb <= 0 or ba <= 0:
            return None
        mid = (bb + ba) / 2.0
        spread = ba - bb
        micro = (bb * bas + ba * bbs) / (bbs + bas) if (bbs + bas) > 0 else mid
        bid5 = sum(_f(l.size()) or 0.0 for l in book.bids()[:5])
        ask5 = sum(_f(l.size()) or 0.0 for l in book.asks()[:5])
        return {
            "best_bid": bb, "best_ask": ba, "mid": mid, "microprice": micro,
            "spread": spread, "spread_bps": spread / mid * 1e4 if mid else None,
            "bid_sz1": bbs, "ask_sz1": bas, "bid_depth5": bid5, "ask_depth5": ask5,
            "imbalance1": (bbs - bas) / (bbs + bas) if (bbs + bas) > 0 else 0.0,
            "imbalance5": (bid5 - ask5) / (bid5 + ask5) if (bid5 + ask5) > 0 else 0.0,
        }

    def on_stop(self) -> None:
        for iid in self._ids:
            try:
                self.unsubscribe_order_book_at_interval(iid, interval_ms=self.config.interval_ms)
            except Exception:
                pass
        self.log.info(f"[l2rec] stopped — total persisted {self._n_persisted}")


# ── data-only node (OKX Demo WS via proxy, no exec client) ──────────────────────

def build_recorder_node():
    from nautilus_trader.adapters.okx.config import (
        OKXDataClientConfig, OKXEnvironment, OKXInstrumentType,
    )
    from nautilus_trader.adapters.okx.factories import OKXLiveDataClientFactory
    from nautilus_trader.config import InstrumentProviderConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode

    api_key    = os.environ["OKX_API_KEY"]
    api_secret = os.environ["OKX_API_SECRET"]
    passphrase = os.environ["OKX_PASSPHRASE"]
    proxy_url  = os.environ.get("OKX_WS_PROXY") or None

    okx_data = OKXDataClientConfig(
        api_key=api_key, api_secret=api_secret, api_passphrase=passphrase,
        environment=OKXEnvironment.DEMO,
        instrument_types=(OKXInstrumentType.SWAP,),
        proxy_url=proxy_url,
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )

    node_config = TradingNodeConfig(
        trader_id="HELIVEX-L2REC-001",
        data_clients={"OKX": okx_data},
        # no exec_clients — this node never trades
    )
    node = TradingNode(config=node_config)
    node.add_data_client_factory("OKX", OKXLiveDataClientFactory)

    recorder = OrderBookRecorder(OrderBookRecorderConfig(
        instrument_ids=(
            "BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX", "SOL-USDT-SWAP.OKX",
        ),
        depth=0, interval_ms=1000, persist_interval_s=10.0,
    ))
    node.trader.add_actor(recorder)
    return node
