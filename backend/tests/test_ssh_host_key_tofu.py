"""SSH host keys: trust on first use (services/ssh_host_keys.py, migration 0216).

These tests run a REAL asyncssh server on 127.0.0.1 (the "box") — the host-key
check is asyncssh's own handshake logic, so a mock of asyncssh.connect would
prove nothing. "A swapped device" = a second server with a different host key
answering on the same address (the connect wrapper points at whichever box is
currently "on the network").

Core assertions (each one was sabotage-checked, see the PR):
  1. first connection stores the key on the host row (+ activity event)
  2. same key again → connection works, key unchanged
  3. swapped key → refused BEFORE any command runs, refusal recorded once
  4. re-trust (admin endpoint) with the right fingerprint → new key accepted
  5. onboarding: a known box with a swapped key never receives the password
  6. system ssh (backend → MC host): accept-new + MC-owned known_hosts, never
     StrictHostKeyChecking=no
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import asyncssh
import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.host import Host
from app.services import runtime_manager, ssh_host_keys
from app.services.host_resolver import ResolvedHost, resolved_host_from_row
from tests.conftest import test_engine

CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")


class _Box:
    """One SSH server = one physical device on the network."""

    def __init__(self, *algs: str) -> None:
        # Like a real sshd: one host key per type. Default: ed25519 only.
        self.keys = [
            asyncssh.generate_private_key(a, key_size=2048) if a == "ssh-rsa" else asyncssh.generate_private_key(a)
            for a in (algs or ("ssh-ed25519",))
        ]
        self.host_key = self.keys[0]
        self.commands: list[str] = []
        self.passwords: list[str] = []
        self.server = None
        self.port = 0

    @property
    def fingerprint(self) -> str:
        return self.host_key.convert_to_public().get_fingerprint()

    def public(self, alg: str) -> asyncssh.SSHKey:
        return next(k for k in self.keys if k.get_algorithm() == alg).convert_to_public()

    async def start(self) -> "_Box":
        box = self

        class _Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                box.passwords.append(password)
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return True

        async def _handle(process):
            box.commands.append(process.command)
            process.stdout.write("ok\n")
            process.exit(0)

        self.server = await asyncssh.create_server(
            _Server, "127.0.0.1", 0, server_host_keys=self.keys, process_factory=_handle,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        # Bounded: a connection a (sabotaged) test left open must not hang teardown.
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), timeout=5)
        except asyncio.TimeoutError:
            pass


class _Network:
    """Which box answers on the host's address right now."""

    def __init__(self, box: _Box) -> None:
        self.box = box


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture
async def boxes():
    real, impostor = await _Box().start(), await _Box().start()
    yield real, impostor
    await real.stop()
    await impostor.stop()


@pytest.fixture
def network(boxes, monkeypatch):
    """Route every asyncssh.connect to the box currently on the network and
    give _ssh_run a client key (no key files in tests)."""
    net = _Network(boxes[0])
    real_connect = asyncssh.connect

    def _connect(*args, **kwargs):
        kwargs.pop("port", None)
        return real_connect(*args, port=net.box.port, **kwargs)

    monkeypatch.setattr(asyncssh, "connect", _connect)

    async def _keys(target):
        return [CLIENT_KEY]

    monkeypatch.setattr(runtime_manager, "_resolve_ssh_client_keys", _keys)
    return net


async def _make_host(**kw) -> Host:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        host = Host(slug=kw.pop("slug", "box-a"), display_name="Box A", kind="ssh",
                    ssh_host="127.0.0.1", ssh_user="mc", **kw)
        s.add(host)
        await s.commit()
        await s.refresh(host)
        return host


async def _reload(host_id) -> Host:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Host, host_id)


async def _events(event_type: str) -> list[ActivityEvent]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(select(ActivityEvent).where(ActivityEvent.event_type == event_type))).all())


# ── 1 + 2: first connection learns, same key passes ─────────────────────────


async def test_first_connection_stores_host_key_and_logs_event(network, boxes):
    real, _ = boxes
    host = await _make_host()
    assert host.ssh_host_key is None  # like every row from before migration 0216

    out, _, code = await runtime_manager._ssh_run("echo hi", host=resolved_host_from_row(host))

    assert (out, code) == ("ok", 0)
    stored = await _reload(host.id)
    assert ssh_host_keys.fingerprint(stored.ssh_host_key) == real.fingerprint
    assert stored.ssh_host_key_learned_at is not None
    events = await _events("host.ssh_key_learned")
    assert len(events) == 1 and events[0].detail["fingerprint"] == real.fingerprint


async def test_same_key_connects_again_without_relearning(network, boxes):
    host = await _make_host()
    await runtime_manager._ssh_run("first", host=resolved_host_from_row(host))
    learned = await _reload(host.id)

    out, _, code = await runtime_manager._ssh_run("second", host=resolved_host_from_row(learned))

    assert (out, code) == ("ok", 0)
    assert boxes[0].commands == ["first", "second"]
    again = await _reload(host.id)
    assert again.ssh_host_key == learned.ssh_host_key
    assert again.ssh_host_key_learned_at == learned.ssh_host_key_learned_at
    assert len(await _events("host.ssh_key_learned")) == 1


# ── 3: swapped device is refused ─────────────────────────────────────────────


async def test_swapped_key_is_refused_before_any_command(network, boxes):
    real, impostor = boxes
    host = await _make_host()
    await runtime_manager._ssh_run("learn", host=resolved_host_from_row(host))

    network.box = impostor  # a different device now answers on 127.0.0.1
    with pytest.raises(ssh_host_keys.HostKeyMismatchError) as exc_info:
        await runtime_manager._ssh_run("rm -rf /important", host=resolved_host_from_row(await _reload(host.id)))

    assert impostor.commands == []  # the command never reached the impostor
    err = exc_info.value
    assert isinstance(err, asyncssh.Error)  # existing `except asyncssh.Error` handlers still catch it
    assert err.error_code == "ssh_host_key_mismatch"
    assert real.fingerprint in str(err) and impostor.fingerprint in str(err)
    assert "/api/v1/hosts/box-a/ssh-host-key/trust" in str(err)

    stored = await _reload(host.id)
    assert ssh_host_keys.fingerprint(stored.ssh_host_key) == real.fingerprint  # NOT overwritten
    assert ssh_host_keys.fingerprint(stored.ssh_host_key_rejected) == impostor.fingerprint

    # A watcher retrying every few seconds must not flood the activity feed.
    with pytest.raises(ssh_host_keys.HostKeyMismatchError):
        await runtime_manager._ssh_run("retry", host=resolved_host_from_row(stored))
    assert len(await _events("host.ssh_key_mismatch")) == 1


async def test_real_box_still_works_after_an_impostor_was_refused(network, boxes):
    real, impostor = boxes
    host = await _make_host()
    await runtime_manager._ssh_run("learn", host=resolved_host_from_row(host))
    network.box = impostor
    with pytest.raises(ssh_host_keys.HostKeyMismatchError):
        await runtime_manager._ssh_run("x", host=resolved_host_from_row(await _reload(host.id)))
    network.box = real

    out, _, code = await runtime_manager._ssh_run("back", host=resolved_host_from_row(await _reload(host.id)))
    assert (out, code) == ("ok", 0)


# ── 4: operator re-trust ─────────────────────────────────────────────────────


async def test_retrust_accepts_exactly_the_refused_key(network, boxes, auth_client):
    real, impostor = boxes  # "impostor" = the reinstalled box in this story
    host = await _make_host()
    await runtime_manager._ssh_run("learn", host=resolved_host_from_row(host))
    network.box = impostor
    with pytest.raises(ssh_host_keys.HostKeyMismatchError):
        await runtime_manager._ssh_run("x", host=resolved_host_from_row(await _reload(host.id)))

    listed = (await auth_client.get("/api/v1/hosts")).json()
    row = next(h for h in listed if h["slug"] == "box-a")
    assert row["ssh_host_key_fingerprint"] == real.fingerprint
    assert row["ssh_host_key_rejected_fingerprint"] == impostor.fingerprint

    wrong = await auth_client.post("/api/v1/hosts/box-a/ssh-host-key/trust", json={"fingerprint": real.fingerprint})
    assert wrong.status_code == 409
    assert ssh_host_keys.fingerprint((await _reload(host.id)).ssh_host_key) == real.fingerprint

    ok = await auth_client.post("/api/v1/hosts/box-a/ssh-host-key/trust", json={"fingerprint": impostor.fingerprint})
    assert ok.status_code == 200, ok.text
    assert ok.json()["fingerprint"] == impostor.fingerprint
    stored = await _reload(host.id)
    assert ssh_host_keys.fingerprint(stored.ssh_host_key) == impostor.fingerprint
    assert stored.ssh_host_key_rejected is None
    assert len(await _events("host.ssh_key_trusted")) == 1

    out, _, code = await runtime_manager._ssh_run("after", host=resolved_host_from_row(stored))
    assert (out, code) == ("ok", 0)
    assert impostor.commands == ["after"]


async def test_retrust_without_a_refused_key_is_409(auth_client):
    await _make_host(slug="box-b")
    resp = await auth_client.post("/api/v1/hosts/box-b/ssh-host-key/trust", json={"fingerprint": "SHA256:x"})
    assert resp.status_code == 409


async def test_retrust_is_admin_only(client):
    resp = await client.post("/api/v1/hosts/box-a/ssh-host-key/trust", json={"fingerprint": "SHA256:x"})
    assert resp.status_code in (401, 403)


# ── targets without a host row stay unpinned (documented gap) ────────────────


async def test_target_without_host_row_connects_unpinned(network, boxes):
    target = ResolvedHost(ssh_host="127.0.0.1", ssh_user="mc", source="settings")
    out, _, code = await runtime_manager._ssh_run("adhoc", host=target)
    assert (out, code) == ("ok", 0)
    network.box = boxes[1]
    out, _, code = await runtime_manager._ssh_run("adhoc2", host=target)
    assert (out, code) == ("ok", 0)  # nothing to pin to — see ssh_host_keys docstring


# ── 5: onboarding ────────────────────────────────────────────────────────────


async def test_onboarding_known_box_with_swapped_key_never_gets_the_password(network, boxes):
    from app.services import host_onboarding

    real, impostor = boxes
    await _make_host(ssh_host_key=ssh_host_keys.export_key(real.host_key.convert_to_public()))
    network.box = impostor

    run = host_onboarding._Run(
        "job-x", host_onboarding.OnboardParams(address="127.0.0.1", username="mc", password="pw-should-not-leak"),
    )
    with pytest.raises(host_onboarding.OnboardingError) as exc_info:
        leaked = await run._connect_with_supplied_credential()
        leaked.close()  # only reached when the check is broken

    assert exc_info.value.status == "failed"
    assert "has changed" in exc_info.value.message
    assert impostor.passwords == []  # refused during the handshake, before auth


async def test_onboarding_new_box_learns_key_and_rejects_swap_before_gegenprobe(network, boxes):
    from app.services import host_onboarding

    real, impostor = boxes
    run = host_onboarding._Run(
        "job-y", host_onboarding.OnboardParams(address="127.0.0.1", username="mc", password="pw"),
    )
    conn = await run._connect_with_supplied_credential()
    conn.close()
    assert run.host_key is not None and run.host_key.get_fingerprint() == real.fingerprint

    network.box = impostor  # swapped between step 1 and step 3
    with pytest.raises(host_onboarding.OnboardingError) as exc_info:
        leaked = await run._connect_with_new_key(CLIENT_KEY)
        leaked.close()  # only reached when the check is broken
    assert "has changed" in exc_info.value.message


# ── 6: system ssh (backend container → MC host) ──────────────────────────────


def test_system_ssh_options_use_accept_new_and_mc_known_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.home_host", str(tmp_path))
    opts = ssh_host_keys.system_ssh_host_key_options()
    assert opts == [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={tmp_path / '.mc' / 'ssh' / 'known_hosts'}",
    ]
    assert (tmp_path / ".mc" / "ssh").is_dir()


def test_no_system_ssh_path_disables_host_key_checking(tmp_path, monkeypatch):
    from app.services import agent_bootstrap

    monkeypatch.setattr("app.config.settings.home_host", str(tmp_path))
    monkeypatch.setattr(agent_bootstrap.shutil, "which", lambda name: None)  # force the SSH branch
    for argv in (
        agent_bootstrap._launchctl_bootstrap_argv(Path("/x/com.mc.test.plist")),
        agent_bootstrap._launchctl_bootout_argv("com.mc.test"),
    ):
        assert argv[0] == "ssh"
        joined = " ".join(argv)
        assert "StrictHostKeyChecking=no" not in joined
        assert "UserKnownHostsFile=/dev/null" not in joined
        assert "StrictHostKeyChecking=accept-new" in joined


async def test_cli_terminal_host_ssh_uses_accept_new(tmp_path, monkeypatch):
    from app.routers import cli_terminal

    monkeypatch.setattr("app.config.settings.home_host", str(tmp_path))
    seen: list[tuple] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_exec(*argv, **kwargs):
        seen.append(argv)
        return _Proc()

    monkeypatch.setattr(cli_terminal.asyncio, "create_subprocess_exec", _fake_exec)
    await cli_terminal._ssh_host("true")
    joined = " ".join(seen[0])
    assert "StrictHostKeyChecking=accept-new" in joined
    assert "StrictHostKeyChecking=no" not in joined
    assert f"UserKnownHostsFile={tmp_path}/.mc/ssh/known_hosts" in joined


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_unreadable_stored_key_counts_as_not_learned():
    assert ssh_host_keys.parse_key("not a key") is None
    assert ssh_host_keys.fingerprint(None) is None


def test_pin_kwargs_never_disable_checking():
    pin = ssh_host_keys.HostKeyPin(label="x", host_id=uuid.uuid4())
    kw = pin.connect_kwargs()
    assert kw["known_hosts"] is not None and kw["known_hosts"] != b""


# ── review #822 H1: no host_id → pinned to the row with the same address ─────


async def test_settings_fallback_is_pinned_to_the_row_with_the_same_address(network, boxes, monkeypatch):
    """Idle stop / readiness gate pass to_registry_dict() (no host_id) and land
    on the settings fallback — same address as the registered row. That row's
    key must be learned and enforced, not skipped."""
    from app.services.host_resolver import settings_fallback_host

    real, impostor = boxes
    monkeypatch.setattr("app.config.settings.dgx_ssh_host", "127.0.0.1")
    monkeypatch.setattr("app.config.settings.dgx_ssh_user", "mc")
    host = await _make_host(slug="dgx-box")
    target = settings_fallback_host()
    assert target.host_id is None and target.source == "settings"

    assert (await runtime_manager._ssh_run("docker stop x", host=target))[2] == 0
    assert ssh_host_keys.fingerprint((await _reload(host.id)).ssh_host_key) == real.fingerprint

    network.box = impostor
    with pytest.raises(ssh_host_keys.HostKeyMismatchError) as exc_info:
        await runtime_manager._ssh_run("docker stop y", host=settings_fallback_host())
    assert impostor.commands == []
    assert "/api/v1/hosts/dgx-box/ssh-host-key/trust" in str(exc_info.value)
    assert ssh_host_keys.fingerprint((await _reload(host.id)).ssh_host_key_rejected) == impostor.fingerprint


async def test_two_rows_on_one_address_the_row_with_a_key_wins(network, boxes):
    real, impostor = boxes
    await _make_host(slug="a-unpinned-duplicate")
    keyed = await _make_host(
        slug="z-pinned", ssh_host_key=ssh_host_keys.export_key(real.host_key.convert_to_public()),
    )
    network.box = impostor
    target = ResolvedHost(ssh_host="127.0.0.1", ssh_user="mc", source="legacy_host_field")
    with pytest.raises(ssh_host_keys.HostKeyMismatchError):
        await runtime_manager._ssh_run("x", host=target)
    assert impostor.commands == []
    assert (await _reload(keyed.id)).ssh_host_key_rejected is not None


# ── review #822 M1/M2: which key is learned, and the pin holds ───────────────


@pytest.fixture
async def multi_key_boxes():
    real = await _Box("ssh-rsa", "ecdsa-sha2-nistp256", "ssh-ed25519").start()
    swapped = await _Box("ssh-rsa", "ecdsa-sha2-nistp256", "ssh-ed25519").start()
    yield real, swapped
    await real.stop()
    await swapped.stop()


async def test_box_with_rsa_and_ed25519_learns_ed25519(network, multi_key_boxes):
    real, _ = multi_key_boxes
    network.box = real
    host = await _make_host()
    await runtime_manager._ssh_run("learn", host=resolved_host_from_row(host))

    stored = ssh_host_keys.parse_key((await _reload(host.id)).ssh_host_key)
    assert stored.get_algorithm() == "ssh-ed25519"
    assert stored == real.public("ssh-ed25519")
    event = (await _events("host.ssh_key_learned"))[0]
    assert event.detail["algorithm"] == "ssh-ed25519"
    assert event.detail["key_file"] == "/etc/ssh/ssh_host_ed25519_key.pub"

    # the pin holds: the same box connects again with the learned ed25519 key
    assert (await runtime_manager._ssh_run("again", host=resolved_host_from_row(await _reload(host.id))))[2] == 0


async def test_existing_rsa_pin_keeps_working_and_mismatch_names_rsa(network, multi_key_boxes):
    """A row that learned RSA (asyncssh's own default before this fix) must
    keep connecting — asyncssh negotiates exactly the pinned algorithm — and a
    mismatch must point at the RSA key file, not at ed25519."""
    real, swapped = multi_key_boxes
    network.box = real
    host = await _make_host(ssh_host_key=ssh_host_keys.export_key(real.public("ssh-rsa")))

    assert (await runtime_manager._ssh_run("rsa ok", host=resolved_host_from_row(host)))[2] == 0
    assert ssh_host_keys.parse_key((await _reload(host.id)).ssh_host_key) == real.public("ssh-rsa")

    network.box = swapped
    with pytest.raises(ssh_host_keys.HostKeyMismatchError) as exc_info:
        await runtime_manager._ssh_run("x", host=resolved_host_from_row(await _reload(host.id)))
    err = exc_info.value
    assert swapped.commands == []
    assert err.expected_algorithm == "ssh-rsa" and err.presented_algorithm == "ssh-rsa"
    assert "ssh-rsa " + swapped.public("ssh-rsa").get_fingerprint() in str(err)
    assert "/etc/ssh/ssh_host_rsa_key.pub" in str(err)
    event = (await _events("host.ssh_key_mismatch"))[0]
    assert event.detail["presented_algorithm"] == "ssh-rsa"
    assert event.detail["key_file"] == "/etc/ssh/ssh_host_rsa_key.pub"


async def test_rsa_only_box_still_learns(network):
    box = await _Box("ssh-rsa").start()
    try:
        network.box = box
        host = await _make_host()
        assert (await runtime_manager._ssh_run("x", host=resolved_host_from_row(host)))[2] == 0
        assert ssh_host_keys.parse_key((await _reload(host.id)).ssh_host_key).get_algorithm() == "ssh-rsa"
        assert (await runtime_manager._ssh_run("y", host=resolved_host_from_row(await _reload(host.id))))[2] == 0
    finally:
        await box.stop()


def test_key_file_per_algorithm():
    assert ssh_host_keys.host_key_file("ssh-ed25519") == "/etc/ssh/ssh_host_ed25519_key.pub"
    assert ssh_host_keys.host_key_file("ecdsa-sha2-nistp256") == "/etc/ssh/ssh_host_ecdsa_key.pub"
    assert ssh_host_keys.host_key_file("ssh-rsa") == "/etc/ssh/ssh_host_rsa_key.pub"
