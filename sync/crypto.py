"""Fernet encryption for secrets at rest (OAuth tokens in sync_connections).

Key comes from APP_ENCRYPTION_KEY (Doppler-sourced env). Plaintext token
values must never be stored, logged, or printed — encrypt on the way into
the DB, decrypt only at the moment of use.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["InvalidToken", "MissingEncryptionKey", "decrypt", "encrypt"]


class MissingEncryptionKey(RuntimeError):
    """APP_ENCRYPTION_KEY is not set in the environment."""


def _fernet() -> Fernet:
    key = os.environ.get("APP_ENCRYPTION_KEY")
    if not key:
        raise MissingEncryptionKey(
            "APP_ENCRYPTION_KEY is not set — see .env.example for how to generate one"
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage; returns Fernet ciphertext (urlsafe str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt stored ciphertext; raises InvalidToken if corrupt or wrong key."""
    return _fernet().decrypt(ciphertext.encode()).decode()
