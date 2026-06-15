#!/usr/bin/env python
"""Generate Ed25519 keypair for helivex audit signing (GOLD tier).

Run this once and add the output to helivex/.env.
The private key signs every paper trading signal; the public key verifies.

Usage:
    python tools/gen_audit_keys.py
"""
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

priv = Ed25519PrivateKey.generate()
pub  = priv.public_key()

priv_b64 = base64.b64encode(priv.private_bytes_raw()).decode()
pub_b64  = base64.b64encode(pub.public_bytes_raw()).decode()

print("# Paste into helivex/.env:")
print(f"HELIVEX_AUDIT_PRIVATE_KEY_B64={priv_b64}")
print(f"HELIVEX_AUDIT_PUBLIC_KEY_B64={pub_b64}")
