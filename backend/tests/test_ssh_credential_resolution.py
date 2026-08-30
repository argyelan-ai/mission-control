"""_ssh_run's Vault-backed key resolution (Fleet & Rezepte v2, Phase 2 —
Auto-Onboarding). Fallback chain: Vault credential → ssh_key_path → settings.

No real network — asyncssh.connect itself is mocked where the whole
connection path is exercised; the resolver tests below only need a real DB
row + real Fernet encryption (already wired in conftest.py's test settings).
"""
import json
import uuid
from unittest.mock import AsyncMock, patch

import asyncssh
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.credential import Credential
from app.models.host import Host
from app.services.encryption import encrypt
from app.services.host_resolver import ResolvedHost
from app.services.runtime_manager import _resolve_ssh_client_keys, _ssh_run
from tests.conftest import test_engine


def _generate_keypair() -> tuple[str, str, str]:
    """Real Ed25519 keypair — same generator the onboarding flow itself uses.

    Returns (private_pem, public_key, fingerprint). asyncssh's OpenSSH
    private-key export is NOT byte-deterministic across calls (random
    padding/checkbytes) — re-exporting the same key twice yields different
    PEM text, so equality checks below compare fingerprints, not PEM strings.
    """
    key = asyncssh.generate_private_key("ssh-ed25519")
    return key.export_private_key().decode(), key.export_public_key().decode(), key.get_fingerprint()


async def _make_ssh_key_credential(private_key_pem: str, public_key: str = "ssh-ed25519 AAAA...") -> uuid.UUID:
    encrypted = encrypt(json.dumps({
        "private_key_pem": private_key_pem,
        "public_key": public_key,
        "username": "mcfleet",
    }))
    credential_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Credential(id=credential_id, name="test ssh key", credential_type="ssh_key", encrypted_data=encrypted))
        await s.commit()
    return credential_id


@pytest.fixture(autouse=True)
def _point_database_engine_at_test_engine(monkeypatch):
    """_load_vault_ssh_private_key opens its OWN session (it's called from
    background jobs with no request-scoped session) via a local
    `from app.database import engine` — re-resolved on every call, so
    patching the module attribute (same pattern as test_agent_create_flow.py)
    reaches it, unlike the app-wide get_session dependency override."""
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.mark.asyncio
async def test_resolve_ssh_client_keys_uses_vault_credential(async_session):
    private_pem, public_key, fingerprint = _generate_keypair()
    credential_id = await _make_ssh_key_credential(private_pem, public_key)

    target = ResolvedHost(ssh_host="192.0.2.10", ssh_credential_id=credential_id, ssh_key_path="/should/not/be/used")
    keys = await _resolve_ssh_client_keys(target)

    assert len(keys) == 1
    assert isinstance(keys[0], asyncssh.SSHKey)
    # Round-trips to the SAME key material, not just "some SSHKey"
    assert keys[0].get_fingerprint() == fingerprint


@pytest.mark.asyncio
async def test_resolve_ssh_client_keys_falls_back_to_path_without_credential():
    target = ResolvedHost(ssh_host="192.0.2.10", ssh_credential_id=None, ssh_key_path="/home/mc/.ssh/id_ed25519")
    keys = await _resolve_ssh_client_keys(target)
    assert keys == ["/home/mc/.ssh/id_ed25519"]


@pytest.mark.asyncio
async def test_resolve_ssh_client_keys_falls_back_when_credential_row_missing():
    target = ResolvedHost(
        ssh_host="192.0.2.10", ssh_credential_id=uuid.uuid4(), ssh_key_path="/home/mc/.ssh/id_ed25519"
    )
    keys = await _resolve_ssh_client_keys(target)
    assert keys == ["/home/mc/.ssh/id_ed25519"]


@pytest.mark.asyncio
async def test_resolve_ssh_client_keys_falls_back_when_credential_undecryptable(async_session):
    credential_id = uuid.uuid4()
    async_session.add(Credential(
        id=credential_id, name="broken", credential_type="ssh_key", encrypted_data="not-a-valid-fernet-token",
    ))
    await async_session.commit()

    target = ResolvedHost(
        ssh_host="192.0.2.10", ssh_credential_id=credential_id, ssh_key_path="/home/mc/.ssh/id_ed25519"
    )
    keys = await _resolve_ssh_client_keys(target)
    assert keys == ["/home/mc/.ssh/id_ed25519"]


@pytest.mark.asyncio
async def test_resolve_ssh_client_keys_falls_back_when_pem_malformed(async_session):
    encrypted = encrypt(json.dumps({"private_key_pem": "not actually a key", "username": "x"}))
    credential_id = uuid.uuid4()
    async_session.add(Credential(id=credential_id, name="malformed", credential_type="ssh_key", encrypted_data=encrypted))
    await async_session.commit()

    target = ResolvedHost(
        ssh_host="192.0.2.10", ssh_credential_id=credential_id, ssh_key_path="/home/mc/.ssh/id_ed25519"
    )
    keys = await _resolve_ssh_client_keys(target)
    assert keys == ["/home/mc/.ssh/id_ed25519"]


@pytest.mark.asyncio
async def test_ssh_run_uses_resolved_client_keys(async_session):
    """End-to-end through _ssh_run: the credential's key reaches
    asyncssh.connect's client_keys, not the (irrelevant here) ssh_key_path."""
    private_pem, _, fingerprint = _generate_keypair()
    credential_id = await _make_ssh_key_credential(private_pem)
    host = Host(slug="onboarded-box", display_name="Onboarded", kind="ssh",
                ssh_host="192.0.2.10", ssh_credential_id=credential_id)
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)

    from app.services.host_resolver import resolved_host_from_row
    resolved = resolved_host_from_row(host)

    fake_conn = AsyncMock()
    fake_result = AsyncMock()
    fake_result.stdout = "ok"
    fake_result.stderr = ""
    fake_result.exit_status = 0
    fake_conn.run = AsyncMock(return_value=fake_result)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.runtime_manager.asyncssh.connect", return_value=fake_conn) as connect_mock:
        stdout, _, exit_code = await _ssh_run("echo ok", host=resolved)

    assert exit_code == 0
    assert stdout == "ok"
    call_kwargs = connect_mock.call_args.kwargs
    assert len(call_kwargs["client_keys"]) == 1
    assert call_kwargs["client_keys"][0].get_fingerprint() == fingerprint
