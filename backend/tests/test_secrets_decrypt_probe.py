"""PR 2-5: startup probe that detects an undecryptable SECRETS_ENCRYPTION_KEY.

If the key changes (e.g. after moving the DB to a new host) `safe_decrypt`
silently returns None today — agents fail with no clear reason. The probe
tries to decrypt one existing secret once at startup; on failure /health
reports "degraded" with a message pointing at the backup docs, instead of
staying silently "ok".
"""
import pytest

from app.config import settings
from app.models.secret import Secret
from app.services import secrets_probe
from app.services.encryption import encrypt


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    from app.services import encryption as encryption_module

    secrets_probe.reset_for_tests()
    yield
    secrets_probe.reset_for_tests()
    encryption_module._fernet = None


async def test_probe_ok_when_no_secrets_exist(session):
    result = await secrets_probe.probe_secrets_decryptable(session)

    assert result is True
    assert secrets_probe.secrets_decryptable() is True


async def test_probe_ok_with_correct_key(session, monkeypatch):
    monkeypatch.setattr(settings, "secrets_encryption_key", "probe-test-key-correct")
    secret = Secret(key="probe_test_secret", encrypted_value=encrypt("super-secret-value"))
    session.add(secret)
    await session.commit()

    result = await secrets_probe.probe_secrets_decryptable(session)

    assert result is True
    assert secrets_probe.secrets_decryptable() is True


async def test_probe_degraded_with_wrong_key(session, monkeypatch):
    # Encrypt under one key, then "move the DB" by switching to another key.
    monkeypatch.setattr(settings, "secrets_encryption_key", "probe-test-key-original")
    from app.services import encryption as encryption_module
    encryption_module._fernet = None
    secret = Secret(key="probe_test_secret_2", encrypted_value=encrypt("super-secret-value"))
    session.add(secret)
    await session.commit()

    monkeypatch.setattr(settings, "secrets_encryption_key", "probe-test-key-DIFFERENT")
    encryption_module._fernet = None

    result = await secrets_probe.probe_secrets_decryptable(session)

    assert result is False
    assert secrets_probe.secrets_decryptable() is False


async def test_health_degraded_when_secrets_undecryptable(client, monkeypatch):
    monkeypatch.setattr(secrets_probe, "secrets_decryptable", lambda: False)

    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["message"] == (
        "secrets cannot be decrypted — restore the .env that belongs to "
        "this database (see backup docs)"
    )


async def test_health_ok_shape_unchanged_when_secrets_decryptable(client, monkeypatch):
    monkeypatch.setattr(secrets_probe, "secrets_decryptable", lambda: True)

    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "message" not in body
