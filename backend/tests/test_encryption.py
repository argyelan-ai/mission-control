from cryptography.fernet import Fernet

import app.services.encryption as encryption
from app.services.encryption import encrypt


def _fresh_fernet(monkeypatch, key: str) -> None:
    monkeypatch.setattr(encryption, "_fernet", None)
    monkeypatch.setattr(encryption.settings, "secrets_encryption_key", key)


def test_valid_fernet_key_is_used_as_is(monkeypatch):
    key = Fernet.generate_key().decode()
    _fresh_fernet(monkeypatch, key)

    assert encryption.decrypt(encryption.encrypt("secret")) == "secret"
    assert encryption._get_fernet()._signing_key == Fernet(key)._signing_key


def test_passphrase_is_derived_into_a_working_key(monkeypatch):
    # App-store installs supply arbitrary random strings, not Fernet keys.
    _fresh_fernet(monkeypatch, "just-a-random-store-generated-string-1234")

    assert encryption.decrypt(encryption.encrypt("secret")) == "secret"


def test_same_passphrase_derives_same_key_across_restarts(monkeypatch):
    _fresh_fernet(monkeypatch, "stable-passphrase")
    token = encryption.encrypt("secret")

    # Simulate a process restart: module-level Fernet cache is empty again.
    monkeypatch.setattr(encryption, "_fernet", None)
    assert encryption.decrypt(token) == "secret"


def test_empty_key_still_refuses_to_boot(monkeypatch):
    import pytest

    _fresh_fernet(monkeypatch, "")
    with pytest.raises(RuntimeError, match="not set"):
        encryption.encrypt("secret")


def test_encrypt_docstring_documents_plaintext_and_base64_ciphertext():
    docstring = encrypt.__doc__

    assert docstring is not None
    assert "Args:" in docstring
    assert "plaintext:" in docstring
    assert "Returns:" in docstring
    assert "base64" in docstring
    assert "Fernet" in docstring
