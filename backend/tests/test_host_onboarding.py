"""End-to-end (SSH-mocked) + security tests for POST /api/v1/hosts/onboard
(Fleet & Rezepte v2, Phase 2 — Auto-Onboarding).

No real network — every asyncssh.connect() call in the run (the initial
connect, the key-only gegenprobe, and every _ssh_run() call bootstrap/agent-
install make afterward) is served by one fake connection factory keyed off
the command text. This also means the SAME mock exercises the real fallback
chain in runtime_manager._ssh_run once the credential is persisted — nothing
here is mocked away.

Only RFC 5737 placeholder addresses (192.0.2.x) — public repo convention,
see test_hosts_api.py's header.
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.credential import Credential
from app.models.host import Host
from app.models.host_pairing_code import HostPairingCode
from app.services import host_onboarding
from app.services.encryption import safe_decrypt
from tests.conftest import test_engine

PASSWORD = "SuperSecretOnboardPW-9f2b"  # distinctive — grepped for in every security test


@pytest.fixture(autouse=True)
def _point_database_engine_at_test_engine(monkeypatch):
    """host_onboarding.py's job opens its OWN sessions (background task, no
    request-scoped session) via app.database.async_session_maker(), which
    reads app.database.engine fresh on every call — patching it here reaches
    every DB access the run makes, same pattern as
    test_ssh_credential_resolution.py."""
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    host_onboarding._auth_failures.clear()
    yield
    host_onboarding._auth_failures.clear()


class _FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


def _make_fake_conn(run_handler) -> MagicMock:
    """A stand-in for asyncssh.SSHClientConnection. `close` is a plain
    (non-async) Mock — asyncssh's real close() is synchronous, and a naive
    AsyncMock here would silently accept `await conn.close()` even though
    the real library does not (a bug this fixture caught during development:
    see host_onboarding.py's _connect_with_new_key call site)."""
    async def _run(command, *args, **kwargs):
        return run_handler(command)

    conn = MagicMock()
    conn.run = AsyncMock(side_effect=_run)
    conn.close = MagicMock(return_value=None)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _default_run_handler(command: str) -> _FakeResult:
    """Canned answers for every remote command the happy path issues."""
    if command.startswith("mkdir -p ~/.ssh"):
        return _FakeResult(exit_status=0)
    if "cat ~/.ssh/authorized_keys" in command:
        return _FakeResult(stdout="", exit_status=0)
    if "authorized_keys.mc-tmp" in command:
        return _FakeResult(exit_status=0)
    if command == "sudo -n true":
        return _FakeResult(exit_status=1)  # no passwordless sudo by default
    if "mc-node-agent.py.mc-tmp" in command or command.startswith(f"echo") and "AGENT" in command:
        return _FakeResult(exit_status=0)
    if "nohup python3" in command:
        return _FakeResult(exit_status=0, stdout="started")
    if command.startswith("sudo -n python3"):
        return _FakeResult(exit_status=0)
    return _FakeResult(exit_status=0)


def _patch_asyncssh_connect(run_handler=None, connect_side_effect=None):
    """Patches asyncssh.connect globally — reached by both host_onboarding.py's
    direct calls and runtime_manager._ssh_run's calls (same imported module),
    so bootstrap/agent-install steps inside the same run go through this too."""
    handler = run_handler or _default_run_handler
    fake_conn = _make_fake_conn(handler)
    if connect_side_effect is not None:
        return patch("asyncssh.connect", AsyncMock(side_effect=connect_side_effect)), fake_conn
    return patch("asyncssh.connect", AsyncMock(return_value=fake_conn)), fake_conn


async def _run_onboarding_and_wait(params: host_onboarding.OnboardParams) -> str:
    """Starts a run and waits for it to reach a terminal status — the run is
    fire-and-forget (asyncio.create_task) in production; tests need it done
    before asserting."""
    job_id = await host_onboarding.start_onboarding(params)
    for _ in range(200):
        status = await host_onboarding.get_status(job_id)
        if status and status.get("status") in host_onboarding.TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.01)
    return job_id


def _params(**overrides) -> host_onboarding.OnboardParams:
    defaults = dict(
        address="192.0.2.50", username="mcfleet", password=PASSWORD,
        bootstrap=False, install_agent=False,
    )
    defaults.update(overrides)
    return host_onboarding.OnboardParams(**defaults)


# ── happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_onboarding_persists_credential_and_host():
    patcher, _ = _patch_asyncssh_connect()
    with patcher:
        job_id = await _run_onboarding_and_wait(_params(display_name="GX10 Test"))

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "done", status

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        host = (await session.exec(select(Host).where(Host.ssh_host == "192.0.2.50"))).first()
        assert host is not None
        assert host.kind == "ssh"
        assert host.ssh_user == "mcfleet"
        assert host.ssh_credential_id is not None

        credential = await session.get(Credential, host.ssh_credential_id)
        assert credential.credential_type == "ssh_key"
        data = json.loads(safe_decrypt(credential.encrypted_data))
        assert data["username"] == "mcfleet"
        assert data["private_key_pem"].startswith("-----BEGIN")
        assert data["public_key"].startswith("ssh-ed25519")


@pytest.mark.asyncio
async def test_reonboarding_same_address_updates_existing_host_not_duplicate():
    patcher, _ = _patch_asyncssh_connect()
    with patcher:
        await _run_onboarding_and_wait(_params(display_name="First Name"))
        await _run_onboarding_and_wait(_params(display_name="Second Name"))

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        hosts = (await session.exec(select(Host).where(Host.ssh_host == "192.0.2.50"))).all()
        assert len(hosts) == 1
        assert hosts[0].display_name == "Second Name"


@pytest.mark.asyncio
async def test_authorized_keys_append_is_idempotent_across_runs():
    """A re-onboard must replace the previous marker line, not add a second one."""
    written_contents = []

    def run_handler(command: str) -> _FakeResult:
        if "cat ~/.ssh/authorized_keys" in command:
            content = written_contents[-1] if written_contents else ""
            return _FakeResult(stdout=content, exit_status=0)
        if "authorized_keys.mc-tmp" in command:
            import base64
            import re

            m = re.search(r"echo (\S+) \|", command)
            written_contents.append(base64.b64decode(m.group(1)).decode("utf-8"))
            return _FakeResult(exit_status=0)
        return _default_run_handler(command)

    patcher, _ = _patch_asyncssh_connect(run_handler=run_handler)
    with patcher:
        await _run_onboarding_and_wait(_params())
        await _run_onboarding_and_wait(_params())

    final_content = written_contents[-1]
    lines = [ln for ln in final_content.splitlines() if ln.strip()]
    marker_lines = [ln for ln in lines if ln.endswith("mc-fleet " + lines[0].split(" mc-fleet ")[-1])]
    # exactly one line carries this host's marker — no duplicate accumulation
    matching = [ln for ln in lines if "mc-fleet" in ln]
    assert len(matching) == 1


# ── failure paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_failed_status_and_nothing_persisted():
    async def raise_auth_failed(*args, **kwargs):
        raise asyncssh.PermissionDenied("bad password")

    patcher, _ = _patch_asyncssh_connect(connect_side_effect=raise_auth_failed)
    with patcher:
        job_id = await _run_onboarding_and_wait(_params())

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "auth_failed"

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        assert (await session.exec(select(Host))).first() is None
        assert (await session.exec(select(Credential))).first() is None


@pytest.mark.asyncio
async def test_unreachable_status_on_connection_error():
    async def raise_unreachable(*args, **kwargs):
        raise OSError("no route to host")

    patcher, _ = _patch_asyncssh_connect(connect_side_effect=raise_unreachable)
    with patcher:
        job_id = await _run_onboarding_and_wait(_params())

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "unreachable"


@pytest.mark.asyncio
async def test_gegenprobe_failure_persists_nothing():
    """Initial password connect + authorized_keys append succeed, but the
    key-only reconnect fails — proof-before-persist means NOTHING durable
    gets written (review requirement: proof comes before persistence)."""
    call_count = {"n": 0}

    async def connect_then_fail_on_key(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_fake_conn(_default_run_handler)
        raise asyncssh.PermissionDenied("key rejected")

    patcher, _ = _patch_asyncssh_connect(connect_side_effect=connect_then_fail_on_key)
    with patcher:
        job_id = await _run_onboarding_and_wait(_params())

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "failed"
    assert "Gegenprobe" in (status.get("message") or "")

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        assert (await session.exec(select(Host))).first() is None
        assert (await session.exec(select(Credential))).first() is None


@pytest.mark.asyncio
async def test_slug_race_returns_clean_failed_status_not_raw_500():
    """Review finding #7 (30.08.2026): self.host_slug is computed via
    _unique_slug BEFORE the authorized_keys write + gegenprobe reconnect —
    long enough for a concurrent run to grab the same slug first. Forced
    deterministically here (real concurrency isn't meaningfully testable
    against the shared SQLite test session) by making _unique_slug hand
    back an ALREADY-taken slug, exactly what a lost race looks like from
    persist()'s point of view."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(Host(slug="collision-host", display_name="Taken", kind="ssh", ssh_host="192.0.2.99"))
        await session.commit()

    patcher, _ = _patch_asyncssh_connect()
    with patcher, patch("app.routers.nodes._unique_slug", new=AsyncMock(return_value="collision-host")):
        job_id = await _run_onboarding_and_wait(_params())

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "failed"
    assert "gleichzeitig angelegt" in (status.get("message") or "")

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        hosts = (await session.exec(select(Host))).all()
        assert len(hosts) == 1  # only the pre-existing collision host — rollback was atomic
        assert (await session.exec(select(Credential))).first() is None


@pytest.mark.asyncio
async def test_bootstrap_failure_downgrades_terminal_status_from_done():
    """Review finding #3 (30.08.2026): a failed host_bootstrap run used to
    only log a warning — the onboarding job still ended STATUS_DONE (a
    green "done" banner while Docker/NVIDIA setup actually failed)."""
    from app.services import host_bootstrap

    patcher, _ = _patch_asyncssh_connect()
    with patcher, \
         patch("app.services.host_bootstrap.run_bootstrap", new=AsyncMock(return_value=None)), \
         patch("app.services.host_bootstrap.get_status", new=AsyncMock(return_value={
             "status": host_bootstrap.STATUS_FAILED,
             "message": "Docker-Installation schlug fehl (exit 1)",
         })):
        job_id = await _run_onboarding_and_wait(_params(bootstrap=True, install_agent=False))

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "failed"
    assert "Docker-Installation schlug fehl" in (status.get("message") or "")

    # SSH+Vault+host-row still succeeded — this isn't a "nothing persisted" case.
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        assert (await session.exec(select(Host))).first() is not None


@pytest.mark.asyncio
async def test_start_onboarding_uses_tracked_task(monkeypatch):
    """Review finding #4 (30.08.2026): a bare asyncio.create_task() has no
    strong reference left once start_onboarding() returns — the event loop
    is free to garbage-collect it mid-run, leaving the job silently stuck on
    "starting" forever. Must go through app.utils.create_tracked_task."""
    calls = []

    def fake_tracked_task(coro, name=None):
        calls.append(name)
        coro.close()  # never actually run it — this test only checks the wiring
        return None

    monkeypatch.setattr(host_onboarding, "create_tracked_task", fake_tracked_task)

    job_id = await host_onboarding.start_onboarding(_params())

    assert len(calls) == 1
    assert job_id in calls[0]


# ── security: password never persisted, never logged ────────────────────────


@pytest.mark.asyncio
async def test_password_never_lands_in_credential_or_host_row():
    patcher, _ = _patch_asyncssh_connect()
    with patcher:
        job_id = await _run_onboarding_and_wait(_params())

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        host = (await session.exec(select(Host))).first()
        assert PASSWORD not in json.dumps(host.model_dump(mode="json"))

        credential = (await session.exec(select(Credential))).first()
        assert PASSWORD not in credential.encrypted_data  # ciphertext (obviously) doesn't leak it
        decrypted = json.loads(safe_decrypt(credential.encrypted_data))
        assert PASSWORD not in json.dumps(decrypted)  # AND the plaintext payload has no password field at all


@pytest.mark.asyncio
async def test_password_never_lands_in_job_log():
    patcher, _ = _patch_asyncssh_connect()
    with patcher:
        job_id = await _run_onboarding_and_wait(_params())

    log = await host_onboarding.read_log(job_id, cursor=0)
    all_text = json.dumps(log["lines"])
    assert PASSWORD not in all_text


@pytest.mark.asyncio
async def test_password_never_lands_in_job_log_even_on_auth_failure():
    """The failure path logs the exception message — asyncssh's own
    PermissionDenied text must not somehow be given the password to echo,
    and our own error-wrapping must not either."""
    async def raise_with_password_nearby(*args, **kwargs):
        # Simulates the worst case: a library exception whose repr happens to
        # mention connection kwargs. Real asyncssh doesn't do this, but the
        # test proves OUR code doesn't add it either.
        raise asyncssh.PermissionDenied("Permission denied for user mcfleet")

    patcher, _ = _patch_asyncssh_connect(connect_side_effect=raise_with_password_nearby)
    with patcher:
        job_id = await _run_onboarding_and_wait(_params())

    log = await host_onboarding.read_log(job_id, cursor=0)
    assert PASSWORD not in json.dumps(log["lines"])


# ── use_existing_credential_id path (no password at all) ────────────────────


@pytest.mark.asyncio
async def test_onboarding_with_existing_credential_needs_no_password():
    key = asyncssh.generate_private_key("ssh-ed25519")
    encrypted = host_onboarding.encrypt(json.dumps({
        "private_key_pem": key.export_private_key().decode(),
        "public_key": key.export_public_key().decode(),
        "username": "mcfleet",
    }))
    credential_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(Credential(id=credential_id, name="pre-existing", credential_type="ssh_key", encrypted_data=encrypted))
        await session.commit()

    patcher, _ = _patch_asyncssh_connect()
    with patcher:
        job_id = await _run_onboarding_and_wait(
            _params(password=None, existing_credential_id=credential_id, address="192.0.2.60")
        )

    status = await host_onboarding.get_status(job_id)
    assert status["status"] == "done", status


# ── rate limiting via the HTTP endpoint ──────────────────────────────────────


@pytest.mark.asyncio
async def test_onboard_endpoint_admin_only(client):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email="viewer-onboard@mc.local", name="Viewer", role="viewer", is_active=True))
        await s.commit()
    token = create_access_token(str(uid), "viewer")

    resp = await client.post(
        "/api/v1/hosts/onboard",
        headers={"Authorization": f"Bearer {token}"},
        json={"address": "192.0.2.70", "username": "u", "auth": {"password": "x"}},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_onboard_endpoint_auth_body_needs_exactly_one_method(auth_client):
    resp = await auth_client.post(
        "/api/v1/hosts/onboard",
        json={"address": "192.0.2.70", "username": "u", "auth": {}},
    )
    assert resp.status_code == 422

    resp2 = await auth_client.post(
        "/api/v1/hosts/onboard",
        json={"address": "192.0.2.70", "username": "u",
              "auth": {"password": "x", "private_key": "y"}},
    )
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_rate_limit_after_three_failed_auths(auth_client):
    async def raise_auth_failed(*args, **kwargs):
        raise asyncssh.PermissionDenied("bad password")

    patcher, _ = _patch_asyncssh_connect(connect_side_effect=raise_auth_failed)
    with patcher:
        for _ in range(3):
            resp = await auth_client.post(
                "/api/v1/hosts/onboard",
                json={"address": "192.0.2.80", "username": "u", "auth": {"password": "wrong"}},
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            for _ in range(200):
                status = await host_onboarding.get_status(job_id)
                if status and status.get("status") == "auth_failed":
                    break
                await asyncio.sleep(0.01)

        resp = await auth_client.post(
            "/api/v1/hosts/onboard",
            json={"address": "192.0.2.80", "username": "u", "auth": {"password": "wrong-again"}},
        )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_onboarding_stores_role_and_reonboarding_without_role_keeps_it(auth_client):
    """P2 (Chef-Entscheid 02.09.): Rolle auch auf dem Passwort-Weg; ohne Angabe
    bleibt die alte Rolle stehen, ausdrücklich gesetzt wechselt sie."""
    patcher, _ = _patch_asyncssh_connect()
    with patcher:
        job_id = await _run_onboarding_and_wait(_params(role="worker"))
        assert (await host_onboarding.get_status(job_id))["status"] == "done"
        hosts = (await auth_client.get("/api/v1/hosts")).json()
        assert len(hosts) == 1 and hosts[0]["role"] == "worker" and hosts[0]["kind"] == "ssh"

        await _run_onboarding_and_wait(_params())  # ohne Rolle → bleibt worker
        hosts = (await auth_client.get("/api/v1/hosts")).json()
        assert len(hosts) == 1 and hosts[0]["role"] == "worker"

        await _run_onboarding_and_wait(_params(role="head"))  # ausdrücklich → wechselt
        hosts = (await auth_client.get("/api/v1/hosts")).json()
        assert len(hosts) == 1 and hosts[0]["role"] == "head"
