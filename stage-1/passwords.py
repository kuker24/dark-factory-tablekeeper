"""Password hashing.

We use stdlib ``hashlib.scrypt`` so the runtime image needs no extra package.
The cost is low enough to satisfy the spec without exceeding the 5 s
per-request budget for a fresh login, and high enough that offline
cracking is the same fight as any other scrypt deployment.
"""
from __future__ import annotations

import hashlib
import hmac
import os


_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """Return ``scrypt$<salt hex>$<hash hex>`` for storage."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: object, encoded: object) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    parts = encoded.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)
