"""Phase 24 — Tests for Hermes host-side provisioning (HERM-01, plan 24-08).

Covers:
  1. test_bootstrap_renders_env_file — env file written with mode 600 + correct keys
  2. test_provision_endpoint_dispatches_hermes_branch — /provision routes to hermes
  3. test_provision_idempotent — re-running regenerates env without launchctl error
  4. test_launchctl_bootstrap_failure_rollback — non-zero rc triggers rollback to local
  5. test_home_host_env_var_respected — HOME_HOST overrides expanduser

All tests mock subprocess.run (launchctl) and use tmp_path for filesystem isolation.
"""
from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


@contextmanager
def _patch_redis(fake_redis):
    """Route all SSE/redis lookups to fakeredis so emit_event works."""
    async def _get():
        return fake_redis
    with patch("app.services.sse.get_redis", _get), \
         patch("app.redis_client.get_redis", _get):
        yield


HERMES_ENDPOINT = "http://192.0.2.10:8000/v1"
HERMES_MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"


def _make_hermes_runtime() -> Runtime:
    # ADR-064: the real Hermes agent binds a plain openai-protocol runtime
    # (e.g. a Spark vLLM registration) — "hermes" used to be a standalone
    # runtime_type sentinel that the pre-adapter dispatch branched on
    # (`if runtime.runtime_type == "hermes"`), but that coupling is exactly
    # what the HostHarnessAdapter registry replaces (Task 3/4). The harness
    # identity now lives on `agent.harness`, not on the runtime row.
    return Runtime(
        slug="hermes-vllm",
        display_name="Hermes (vLLM)",
        runtime_type="vllm_docker",
        endpoint=HERMES_ENDPOINT,
        model_identifier=HERMES_MODEL,
        enabled=True,
    )


async def _make_hermes_agent(session) -> Agent:
    """Persist a Hermes-style agent (agent_runtime='host') in the test DB."""
    runtime = _make_hermes_runtime()
    session.add(runtime)
    await session.commit()
    await session.refresh(runtime)

    agent = Agent(
        id=uuid.uuid4(),
        name="Hermes",
        agent_runtime="host",
        harness="hermes",
        runtime_id=runtime.id,
        provision_status="local",
        workspace_path="/Users/testuser/.mc/agents/hermes",
        model=HERMES_MODEL,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_bootstrap_renders_env_file(async_session, tmp_path, monkeypatch, fake_redis):
    """bootstrap_hermes_agent writes agent.env with mode 600 + required keys."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Mock launchctl as a successful no-op
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run",
        return_value=proc,
    ):
        from app.services.agent_bootstrap import bootstrap_hermes_agent

        agent = await _make_hermes_agent(async_session)
        runtime = await async_session.get(Runtime, agent.runtime_id)

        result = await bootstrap_hermes_agent(async_session, agent, runtime)

    env_path = tmp_path / ".mc" / "agents" / "hermes" / "agent.env"
    assert env_path.exists(), "agent.env not written"

    # mode 600
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f"expected mode 600, got {oct(mode)}"

    content = env_path.read_text()
    assert f"OPENAI_BASE_URL='{HERMES_ENDPOINT}'" in content
    assert f"OPENAI_MODEL='{HERMES_MODEL}'" in content
    assert "MC_AGENT_TOKEN=" in content
    assert "MC_BASE_URL=" in content
    # Hermes must NOT carry Anthropic auth in env file
    assert "ANTHROPIC_AUTH_TOKEN" not in content
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in content

    assert result["tmux_session"] == "hermes-worker"
    assert result["plist_loaded"] is True
    assert result["token"]  # one-time visible

    # DB transition
    await async_session.refresh(agent)
    assert agent.provision_status == "provisioned"
    assert agent.provisioned_at is not None
    assert agent.agent_token_hash  # token persisted as PBKDF2 hash


@pytest.mark.asyncio
async def test_home_host_env_var_respected(async_session, tmp_path, monkeypatch, fake_redis):
    """HOME_HOST=tmp_path → agent.env lands at tmp_path/.mc/agents/hermes/agent.env."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME_HOST", str(fake_home))

    proc = MagicMock(returncode=0, stdout="", stderr="")
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run",
        return_value=proc,
    ):
        from app.services.agent_bootstrap import bootstrap_hermes_agent

        agent = await _make_hermes_agent(async_session)
        runtime = await async_session.get(Runtime, agent.runtime_id)
        result = await bootstrap_hermes_agent(async_session, agent, runtime)

    expected = fake_home / ".mc" / "agents" / "hermes" / "agent.env"
    assert expected.exists()
    assert result["env_path"] == str(expected)
    # Config stays in ~/.mc/agents/hermes; the browsable task workspace is
    # ~/.mc/workspaces/hermes (fleet convention — shows up under Files).
    assert result["workspace_path"] == str(fake_home / ".mc" / "workspaces" / "hermes")
    assert (fake_home / ".mc" / "workspaces" / "hermes").is_dir()


@pytest.mark.asyncio
async def test_provision_idempotent(async_session, tmp_path, monkeypatch, fake_redis):
    """Calling bootstrap twice — second run regenerates env, tolerates 'already loaded'."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    proc_ok = MagicMock(returncode=0, stdout="", stderr="")
    # Second call: already loaded (rc=37 on macOS launchctl, or stderr message)
    proc_already = MagicMock(
        returncode=37,
        stdout="",
        stderr="Service already bootstrapped: gui/501/com.mc.hermes-bridge",
    )

    from app.services.agent_bootstrap import bootstrap_hermes_agent

    agent = await _make_hermes_agent(async_session)
    runtime = await async_session.get(Runtime, agent.runtime_id)

    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run",
        side_effect=[proc_ok, proc_already],
    ):
        first = await bootstrap_hermes_agent(async_session, agent, runtime)
        # New token generated each time; second call must succeed
        second = await bootstrap_hermes_agent(async_session, agent, runtime)

    assert first["plist_loaded"] is True
    assert first["plist_already"] is False
    assert second["plist_loaded"] is True
    assert second["plist_already"] is True
    # env_path stable across runs
    assert first["env_path"] == second["env_path"]
    # Tokens differ across runs (fresh per call)
    assert first["token"] != second["token"]


@pytest.mark.asyncio
async def test_launchctl_bootstrap_failure_rollback(async_session, tmp_path, monkeypatch, fake_redis):
    """Hard launchctl failure raises so caller can rollback provision_status."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    proc_fail = MagicMock(
        returncode=5,
        stdout="",
        stderr="Bootstrap failed: 5: Input/output error",
    )
    from app.services.agent_bootstrap import bootstrap_hermes_agent

    agent = await _make_hermes_agent(async_session)
    runtime = await async_session.get(Runtime, agent.runtime_id)

    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run",
        return_value=proc_fail,
    ):
        with pytest.raises(RuntimeError, match="launchctl bootstrap failed"):
            await bootstrap_hermes_agent(async_session, agent, runtime)

    # bootstrap_hermes_agent itself does not roll back — caller does.
    # But verify env file IS written (failure happens after env write,
    # which is fine: re-run will overwrite).
    env_path = tmp_path / ".mc" / "agents" / "hermes" / "agent.env"
    assert env_path.exists()


@pytest.mark.asyncio
async def test_bootstrap_auto_assigns_mc_dev_board_when_null(
    async_session, tmp_path, monkeypatch, fake_redis
):
    """Plan 25-07 T1: board_id IS NULL → bootstrap finds 'MC Development' → assigns."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.models.board import Board
    mc_dev = Board(name="MC Development", slug="mc-dev")
    async_session.add(mc_dev)
    await async_session.commit()
    await async_session.refresh(mc_dev)

    proc = MagicMock(returncode=0, stdout="", stderr="")
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run",
        return_value=proc,
    ):
        from app.services.agent_bootstrap import bootstrap_hermes_agent

        agent = await _make_hermes_agent(async_session)
        assert agent.board_id is None
        runtime = await async_session.get(Runtime, agent.runtime_id)

        await bootstrap_hermes_agent(async_session, agent, runtime)

    await async_session.refresh(agent)
    assert agent.board_id == mc_dev.id


@pytest.mark.asyncio
async def test_bootstrap_keeps_existing_board_id(
    async_session, tmp_path, monkeypatch, fake_redis
):
    """Plan 25-07 T1: board_id already set → bootstrap does NOT overwrite."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.models.board import Board
    mc_dev = Board(name="MC Development", slug="mc-dev")
    other = Board(name="OpenClaw Integration", slug="openclaw")
    async_session.add(mc_dev)
    async_session.add(other)
    await async_session.commit()
    await async_session.refresh(mc_dev)
    await async_session.refresh(other)

    proc = MagicMock(returncode=0, stdout="", stderr="")
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run",
        return_value=proc,
    ):
        from app.services.agent_bootstrap import bootstrap_hermes_agent

        agent = await _make_hermes_agent(async_session)
        agent.board_id = other.id
        async_session.add(agent)
        await async_session.commit()
        runtime = await async_session.get(Runtime, agent.runtime_id)

        await bootstrap_hermes_agent(async_session, agent, runtime)

    await async_session.refresh(agent)
    assert agent.board_id == other.id  # NOT overwritten


@pytest.mark.asyncio
async def test_provision_endpoint_dispatches_hermes_branch(
    auth_client, async_session, tmp_path, monkeypatch, fake_redis
):
    """POST /api/v1/agents/{id}/provision on Hermes-Agent → calls bootstrap_hermes_agent."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    agent = await _make_hermes_agent(async_session)

    fake_result = {
        "token": "test-token-once-visible",
        "env_path": str(tmp_path / ".mc" / "agents" / "hermes" / "agent.env"),
        "plist_loaded": True,
        "plist_already": False,
        "tmux_session": "hermes-worker",
        "workspace_path": str(tmp_path / ".mc" / "workspaces" / "hermes"),
    }

    # Dispatch now routes through HostHarnessAdapter.bootstrap(), which does its
    # own lazy `from app.services.agent_bootstrap import bootstrap_hermes_agent`
    # import at call time — patch at the source module so the adapter picks up
    # the mock regardless of which module imported the name.
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.bootstrap_hermes_agent",
        new=AsyncMock(return_value=fake_result),
    ) as mocked:
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/provision")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "provisioned"
    assert body["tmux_session"] == "hermes-worker"
    assert body["token"] == "test-token-once-visible"
    mocked.assert_awaited_once()
