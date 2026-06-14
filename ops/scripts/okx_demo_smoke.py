"""OKX Demo adapter smoke test via NautilusTrader.

Reads credentials from .env (project root or ~/projects/helivex/.env):
  OKX_API_KEY=...
  OKX_API_SECRET=...
  OKX_API_PASSPHRASE=...

Run: python ops/scripts/okx_demo_smoke.py
"""
import asyncio
import os
import sys
from pathlib import Path


def load_env(paths: list[Path]) -> None:
    for p in paths:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            print(f"[env] loaded {p}")
            return
    print("[env] no .env file found; falling back to existing env vars")


load_env([
    Path(__file__).parent.parent.parent / ".env",  # helivex/.env
    Path.home() / ".config" / "keys" / ".env",
])

_required = ["OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"]
_missing = [k for k in _required if not os.environ.get(k)]
if _missing:
    print(f"[SKIP] Missing env vars: {_missing}")
    print("       Add OKX Demo keys to helivex/.env and re-run.")
    sys.exit(0)


async def main() -> None:
    from nautilus_trader.core.nautilus_pyo3 import (
        OKXHttpClient,
        OKXEnvironment,
        OKXInstrumentType,
    )

    print("[okx] creating DEMO HTTP client …")
    # from_env() reads OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE
    # and uses OKX_ENVIRONMENT if set, otherwise defaults to LIVE.
    # We force DEMO here via the environment kwarg.
    os.environ["OKX_ENVIRONMENT"] = "demo"
    client = OKXHttpClient.from_env()
    print(f"[okx] client base_url: {client.base_url}")

    print("[okx] fetching server time …")
    ts = await client.get_server_time()
    print(f"[okx] server_time: {ts}")

    print("[okx] fetching BTC-USDT-SWAP instruments …")
    instruments = await client.request_instruments(
        instrument_type=OKXInstrumentType.SWAP,
    )
    btc = [i for i in instruments if "BTC" in str(i.id)][:3]
    print(f"[okx] instruments (BTC sample): {[str(i.id) for i in btc]}")

    print("[okx] requesting BTC-USDT-SWAP bars (1m, last 5) …")
    from nautilus_trader.core.nautilus_pyo3 import OKXBarType
    bars = await client.request_bars(
        bar_type=OKXBarType.from_str("BTC-USDT-SWAP-1m"),
        limit=5,
    )
    print(f"[okx] bars received: {len(bars)}")
    for b in bars:
        print(f"  {b}")

    print("\n=== OKX Demo adapter smoke test PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
