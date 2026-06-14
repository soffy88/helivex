"""Determinism check: run the same backtest twice via subprocess and assert identical results.

Proves Cap1 — backtest is deterministic (fixed DB data + NT single-thread engine).
Each run is isolated in its own Python process to avoid NautilusTrader global state.

Usage:
    python ops/scripts/determinism_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "backtest_from_db.py")
PYTHON = str(Path(__file__).parent.parent.parent / "venv" / "bin" / "python")


def run_once(run_id: int, months: int = 3) -> dict:
    """Run backtest_from_db in a fresh subprocess, return parsed result dict."""
    result = subprocess.run(
        [PYTHON, SCRIPT, "--months", str(months), "--quiet"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent.parent),
    )
    if result.returncode != 0:
        print(f"[run {run_id}] STDERR (last 10 lines):")
        for line in result.stderr.strip().splitlines()[-10:]:
            print(f"  {line}")
        raise RuntimeError(f"Backtest subprocess failed (exit {result.returncode})")

    # Parse "  key  : value" lines from stdout
    parsed: dict = {}
    for line in result.stdout.splitlines():
        if ":" in line and line.strip():
            k, _, v = line.strip().partition(":")
            parsed[k.strip()] = v.strip()
    return parsed


def main() -> None:
    print("=== Determinism check: running backtest twice (subprocess isolation) ===\n")

    r1 = run_once(1)
    print(f"Run 1: bars={r1.get('bars_loaded')} fills={r1.get('fill_count')} pnl={r1.get('realized_pnl_usd')}")

    r2 = run_once(2)
    print(f"Run 2: bars={r2.get('bars_loaded')} fills={r2.get('fill_count')} pnl={r2.get('realized_pnl_usd')}")

    checks = ["bars_loaded", "fill_count", "realized_pnl_usd"]
    all_ok = True
    print()
    for key in checks:
        match = r1.get(key) == r2.get(key)
        status = "OK  " if match else "FAIL"
        print(f"  [{status}] {key}: {r1.get(key)!r} == {r2.get(key)!r}")
        if not match:
            all_ok = False

    print()
    if all_ok:
        print("DETERMINISM CHECK PASSED — both runs identical")
        sys.exit(0)
    else:
        print("DETERMINISM CHECK FAILED — runs diverged!")
        sys.exit(1)


if __name__ == "__main__":
    main()
