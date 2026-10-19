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

──────────────── CONNECTION-SCALING FIX (2026-10-19) ────────────────
Symptom: platform-postgres (max_connections=100) neared exhaustion; this one
container (helivex-paper, 172.18.0.22) held ~20 idle connections.

Root cause: `paper/node.py` registers ~20 strategy instances and each strategy's
`on_start` created its OWN ResilientPool against the SAME DSN. Each pool kept a
process-local asyncpg.Pool with min_size=1, so ~20 backend connections were
pinned idle for the life of the node (each with max_size=2 headroom, so a busy
moment could push this one container past 40 connections).

Fix: all ResilientPool instances that share a DSN now share ONE asyncpg.Pool,
held in a process-wide registry keyed by DSN and reference-counted. The shared
pool is created under a per-DSN lock (so concurrent strategy boots can't race two
physical pools into existence) and configured with
  - min_size=0            : no connection is pinned while idle (idle conns drain)
  - max_size=<env>        : a single hard ceiling for this container's backend
                            connections toward platform-postgres; default 4
                            (was effectively ~20 pinned / ~40 peak).
  - max_inactive_connection_lifetime=<env>, default 60s: an idle connection is
                            closed by asyncpg once idle longer than this, so a
                            quiet period returns the container toward ~0 conns.

Each ResilientPool still owns its DDL + self-heal + raise-on-failure contract;
only the *physical* connection is shared. When the last reference for a DSN
releases, the shared pool is closed+forgotten so a node restart won't leak.

Overridable via env (read at pool-build time):
  HELIVEX_POOL_MAX_SIZE         default 4
  HELIVEX_POOL_MIN_SIZE         default 0
  HELIVEX_POOL_MAX_INACTIVE_S   default 60
"""
from __future__ import annotations

import asyncio
import os

import asyncpg

# Connection-class failures worth rebuilding the pool for (vs. a SQL bug, which
# should surface, not loop). CannotConnectNowError happens at connect time and
# is handled inside ensure()'s broad except; these are the mid-write drops.
_CONN_ERRORS = (OSError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Process-wide registry of physical asyncpg pools, keyed by DSN. Shared by every
# ResilientPool pointing at the same DSN so N strategies != N connections.
# entry = {"pool": Pool|None, "refs": int, "lock": asyncio.Lock}
_SHARED: dict[str, dict] = {}


def _entry(dsn: str) -> dict:
    e = _SHARED.get(dsn)
    if e is None:
        e = {"pool": None, "refs": 0, "lock": asyncio.Lock()}
        _SHARED[dsn] = e
    return e


class ResilientPool:
    """A lazily-built, self-healing wrapper around an asyncpg pool.

    Physical connections are now shared across all instances with the same DSN
    (see module docstring); this class owns only the logical state, its DDL, and
    self-heal semantics. Keeping the public surface identical means every
    strategy call site (``ensure`` / ``execute`` / ``close`` / ``last_error``)
    works unchanged — no strategy file needed editing.
    """

    def __init__(
        self,
        dsn: str,
        ddl: str,
        name: str,
        logger=None,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> None:
        self._dsn = dsn
        self._ddl = ddl
        self._name = name
        self._log = logger
        # Per-instance knobs are accepted for backward compatibility, but the
        # *physical* pool is shared process-wide, so the real ceiling comes from
        # env. A caller may still tighten this instance's max (never above env).
        env_max = _env_int("HELIVEX_POOL_MAX_SIZE", 4)
        self._max = env_max if max_size is None else min(max_size, env_max)
        env_min = _env_int("HELIVEX_POOL_MIN_SIZE", 0)
        self._min = env_min if min_size is None else min(min_size, self._max)
        # Held reference to the shared pool (for execute() typing / live checks).
        self._pool: asyncpg.Pool | None = None
        self._hold: bool = False  # does this instance currently count a reference
        self._lock: asyncio.Lock | None = None
        self.last_error: str | None = None

    def _get_lock(self) -> asyncio.Lock:
        # Created lazily so it binds to the running NT event loop, not import time.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _acquire_shared(self) -> asyncpg.Pool | None:
        """Open (or reuse) the process-wide pool for this DSN with bounded retry.

        Serialized by a per-DSN lock so simultaneous strategy boots cannot race
        two physical pools into existence. Reference-counted: many strategies
        reuse one pool. Returns the pool, or None if it could not be established
        after retries (the failure is loud, matching the historical contract).
        """
        entry = _entry(self._dsn)
        # Fast path: someone already holds the physical pool open for this DSN.
        if entry["refs"] > 0 and entry["pool"] is not None:
            entry["refs"] += 1
            self._pool = entry["pool"]
            self._hold = True
            return entry["pool"]

        async with entry["lock"]:
            # Re-check under the lock — another worker may have finished first.
            if entry["refs"] > 0 and entry["pool"] is not None:
                entry["refs"] += 1
                self._pool = entry["pool"]
                self._hold = True
                return entry["pool"]

            delay = 1.0
            for attempt in range(1, 4):
                try:
                    pool = await asyncpg.create_pool(
                        self._dsn,
                        min_size=self._min,
                        max_size=self._max,
                        max_inactive_connection_lifetime=_env_float(
                            "HELIVEX_POOL_MAX_INACTIVE_S", 60.0
                        ),
                    )
                    entry["pool"] = pool
                    entry["refs"] = 1
                    self._pool = pool
                    self._hold = True
                    self.last_error = None
                    if self._log:
                        self._log.info(
                            f"[db_pool:{self._name}] shared pool connected "
                            f"(attempt {attempt}, min={self._min} max={self._max} "
                            f"inactive_s={_env_float('HELIVEX_POOL_MAX_INACTIVE_S', 60.0):.0f})"
                        )
                    return pool
                except Exception as exc:  # CannotConnectNow / OSError / etc.
                    self.last_error = str(exc)
                    if self._log:
                        self._log.warning(
                            f"[db_pool:{self._name}] shared pool connect attempt "
                            f"{attempt} failed: {exc}"
                        )
                    await asyncio.sleep(delay)
                    delay *= 2
            if self._log:
                self._log.error(
                    f"[db_pool:{self._name}] shared pool connect failed after retries; "
                    f"will retry lazily on next write"
                )
            return None

    async def ensure(self) -> bool:
        """(Re)create the (shared) pool and run this instance's DDL. True if ready."""
        if self._hold and self._pool is not None:
            return True
        async with self._get_lock():
            if self._hold and self._pool is not None:
                return True
            pool = await self._acquire_shared()
            if pool is None:
                return False
            try:
                async with pool.acquire() as conn:
                    await conn.execute(self._ddl)
            except _CONN_ERRORS as exc:
                # A DDL-time connection drop means the shared pool went bad;
                # release our reference; the next write rebuilds loudly.
                self.last_error = str(exc)
                await self._release()
                if self._log:
                    self._log.warning(f"[db_pool:{self._name}] DDL conn error: {exc}")
                return False
            return True

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
                # asyncpg auto-replaces a single dropped connection; release our
                # reference and let ensure() re-acquire (rebuild if it was bad).
                await self._release()
                if self._log:
                    self._log.warning(
                        f"[db_pool:{self._name}] write conn error, rebuilding pool: {exc}"
                    )
                # loop retries with a freshly-acquired pool
        raise RuntimeError(f"db_pool:{self._name} write failed (last_error={self.last_error})")

    async def _release(self) -> None:
        """Drop this instance's reference to the shared pool; when the last
        reference for a DSN goes, close+forget the physical pool so idle backend
        connections drain immediately and a restart won't leak."""
        if not self._hold:
            return
        self._hold = False
        self._pool = None
        entry = _SHARED.get(self._dsn)
        if entry is None:
            return
        entry["refs"] = max(0, entry["refs"] - 1)
        if entry["refs"] == 0:
            pool, entry["pool"] = entry["pool"], None
            _SHARED.pop(self._dsn, None)
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    pass

    async def close(self) -> None:
        await self._release()
