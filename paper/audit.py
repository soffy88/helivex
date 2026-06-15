"""paper.audit — GOLD-tier Ed25519 audit record for every paper trading signal.

Every paper signal decision is signed with Ed25519 before order submission.
The signed record provides tamper-evident audit trail for the holdout verification.

Configuration (via env or .env):
  HELIVEX_AUDIT_PRIVATE_KEY_B64  — base64-encoded 32-byte Ed25519 private key
  HELIVEX_AUDIT_PUBLIC_KEY_B64   — base64-encoded 32-byte Ed25519 public key

If keys not configured, records are fingerprinted but unsigned (tier='STANDARD').
"""
from __future__ import annotations

import os
from typing import Any

from omodul.audit_record import AuditRecordConfig, audit_record

_PRIVATE_KEY_B64 = os.environ.get("HELIVEX_AUDIT_PRIVATE_KEY_B64", "")
_PUBLIC_KEY_B64  = os.environ.get("HELIVEX_AUDIT_PUBLIC_KEY_B64", "")

_TIER = "GOLD" if _PRIVATE_KEY_B64 else "STANDARD"

_CONFIG = AuditRecordConfig(
    event_type="paper_signal",
    actor_id="helivex-paper",
    tier=_TIER,
    private_key_b64=_PRIVATE_KEY_B64,
)


def sign_signal(event_body: dict[str, Any]) -> dict[str, Any]:
    """Create a GOLD (signed) or STANDARD (fingerprint-only) audit record.

    Returns the full result dict from audit_record. Key fields:
      record_id      — first 16 hex chars of fingerprint
      fingerprint_hex — SHA-256 of canonical event_body
      sig_b64        — Ed25519 signature (empty if no private key)
      tier           — 'GOLD' or 'STANDARD'
    """
    return audit_record(event_body, config=_CONFIG)


def public_key_b64() -> str:
    return _PUBLIC_KEY_B64


def audit_tier() -> str:
    return _TIER
