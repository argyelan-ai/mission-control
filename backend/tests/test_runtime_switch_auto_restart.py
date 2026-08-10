"""Tests for Task #26 — the runtime switch auto-triggers the agent restart.

Before this, a successful `agent.runtime_switched` left the caller to find a
separate restart button; if they forgot (or the agent was mid-task), the
process kept running the OLD model while DB/UI already showed the new one.

Coverage:
  a. Host-agent switch triggers the strong process-level restart
     (host_harness_adapter reload() now calls _host_agent_process_restart,
     not the weak _host_agent_lifecycle("restart") kickstart).
  b. Container-agent switch triggers the existing recreate path
     (restart_docker_agent_container) — unchanged, still automatic.
  c. Busy agent (current_task_id set, force_when_in_progress=True) → switch
     succeeds, restart is SKIPPED, and the skip is noted in the result +
     the emitted activity event.
  d. restart_after_switch=False → restart skipped regardless of busy state.
  e. Restart itself fails → switch DB state is NOT rolled back (unlike a
     health-check failure), and the failure is surfaced.

SSH/Docker/tmux are mocked throughout — no real subprocess, no real
launchctl, matching test_host_agent_process_restart.py's convention.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.agent_runtime_switch import switch_agent_runtime
from sqlmodel import select


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


async def _mk_runtime(session, *, slug, rtype="openai_compatible", model="model-x",
                       endpoint="https://example.com/v1"):
    rt = Runtime(slug=slug, display_name=slug, runtime_type=rtype,
                 endpoint=endpoint, model_identifier=model, enabled=True)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_hermes(session, rt, *, current_task_id=None):
    agent = Agent(name="Hermes", role="developer", agent_runtime="host",
                  harness="hermes", runtime_id=rt.id, slug="hermes",
                  current_task_id=current_task_id)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _mk_docker_agent(session, rt, *, current_task_id=None):
    agent = Agent(name=f"A-{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge",
                  runtime_id=rt.id, cli_plugins=[], current_task_id=current_task_id)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _docker_side_effects(*, restart_status="restarted", health_ok=True):
    restart_mock = AsyncMock()  # unused sentinel; real patches built per-test
    return restart_mock


# ── a. Host-agent switch → strong process-level restart ────────────────────


@pytest.mark.asyncio
async def test_host_switch_calls_strong_process_restart_not_weak_kickstart(
    async_session, fake_redis, tmp_path, monkeypatch
):
    """Task #25/#26 root cause: adapter.reload() used to call
    _host_agent_lifecycle("restart"), a plain launchd kickstart that never
    touches the tmux 'hermes-worker' session — the switch reported success
    while the TUI kept serving the stale model. reload() must now call the
    same strong restart as POST /host-agents/{id}/restart-process.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "hermes"
    d.mkdir(parents=True)
    (d / "agent.env").write_text(
        "MC_AGENT_TOKEN='keep'\nOPENAI_BASE_URL='http://old'\nOPENAI_MODEL='old'\n"
    )
    old_rt = await _mk_runtime(async_session, slug="hermes-vllm", rtype="hermes",
                                endpoint="http://192.0.2.10:8000/v1", model="qwen")
    new_rt = await _mk_runtime(async_session, slug="ollama-cloud")
    agent = await _mk_hermes(async_session, old_rt)

    ssh_calls: list[str] = []

    async def fake_ssh(cmd: str) -> str:
        ssh_calls.append(cmd)
        if "pgrep" in cmd:
            return "1234"
        if cmd.startswith("curl"):
            return '{"ok": true}'
        return "EXIT:0"

    from app.services import agent_runtime_switch as sw
    from app.services import sse as sse_mod

    with (
        patch.object(sw, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch("app.routers.cli_terminal._ssh_host", side_effect=fake_ssh),
    ):
        result = await switch_agent_runtime(async_session, agent, new_rt.id)

    assert result.restart_skipped is False
    # The strong restart path sweeps orphans + kickstarts + verifies via
    # pgrep + (for hermes specifically) hits the worker-session restart curl.
    joined = " ".join(ssh_calls)
    assert "pgrep" in joined
    assert any(c.startswith("curl") for c in ssh_calls), (
        "hermes worker-session restart (POST /restart) must run — this is "
        "exactly what the weak _host_agent_lifecycle path skipped"
    )


# ── b. Container-agent switch → existing recreate path (unchanged) ─────────


@pytest.mark.asyncio
async def test_container_switch_still_calls_restart_docker_agent_container(async_session, fake_redis):
    from app.services import agent_runtime_switch as sw

    rt_old = await _mk_runtime(async_session, slug="lms-old", rtype="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="vllm-new", rtype="vllm_docker")
    agent = await _mk_docker_agent(async_session, rt_old)

    restart_calls: list[dict] = []

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        restart_calls.append({"force_recreate": force_recreate, "agent": a.id})
        return {"status": "recreated", "container": "x", "mode": "recreate"}

    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        result = await switch_agent_runtime(async_session, agent, rt_new.id)

    assert result.restart_skipped is False
    assert len(restart_calls) == 1
    assert restart_calls[0]["agent"] == agent.id


# ── c. Busy agent → switch saved, restart skipped + noted ──────────────────


@pytest.mark.asyncio
async def test_busy_agent_switch_saved_restart_skipped_and_noted_container(async_session, fake_redis):
    from app.services import agent_runtime_switch as sw

    rt_old = await _mk_runtime(async_session, slug="lms-old", rtype="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="vllm-new", rtype="vllm_docker")
    task_id = uuid.uuid4()
    agent = await _mk_docker_agent(async_session, rt_old, current_task_id=task_id)

    restart_calls: list = []
    restart_mock = lambda a, **k: restart_calls.append(k) or {"status": "recreated"}

    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=restart_mock), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock()), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        result = await switch_agent_runtime(
            async_session, agent, rt_new.id, force_when_in_progress=True,
        )

    # Switch DID persist ...
    await async_session.refresh(agent)
    assert agent.runtime_id == rt_new.id
    # ... but the container was never touched.
    assert restart_calls == []
    assert result.restart_skipped is True
    assert result.restart_skip_reason and str(task_id) in result.restart_skip_reason

    events = (await async_session.exec(select(ActivityEvent))).all()
    switched = [e for e in events if e.event_type == "agent.runtime_switched"]
    assert len(switched) == 1
    assert switched[0].detail["restart_skipped"] is True
    assert switched[0].detail["restart_skip_reason"]


@pytest.mark.asyncio
async def test_busy_host_agent_switch_saved_restart_skipped(async_session, fake_redis, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "hermes"
    d.mkdir(parents=True)
    (d / "agent.env").write_text("MC_AGENT_TOKEN='keep'\nOPENAI_BASE_URL='http://old'\nOPENAI_MODEL='old'\n")
    old_rt = await _mk_runtime(async_session, slug="hermes-vllm", rtype="hermes",
                                endpoint="http://192.0.2.10:8000/v1", model="qwen")
    new_rt = await _mk_runtime(async_session, slug="ollama-cloud")
    task_id = uuid.uuid4()
    agent = await _mk_hermes(async_session, old_rt, current_task_id=task_id)

    from app.services import agent_runtime_switch as sw
    from app.services import sse as sse_mod

    with (
        patch.object(sw, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.host_harness_adapter.HermesAdapter.reload",
              new=AsyncMock(return_value={"ok": True})) as mock_reload,
    ):
        result = await switch_agent_runtime(
            async_session, agent, new_rt.id, force_when_in_progress=True,
        )

    await async_session.refresh(agent)
    assert agent.runtime_id == new_rt.id
    mock_reload.assert_not_awaited()
    assert result.restart_skipped is True
    assert str(task_id) in (result.restart_skip_reason or "")


# ── d. restart_after_switch=False → always skipped ──────────────────────────


@pytest.mark.asyncio
async def test_restart_after_switch_false_skips_even_when_idle(async_session, fake_redis):
    from app.services import agent_runtime_switch as sw

    rt_old = await _mk_runtime(async_session, slug="lms-old", rtype="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="vllm-new", rtype="vllm_docker")
    agent = await _mk_docker_agent(async_session, rt_old)

    restart_calls: list = []
    restart_mock = lambda a, **k: restart_calls.append(k) or {"status": "recreated"}

    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=restart_mock), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock()), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        result = await switch_agent_runtime(
            async_session, agent, rt_new.id, restart_after_switch=False,
        )

    await async_session.refresh(agent)
    assert agent.runtime_id == rt_new.id  # DB switch still applied
    assert restart_calls == []
    assert result.restart_skipped is True
    assert result.restart_skip_reason == "restart_after_switch=false — Neustart bewusst übersprungen."


# ── e. Restart failure → switch is NOT rolled back ──────────────────────────


@pytest.mark.asyncio
async def test_host_restart_failure_does_not_roll_back_switch(async_session, fake_redis, tmp_path, monkeypatch):
    """A restart failure is a "the agent still needs a manual bump" problem,
    not a "the switch itself was bad" problem. agent.env already has the new
    binding rendered by the time reload() is attempted, so unlike a failed
    compose render (which DOES still roll back — that's a genuine "the switch
    didn't take" failure), a failed restart leaves the switch committed and
    reports the error instead.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "hermes"
    d.mkdir(parents=True)
    (d / "agent.env").write_text("MC_AGENT_TOKEN='keep'\nOPENAI_BASE_URL='http://old'\nOPENAI_MODEL='old'\n")
    old_rt = await _mk_runtime(async_session, slug="hermes-vllm", rtype="hermes",
                                endpoint="http://192.0.2.10:8000/v1", model="qwen")
    new_rt = await _mk_runtime(async_session, slug="ollama-cloud")
    agent = await _mk_hermes(async_session, old_rt)

    from app.services import agent_runtime_switch as sw
    from app.services import sse as sse_mod

    with (
        patch.object(sw, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.host_harness_adapter.HermesAdapter.reload",
              new=AsyncMock(side_effect=RuntimeError("restart-process: no matching process"))),
    ):
        result = await switch_agent_runtime(async_session, agent, new_rt.id)

    # Switch stayed committed — this is the behaviour change from #26.
    await async_session.refresh(agent)
    assert agent.runtime_id == new_rt.id
    assert result.restart_failed is True
    assert result.restart_skipped is False

    events = (await async_session.exec(select(ActivityEvent))).all()
    switched = [e for e in events if e.event_type == "agent.runtime_switched"]
    assert len(switched) == 1
    assert switched[0].detail["restart_failed"] is True
    assert "no matching process" in switched[0].detail["restart_error"]
    assert switched[0].severity == "warning"


@pytest.mark.asyncio
async def test_docker_restart_command_failure_does_not_roll_back_switch(async_session, fake_redis):
    from app.services import agent_runtime_switch as sw

    rt_old = await _mk_runtime(async_session, slug="lms-old", rtype="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="vllm-new", rtype="vllm_docker")
    agent = await _mk_docker_agent(async_session, rt_old)

    def fake_restart_error(a, *, force_recreate=False, respawn_window_only=False):
        return {"status": "error: docker daemon unreachable"}

    health_mock = AsyncMock()  # must NOT be called — no point probing a container we never restarted
    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart_error), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", health_mock), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        result = await switch_agent_runtime(async_session, agent, rt_new.id)

    await async_session.refresh(agent)
    assert agent.runtime_id == rt_new.id  # NOT rolled back
    assert result.restart_failed is True
    health_mock.assert_not_awaited()

    events = (await async_session.exec(select(ActivityEvent))).all()
    switched = [e for e in events if e.event_type == "agent.runtime_switched"]
    assert len(switched) == 1
    assert switched[0].detail["restart_failed"] is True
    assert "docker daemon unreachable" in switched[0].detail["restart_error"]


@pytest.mark.asyncio
async def test_docker_health_check_failure_still_rolls_back_unchanged(async_session, fake_redis):
    """Distinct from the two tests above: a container that DID restart but
    never became healthy is still evidence the new runtime is broken, so the
    pre-#26 rollback safety net stays exactly as it was
    (test_health_check_failure_triggers_rollback covers the same contract on
    the main switch service test file; repeated narrowly here so the #26
    restart-failure/skip changes are proven not to have loosened it).
    """
    from app.services import agent_runtime_switch as sw

    rt_old = await _mk_runtime(async_session, slug="anthropic-claude-old2", rtype="anthropic_api")
    rt_new = await _mk_runtime(async_session, slug="new-oc2", rtype="vllm_docker")
    agent = await _mk_docker_agent(async_session, rt_old)

    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)), \
         patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container",
               side_effect=lambda a, **k: {"status": "recreated"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy",
               AsyncMock(return_value={"healthy": False, "reason": "timeout"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        with pytest.raises(sw.SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_new.id)

    await async_session.refresh(agent)
    assert agent.runtime_id == rt_old.id  # rolled back, unchanged from before #26
