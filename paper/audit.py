"""paper.audit — GOLD-tier Ed25519 audit record for every paper trading signal.

Every paper signal decision is signed with Ed25519 before order submission.
The signed record provides tamper-evident audit trail for the holdout verification.

Configuration (via env or .env):
  HELIVEX_AUDIT_PRIVATE_KEY_B64  — base64-encoded 32-byte Ed25519 private key
  HELIVEX_AUDIT_PUBLIC_KEY_B64   — base64-encoded 32-byte Ed25519 public key

If keys not configured, records are fingerprinted but unsigned (tier='STANDARD').

Self-contained: replicates omodul.audit_record's algorithm exactly
(obase.canonical_json + obase.sha256_hash + oprim.ed25519_sign) so records
verify identically in gateway (which checks the Ed25519 sig over fingerprint_hex).
Fingerprint = SHA-256(canonical_json(event_body)); sig = Ed25519(fingerprint).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

_PRIVATE_KEY_B64 = os.environ.get("HELIVEX_AUDIT_PRIVATE_KEY_B64", "")
_PUBLIC_KEY_B64  = os.environ.get("HELIVEX_AUDIT_PUBLIC_KEY_B64", "")

_TIER = "GOLD" if _PRIVATE_KEY_B64 else "STANDARD"


def _canonical_json(obj: Any) -> bytes:
    """UTF-8 JSON bytes with sorted keys; numpy scalars coerced to native.

    Identical to obase.canonical_json so fingerprints match the write/verify path.
    """
    def _default(o: Any) -> Any:
        try:
            import numpy as np  # noqa: PLC0415
        except ImportError:
            raise TypeError(f"Object of type {type(o).__name__} is not JSON serialisable") from None
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serialisable")

    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=_default).encode()


def sign_signal(event_body: dict[str, Any]) -> dict[str, Any]:
    """Create a GOLD (signed) or STANDARD (fingerprint-only) audit record.

    Returns a dict with the same key fields as the original:
      record_id      — first 16 hex chars of fingerprint
      fingerprint_hex — SHA-256 of canonical event_body
      sig_b64        — Ed25519 signature over the fingerprint (empty if no key)
      tier           — 'GOLD' or 'STANDARD'
      body           — the original event_body
    """
    fingerprint: bytes = hashlib.sha256(_canonical_json(event_body)).digest()
    fingerprint_hex: str = fingerprint.hex()

    sig_b64 = ""
    if _PRIVATE_KEY_B64:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(_PRIVATE_KEY_B64))
        sig_b64 = base64.b64encode(key.sign(fingerprint)).decode()

    return {
        "status": "ok",
        "record_id": fingerprint_hex[:16],
        "fingerprint_hex": fingerprint_hex,
        "sig_b64": sig_b64,
        "tier": _TIER,
        "body": event_body,
    }


def public_key_b64() -> str:
    return _PUBLIC_KEY_B64


def audit_tier() -> str:
    return _TIER
