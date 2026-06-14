"""Cap1 determinism check for R3: run alpha_gate twice via subprocess.

Proves that backtest_gate results are deterministic (fixed DB + pure functions).

Usage:
    python ops/scripts/determinism_r3.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "alpha_gate.py")
PYTHON = str(Path(__file__).parent.parent.parent / "venv" / "bin" / "python")

KEYS = [
    "fa_gate_status",
    "fa_mean_is_sharpe",
    "fa_mean_oos_sharpe",
    "fa_deflated_sharpe",
    "fa_pbo",
    "sa_gate_status",
    "sa_p_value",
    "sa_mean_is_sharpe",
    "sa_mean_oos_sharpe",
    "sa_deflated_sharpe",
    "sa_pbo",
]


def run_once(run_id: int) -> dict:
    result = subprocess.run(
        [PYTHON, SCRIPT, "--quiet"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent.parent),
    )
    if result.returncode != 0:
        print(f"[run {run_id}] STDERR (last 10 lines):")
        for line in result.stderr.strip().splitlines()[-10:]:
            print(f"  {line}")
        raise RuntimeError(f"alpha_gate subprocess failed (exit {result.returncode})")

    parsed: dict = {}
    for line in result.stdout.splitlines():
        if ":" in line and line.strip():
            k, _, v = line.strip().partition(":")
            parsed[k.strip()] = v.strip()
    return parsed


def main() -> None:
    print("=== R3 Determinism check: running alpha_gate twice (subprocess isolation) ===\n")

    r1 = run_once(1)
    r2 = run_once(2)

    print(f"Run 1: fa_oos={r1.get('fa_mean_oos_sharpe')}  fa_gate={r1.get('fa_gate_status')}  "
          f"sa_oos={r1.get('sa_mean_oos_sharpe')}  sa_gate={r1.get('sa_gate_status')}")
    print(f"Run 2: fa_oos={r2.get('fa_mean_oos_sharpe')}  fa_gate={r2.get('fa_gate_status')}  "
          f"sa_oos={r2.get('sa_mean_oos_sharpe')}  sa_gate={r2.get('sa_gate_status')}")

    print()
    all_ok = True
    for key in KEYS:
        match = r1.get(key) == r2.get(key)
        status = "OK  " if match else "FAIL"
        print(f"  [{status}] {key}: {r1.get(key)!r} == {r2.get(key)!r}")
        if not match:
            all_ok = False

    print()
    if all_ok:
        print("R3 DETERMINISM CHECK PASSED — both runs identical")
        sys.exit(0)
    else:
        print("R3 DETERMINISM CHECK FAILED — runs diverged!")
        sys.exit(1)


if __name__ == "__main__":
    main()
