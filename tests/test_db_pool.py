"""Unit tests for the self-healing asyncpg wrapper (paper.db_pool.ResilientPool).
The failure paths need no DB (point at a dead port); the happy path uses the real
DB and skips if it's unavailable. Backoff sleeps are patched out for speed."""
from __future__ import annotations

import asyncio

import pytest

from paper import db_pool
from paper.db import DB_DSN

DEAD_DSN = "postgresql://x:x@127.0.0.1:1/none"


async def _no_sleep(*_a, **_k):
    return None


def test_ensure_fails_gracefully_on_bad_dsn(monkeypatch) -> None:
    monkeypatch.setattr(db_pool.asyncio, "sleep", _no_sleep)
    rp = db_pool.ResilientPool(DEAD_DSN, "SELECT 1;", name="t")
    assert asyncio.run(rp.ensure()) is False  # returns False, never raises
    assert rp.last_error is not None


def test_execute_raises_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(db_pool.asyncio, "sleep", _no_sleep)
    rp = db_pool.ResilientPool(DEAD_DSN, "SELECT 1;", name="t")
    with pytest.raises(RuntimeError):
        asyncio.run(rp.execute(lambda c: c.fetchval("SELECT 1")))


def test_happy_path_and_self_heal() -> None:
    async def run():
        rp = db_pool.ResilientPool(DB_DSN, "SELECT 1;", name="selftest")
        try:
            if not await rp.ensure():
                pytest.skip("DB unavailable")
            assert await rp.execute(lambda c: c.fetchval("SELECT 42")) == 42
            await rp._discard()                       # simulate a dropped pool
            assert rp._pool is None
            assert await rp.execute(lambda c: c.fetchval("SELECT 7")) == 7  # self-heals
        finally:
            await rp.close()
    asyncio.run(run())
