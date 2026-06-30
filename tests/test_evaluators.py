"""Unit tests for monitor evaluators that don't require a live node/DB:
backup freshness (filesystem only) and write-freshness's node-down short-circuit.
CI-safe."""
from __future__ import annotations

import asyncio
import os
import time

from paper.evaluators import eval_backup_freshness, eval_write_freshness


def test_backup_freshness_no_dumps(tmp_path) -> None:
    r = asyncio.run(eval_backup_freshness(config={"backup_dir": str(tmp_path)}))
    assert r and r[0]["entity_id"] == "backup_freshness"


def test_backup_freshness_fresh(tmp_path) -> None:
    (tmp_path / "helivex_20260101_020000.dump").write_text("x")
    r = asyncio.run(eval_backup_freshness(
        config={"backup_dir": str(tmp_path), "max_age_hours": 26}))
    assert r == []


def test_backup_freshness_stale(tmp_path) -> None:
    p = tmp_path / "helivex_20260101_020000.dump"
    p.write_text("x")
    old = time.time() - 48 * 3600
    os.utime(p, (old, old))
    r = asyncio.run(eval_backup_freshness(
        config={"backup_dir": str(tmp_path), "max_age_hours": 26}))
    assert r and r[0]["severity"] == "high"


def test_write_freshness_skips_when_node_down(tmp_path) -> None:
    # No PID file -> node not running -> eval_node_alive owns it -> healthy ([]).
    r = asyncio.run(eval_write_freshness(
        config={"pid_file": str(tmp_path / "absent.pid")}))
    assert r == []
