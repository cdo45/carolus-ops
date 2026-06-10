"""Crypto round-trip and key-handling tests."""

import pytest
from cryptography.fernet import Fernet

from sync import crypto


@pytest.fixture
def key_env(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APP_ENCRYPTION_KEY", key)
    return key


def test_round_trip(key_env: str) -> None:
    secret = "rt-1234567890-secret"
    ciphertext = crypto.encrypt(secret)
    assert secret not in ciphertext, "ciphertext must not contain the plaintext"
    assert crypto.decrypt(ciphertext) == secret


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(crypto.MissingEncryptionKey):
        crypto.encrypt("anything")


def test_wrong_key_fails_closed(key_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ciphertext = crypto.encrypt("secret")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(crypto.InvalidToken):
        crypto.decrypt(ciphertext)


def test_corrupt_ciphertext_fails_closed(key_env: str) -> None:
    with pytest.raises(crypto.InvalidToken):
        crypto.decrypt("not-a-fernet-token")
