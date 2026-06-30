"""Unit tests for the gateway shared-token guard (gateway.auth.require_token).
Pure logic — no DB, CI-safe."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from gateway.auth import require_token


def test_disabled_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("HELIVEX_GW_TOKEN", raising=False)
    # auth off: any header (incl. None) is allowed
    assert require_token(None) is None
    assert require_token("anything") is None


def test_correct_token_passes(monkeypatch) -> None:
    monkeypatch.setenv("HELIVEX_GW_TOKEN", "s3cret")
    assert require_token("s3cret") is None


def test_missing_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("HELIVEX_GW_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        require_token(None)
    assert exc.value.status_code == 401


def test_wrong_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("HELIVEX_GW_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        require_token("wrong")
    assert exc.value.status_code == 401
