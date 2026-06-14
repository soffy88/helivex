"""CI gate: all 5 3O libs must resolve to platform/3O, never a stub or shadow.

Enforces:
  D3 — import path uniqueness (no split-brain installs)
  D2 — unidirectional dependency (3O never imports nautilus_trader)
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

PLATFORM_3O = Path.home() / "projects" / "platform" / "3O"

LIBS = ["obase", "oprim", "oskill", "omodul", "oservi"]


@pytest.mark.parametrize("lib_name", LIBS)
def test_import_path_in_platform_3o(lib_name: str) -> None:
    """Each lib's __file__ must be under ~/projects/platform/3O/."""
    mod = __import__(lib_name)
    lib_file = Path(inspect.getfile(mod)).resolve()
    expected_root = (PLATFORM_3O / lib_name).resolve()
    assert lib_file.is_relative_to(expected_root), (
        f"{lib_name} imported from {lib_file}, expected under {expected_root}. "
        "Possible shadow install — check venv editable links."
    )


@pytest.mark.parametrize("lib_name", LIBS)
def test_no_nautilus_import_in_3o(lib_name: str) -> None:
    """3O source files must never import nautilus_trader (unidirectional dep)."""
    lib_root = PLATFORM_3O / lib_name / lib_name
    if not lib_root.exists():
        pytest.skip(f"{lib_root} not found")

    violations: list[str] = []
    for py_file in lib_root.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    if name and name.startswith("nautilus"):
                        violations.append(f"{py_file.relative_to(PLATFORM_3O)}:{node.lineno}")

    assert not violations, (
        f"{lib_name} imports nautilus_trader — violates unidirectional dep:\n"
        + "\n".join(violations)
    )


def test_shell_strategy_calls_omodul() -> None:
    """ShellStrategy.on_bar must call omodul.compute_fingerprint (integration smoke)."""
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    instrument = TestInstrumentProvider.btcusdt_binance()
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")

    import pandas as pd
    ts = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1e9)
    bar = Bar(
        bar_type=bar_type,
        open=Price.from_str("40000.00"),
        high=Price.from_str("40050.00"),
        low=Price.from_str("39950.00"),
        close=Price.from_str("40010.00"),
        volume=Quantity.from_str("1.000000"),
        ts_event=ts,
        ts_init=ts,
    )

    from services.shell_strategy import ShellConfig, ShellStrategy

    config = ShellConfig(
        instrument_id=str(instrument.id),
        bar_type=str(bar_type),
    )
    strategy = ShellStrategy(config=config)
    strategy.on_bar(bar)

    assert len(strategy._last_fingerprint) == 24, (
        f"Expected 24-char fingerprint, got {strategy._last_fingerprint!r}"
    )
