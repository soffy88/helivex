"""gateway.auth — optional shared-token guard for mutating endpoints.

Defense-in-depth on top of binding the gateway to 127.0.0.1. Enabled ONLY when
HELIVEX_GW_TOKEN is set (so local dev / first rollout stays open). When set,
guarded routes require header `X-Helivex-Token: <token>`. The Next.js /gw route
handler injects this header server-side, so the browser never holds the token.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def require_token(x_helivex_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject mutating calls lacking the shared token.

    No-op when HELIVEX_GW_TOKEN is unset (auth disabled). Uses a constant-time
    compare to avoid leaking the token via timing.
    """
    expected = os.environ.get("HELIVEX_GW_TOKEN", "")
    if not expected:
        return  # auth disabled — no token configured
    if not x_helivex_token or not hmac.compare_digest(x_helivex_token, expected):
        raise HTTPException(status_code=401, detail="invalid or missing gateway token")
