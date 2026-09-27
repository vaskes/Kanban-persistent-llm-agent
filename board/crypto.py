"""
Reversible encryption for secrets stored in the database.

A model credential is something the runtime needs to send back to a remote
service verbatim (an Authorization header, for example), so a one-way hash is
not enough. We need the plaintext at request time.

Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package is the right
primitive: authenticated, deterministic round-trip, no nonce management. The
key is derived from Django's SECRET_KEY via PBKDF2 so we do not have to ship a
separate secret, and so rotating SECRET_KEY rotates every stored credential.

The threat model is "an attacker who got read-only access to a database
backup". It is NOT "an attacker who got Django process memory". Anything in
process memory can be had by other means.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings


_FERNET_SALT = b"kanban-agent.model-key.v1"
_KDF_ITERS = 390_000  # OWASP 2023+ floor for PBKDF2-HMAC-SHA256


def _fernet() -> Fernet:
    secret = settings.SECRET_KEY.encode("utf-8")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_FERNET_SALT,
        iterations=_KDF_ITERS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret))
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a URL-safe base64 token."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a token produced by encrypt_secret. Returns "" on empty / bad input.

    A bad token returns "" rather than raising because losing a credential to a
    recoverable error must not take down the whole board; the missing
    credential will surface as a provider error on next use.
    """
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
