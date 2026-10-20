"""The single Docker gateway and its drain guard (ADR-094 decision 7).

While an MC update drains the system, nothing in the backend may create,
replace, start, restart or remove a container — the update engine is about to
swap the stack underneath it. Read-only calls (``ps``, ``inspect``, ``logs``)
and calls that only stop or run something inside a container stay free.
Without a drain, the gateway is a transparent pass-through: same argv, same
keyword arguments, same result — this PR changes no behaviour.
"""
from __future__ import annotations

import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.services import docker_gateway as gw
from app.services.docker_gateway import DockerDeferred, DrainState


class FakeSource:
    def __init__(self, state: DrainState | Exception):
        self.state = state
        self.reads = 0

    def read(self) -> DrainState:
        self.reads += 1
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


DRAINED = DrainState(active=True, run_id="run-1", reason="mc update")


@pytest.fixture
def drained():
    """The gateway sees an active drain for the length of one test."""
    previous = gw.set_drain_source(FakeSource(DRAINED))
    try:
        yield
    finally:
        gw.set_drain_source(previous)


@pytest.fixture(autouse=True)
def _fresh_cache():
    gw.reset_drain_cache()
    yield
    gw.reset_drain_cache()


# ── classification ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ["restart", "-t", "5", "mc-agent-x"],
        ["start", "mc-agent-x"],
        ["rm", "-f", "mc-agent-x"],
        ["run", "-d", "--name", "x", "img"],
        ["create", "img"],
        ["compose", "-p", "mission-control", "--env-file", "/r/.env", "-f", "/r/a.yml",
         "up", "-d", "--force-recreate", "mc-agent-x"],
        ["compose", "--project-name=mission-control", "restart", "backend"],
        ["container", "rm", "x"],
        ["container", "start", "x"],
    ],
)
def test_container_changes_are_deferred_in_drain(args):
    assert gw.deferred_in_drain(args) is True


@pytest.mark.parametrize(
    "args",
    [
        ["stop", "-t", "5", "mc-agent-x"],
        ["exec", "mc-agent-x", "true"],
        ["inspect", "-f", "{{.State.Running}}", "x"],
        ["ps", "-a"],
        ["logs", "x"],
        ["builder", "prune", "--force"],
        ["compose", "-p", "mission-control", "-f", "/r/a.yml", "ps"],
        ["compose", "-p", "mission-control", "logs", "backend"],
    ],
)
def test_reads_stops_and_exec_stay_free_in_drain(args):
    assert gw.deferred_in_drain(args) is False


def test_unknown_subcommand_counts_as_a_change():
    """Fail closed: a subcommand the gateway does not know is guarded."""
    assert gw.deferred_in_drain(["somethingnew", "x"]) is True
    assert gw.deferred_in_drain([]) is True


# ── drain cache ───────────────────────────────────────────────────────────


def test_drain_is_read_at_most_every_ten_seconds():
    source = FakeSource(DrainState(active=False))
    clock = FakeClock()
    guard = gw.DrainGuard(source, ttl=10.0, clock=clock)

    assert guard.state().active is False
    clock.now += 9.9
    source.state = DRAINED
    assert guard.state().active is False  # still cached
    assert source.reads == 1
    clock.now += 0.2
    assert guard.state().active is True  # refreshed after 10 s
    assert source.reads == 2


def test_failing_source_keeps_the_last_known_state():
    """A drain set before a DB hiccup must not lift because one read failed."""
    source = FakeSource(DRAINED)
    clock = FakeClock()
    guard = gw.DrainGuard(source, ttl=10.0, clock=clock)
    assert guard.state().active is True
    source.state = RuntimeError("db down")
    clock.now += 11
    assert guard.state().active is True


def test_failing_source_without_history_is_not_drained():
    guard = gw.DrainGuard(FakeSource(RuntimeError("db down")), ttl=10.0, clock=FakeClock())
    assert guard.state().active is False


def test_settings_source_reads_the_flag(monkeypatch):
    monkeypatch.setattr(gw.settings, "mc_update_drain", True)
    assert gw.SettingsDrainSource().read().active is True
    monkeypatch.setattr(gw.settings, "mc_update_drain", False)
    assert gw.SettingsDrainSource().read().active is False


def test_default_source_is_the_settings_flag(monkeypatch):
    monkeypatch.setattr(gw.settings, "mc_update_drain", True)
    gw.reset_drain_cache()
    assert gw.drain_state().active is True


# ── run / run_async ───────────────────────────────────────────────────────


def test_run_passes_through_without_drain():
    done = subprocess.CompletedProcess(["docker", "restart", "x"], 0, "ok", "")
    with patch("subprocess.run", return_value=done) as run:
        result = gw.run(["restart", "-t", "5", "x"], capture_output=True, text=True, timeout=60)
    assert result is done
    run.assert_called_once_with(
        ["docker", "restart", "-t", "5", "x"], capture_output=True, text=True, timeout=60
    )


def test_run_in_drain_raises_deferred_and_never_calls_docker(drained):
    with patch("subprocess.run") as run, pytest.raises(DockerDeferred) as exc:
        gw.run(["restart", "x"], capture_output=True, text=True, timeout=60)
    run.assert_not_called()
    assert "deferred" in str(exc.value)
    assert exc.value.run_id == "run-1"


def test_run_in_drain_lets_reads_through(drained):
    done = subprocess.CompletedProcess(["docker", "stop", "x"], 0, "", "")
    with patch("subprocess.run", return_value=done) as run:
        gw.run(["stop", "-t", "5", "x"], capture_output=True, text=True, timeout=60)
    run.assert_called_once()


@pytest.mark.anyio
async def test_run_async_passes_through_without_drain():
    proc = object()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as cse:
        got = await gw.create_subprocess(["start", "x"], stdout=-1, stderr=-1)
    assert got is proc
    cse.assert_awaited_once_with("docker", "start", "x", stdout=-1, stderr=-1)


@pytest.mark.anyio
async def test_run_async_in_drain_raises_deferred(drained):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as cse, \
         pytest.raises(DockerDeferred):
        await gw.create_subprocess(["compose", "-p", "p", "up", "-d", "x"])
    cse.assert_not_awaited()


# ── the call sites (sabotage a: drain on → no compose call) ───────────────


def _agent(name: str = "Drain Probe"):
    return SimpleNamespace(
        id=uuid.uuid4(), name=name, agent_runtime="cli-bridge", archived_at=None
    )


def test_agent_recreate_in_drain_runs_no_compose(drained, tmp_path, monkeypatch):
    from app.services import docker_agent_sync

    monkeypatch.setattr(docker_agent_sync, "compose_preflight_error", lambda *a, **k: None)
    monkeypatch.setattr(docker_agent_sync, "disk_build_preflight_error", lambda: None)
    with patch("subprocess.run") as run:
        result = docker_agent_sync.restart_docker_agent_container(_agent(), force_recreate=True)
    run.assert_not_called()
    assert result["status"].startswith("error: deferred"), result
    assert result["mode"] == "recreate"


def test_agent_restart_in_drain_runs_no_docker(drained):
    from app.services import docker_agent_sync

    with patch("subprocess.run") as run:
        result = docker_agent_sync.restart_docker_agent_container(_agent())
    run.assert_not_called()
    assert result["status"].startswith("error: deferred"), result


def test_agent_start_and_remove_in_drain_are_deferred(drained):
    from app.services import docker_agent_sync

    with patch("subprocess.run") as run:
        started = docker_agent_sync.start_docker_agent_container("probe")
        removed = docker_agent_sync.remove_docker_agent_container("probe")
    run.assert_not_called()
    assert started["ok"] == "false" and "deferred" in started["error"]
    assert removed["ok"] == "false" and "deferred" in removed["error"]


def test_agent_stop_in_drain_still_runs(drained):
    """Stopping is never deferred — it starts no work and the operator may need it."""
    from app.services import docker_agent_sync

    done = subprocess.CompletedProcess([], 0, "", "")
    with patch("subprocess.run", return_value=done) as run:
        result = docker_agent_sync.stop_docker_agent_container("probe")
    assert run.call_args[0][0] == ["docker", "stop", "-t", "5", "mc-agent-probe"]
    assert result["ok"] == "true"


def test_agent_restart_without_drain_calls_docker_unchanged():
    from app.services import docker_agent_sync

    done = subprocess.CompletedProcess([], 0, "", "")
    with patch("subprocess.run", return_value=done) as run:
        result = docker_agent_sync.restart_docker_agent_container(_agent())
    assert run.call_args[0][0] == ["docker", "restart", "-t", "5", "mc-agent-drain-probe"]
    assert run.call_args.kwargs == {"capture_output": True, "text": True, "timeout": 60}
    assert result["status"] == "restarted"


@pytest.mark.anyio
async def test_docker_action_in_drain_answers_503(drained):
    from app.routers import cli_terminal

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as cse, \
         pytest.raises(HTTPException) as exc:
        await cli_terminal._docker_action("restart", "mc-agent-x")
    cse.assert_not_awaited()
    assert exc.value.status_code == 503
    assert "deferred" in exc.value.detail


@pytest.mark.anyio
async def test_force_recreate_route_in_drain_answers_503(drained, auth_client, session):
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="Drain Recreate", agent_runtime="cli-bridge")
    session.add(agent)
    await session.commit()

    with patch("app.services.docker_agent_sync.compose_preflight_error", new=lambda *a, **k: None), \
         patch("app.services.disk_preflight.build_preflight_error", new=lambda: None), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock()) as cse:
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/force-recreate")

    assert resp.status_code == 503, resp.text
    assert "deferred" in resp.json()["detail"]
    cse.assert_not_awaited()


def test_claude_login_container_in_drain_is_deferred(drained):
    from app.services import claude_account

    with patch("subprocess.run") as run:
        code, out = claude_account._docker(["run", "-d", "--name", "x", "img"], 15)
    run.assert_not_called()
    assert code != 0 and "deferred" in out


def test_claude_account_reads_still_run_in_drain(drained):
    from app.services import claude_account

    done = subprocess.CompletedProcess([], 0, "running\n", "")
    with patch("subprocess.run", return_value=done) as run:
        code, out = claude_account._docker(["inspect", "-f", "{{.State.Status}}", "x"], 15)
    run.assert_called_once()
    assert (code, out) == (0, "running\n")


def test_any_source_is_drained_when_one_source_is():
    off = FakeSource(DrainState(active=False))
    on = FakeSource(DRAINED)
    assert gw.AnyDrainSource(off, on).read() == DRAINED
    assert gw.AnyDrainSource(off, off).read().active is False
