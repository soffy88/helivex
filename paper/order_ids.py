"""Alphanumeric client order IDs for OKX.

OKX requires `clOrdId` to match ^[a-zA-Z0-9]{1,32}$. NautilusTrader's default
client_order_id is `O-YYYYMMDD-HHMMSS-<trader>-<strategy>-<n>` which contains
dashes — OKX rejects every such order with "Parameter clOrdId error" (this is
what caused 669/669 rejected, 0 fills). The OKX adapter passes the NT id through
unmodified, so we must generate a venue-safe id ourselves and hand it to the
order factory explicitly.

Format:  hvx<ms-base36><counter-base36><tag>   (truncated to 32 chars)
  - ms      : milliseconds since 2023-11-14, base36 → ~7 chars, time-unique
  - counter : process-monotonic, base36 → disambiguates same-ms orders
  - tag     : cleaned strategy id suffix, for human readability (may truncate)
Uniqueness comes from ms+counter (kept ahead of the truncation point).
"""
from __future__ import annotations

import itertools
import time

from nautilus_trader.model.identifiers import ClientOrderId

_ALNUM = "0123456789abcdefghijklmnopqrstuvwxyz"
_EPOCH_MS = 1_700_000_000_000  # 2023-11-14, shrinks the timestamp
_counter = itertools.count(1)


def _b36(n: int) -> str:
    if n <= 0:
        return "0"
    out: list[str] = []
    while n:
        n, r = divmod(n, 36)
        out.append(_ALNUM[r])
    return "".join(reversed(out))


def _clean(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum())


def next_client_order_id(tag: str = "") -> ClientOrderId:
    """Return a unique OKX-safe (alphanumeric, ≤32 char) ClientOrderId."""
    ms = int(time.time() * 1000) - _EPOCH_MS
    raw = f"hvx{_b36(ms)}{_b36(next(_counter))}{_clean(tag)}"
    return ClientOrderId(raw[:32])
