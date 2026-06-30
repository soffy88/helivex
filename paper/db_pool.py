"""paper.db_pool — a self-healing asyncpg pool wrapper.

ROOT-CAUSE FIX for the silent-persistence-death bug (see audit 2026-06-30).
The old per-strategy/per-recorder `_init_db()` created the pool exactly ONCE in
`on_start` with no retry. A transient Postgres race — `CannotConnectNowError`
("the database system is starting up") on boot, or a `platform-postgres`
container restart mid-run (ConnectionReset/Refused) — left `_db_pool = None`
forever. Every write was guarded by `if self._db_pool:`, so signals / fills /
orderbook rows were then dropped *silently* for the whole process lifetime while
trading (and L2 capture) carried on blind.

`ResilientPool` removes that failure mode:
  - `ensure()` builds the pool + runs idempotent DDL with bounded retry/backoff,
    so the boot race no longer kills persistence permanently.
  - `execute()` re-establishes the pool lazily if a write hits a connection-class
    error, so a container restart self-heals on the next write.
  - persistent failure RAISES (never silently skips) so callers log loudly.

This is process-local; cross-process "writes frozen while node alive" detection
lives in paper.evaluators (eval_write_freshness), which reads the DB directly.
"""
from __future__ import annotations

import asyncio

import asyncpg

# Connection-class failures worth rebuilding the pool for (vs. a SQL bug, which
# should surface, not loop). CannotConnectNowError happens at connect time and is
# handled inside ensure()'s broad except; these are the mid-write drops.
_CONN_ERRORS = (OSError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError)


class ResilientPool:
    """A lazily-built, self-healing wrapper around an asyncpg pool."""

    def __init__(
        self,
        dsn: str,
        ddl: str,
        name: str,
        logger=None,
        min_size: int = 1,
        max_size: int = 2,
    ) -> None:
        self._dsn = dsn
        self._ddl = ddl
        self._name = name
        self._log = logger
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None
        self._lock: asyncio.Lock | None = None
        self.last_error: str | None = None

    def _get_lock(self) -> asyncio.Lock:
        # Created lazily so it binds to the running NT event loop, not import time.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def ensure(self) -> bool:
        """(Re)create the pool and run DDL, with bounded retry. True if ready."""
        if self._pool is not None:
            return True
        async with self._get_lock():
            if self._pool is not None:  # built by another waiter
                return True
            delay = 1.0
            for attempt in range(1, 4):
                try:
                    pool = await asyncpg.create_pool(
                        self._dsn, min_size=self._min, max_size=self._max
                    )
                    async with pool.acquire() as conn:
                        await conn.execute(self._ddl)
                    self._pool = pool
                    self.last_error = None
                    if self._log:
                        self._log.info(f"[db_pool:{self._name}] connected (attempt {attempt})")
                    return True
                except Exception as exc:  # CannotConnectNow / OSError / etc.
                    self.last_error = str(exc)
                    if self._log:
                        self._log.warning(
                            f"[db_pool:{self._name}] connect attempt {attempt} failed: {exc}"
                        )
                    await asyncio.sleep(delay)
                    delay *= 2
            if self._log:
                self._log.error(
                    f"[db_pool:{self._name}] connect failed after retries; "
                    f"will retry lazily on next write"
                )
            return False

    async def execute(self, fn):
        """Run `await fn(conn)` with a healthy connection; self-heal once on a
        connection-class error. Returns fn's result; raises on persistent failure
        so the caller can log loudly (never silently drops)."""
        for attempt in (1, 2):
            if not await self.ensure():
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                raise RuntimeError(f"db_pool:{self._name} unavailable (last_error={self.last_error})")
            try:
                async with self._pool.acquire() as conn:
                    return await fn(conn)
            except _CONN_ERRORS as exc:
                self.last_error = str(exc)
                await self._discard()
                if self._log:
                    self._log.warning(
                        f"[db_pool:{self._name}] write conn error, rebuilding pool: {exc}"
                    )
                # loop retries with a freshly-built pool
        raise RuntimeError(f"db_pool:{self._name} write failed (last_error={self.last_error})")

    async def _discard(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:
                pass

    async def close(self) -> None:
        await self._discard()
