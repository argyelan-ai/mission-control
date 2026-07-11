"""ADR-066 — Tests for grok host-side provisioning (Grok Build CLI as host harness).

Mirrors test_hermes_provisioning.py: env file carries only MC_* (no provider env),
tmux_session is the persistent grok TUI session (ADR-068), and the provision
endpoint routes through the HostHarnessAdapter registry to bootstrap_grok_agent.
"""
from __future__ import annotations

import stat
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


@contextmanager
def _patch_redis(fake_redis):
    async def _get():
        return fake_redis
    with patch("app.services.sse.get_redis", _get), \
         patch("app.redis_client.get_redis", _get):
        yield


def _make_grok_runtime() -> Runtime:
    return Runtime(
        slug="grok-cloud",
        display_name="Grok Build (xAI Cloud)",
        runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
        model_identifier="grok-4.5",
        enabled=True,
        single_instance=True,
    )


async def _make_grok_agent(session) -> Agent:
    runtime = _make_grok_runtime()
    session.add(runtime)
    await session.commit()
    await session.refresh(runtime)

    agent = Agent(
        id=uuid.uuid4(),
        name="Grok",
        agent_runtime="host",
        harness="grok",
        runtime_id=runtime.id,
        provision_status="local",
        workspace_path="/Users/testuser/.mc/agents/grok",
        model="grok-4.5",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_bootstrap_renders_env_file_mc_only(async_session, tmp_path, monkeypatch, fake_redis):
    """bootstrap_grok_agent writes agent.env mode 600 with MC_* keys and NO provider env."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run", return_value=proc,
    ):
        from app.services.agent_bootstrap import bootstrap_grok_agent
        agent = await _make_grok_agent(async_session)
        runtime = await async_session.get(Runtime, agent.runtime_id)
        result = await bootstrap_grok_agent(async_session, agent, runtime)

    env_path = tmp_path / ".mc" / "agents" / "grok" / "agent.env"
    assert env_path.exists()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    content = env_path.read_text()
    assert "MC_AGENT_TOKEN=" in content
    assert "MC_BASE_URL=" in content
    # Grok reads its provider from its own OAuth — no provider env leaks in.
    assert "OPENAI_BASE_URL" not in content
    assert "OPENAI_MODEL" not in content
    assert "ANTHROPIC" not in content

    assert result["tmux_session"] == "grok"  # ADR-068: persistent TUI (paste model)
    assert result["plist_loaded"] is True
    assert result["token"]

    await async_session.refresh(agent)
    assert agent.provision_status == "provisioned"
    assert agent.provisioned_at is not None
    assert agent.workspace_path == str(tmp_path / ".mc" / "workspaces" / "grok")


@pytest.mark.asyncio
async def test_grok_provision_idempotent(async_session, tmp_path, monkeypatch, fake_redis):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    proc_ok = MagicMock(returncode=0, stdout="", stderr="")
    proc_already = MagicMock(
        returncode=37, stdout="",
        stderr="Service already bootstrapped: gui/501/com.mc.grok-bridge",
    )
    from app.services.agent_bootstrap import bootstrap_grok_agent
    agent = await _make_grok_agent(async_session)
    runtime = await async_session.get(Runtime, agent.runtime_id)
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run", side_effect=[proc_ok, proc_already],
    ):
        first = await bootstrap_grok_agent(async_session, agent, runtime)
        second = await bootstrap_grok_agent(async_session, agent, runtime)
    assert first["plist_already"] is False
    assert second["plist_already"] is True
    assert first["env_path"] == second["env_path"]
    assert first["token"] != second["token"]


@pytest.mark.asyncio
async def test_grok_provision_endpoint_dispatches_adapter(
    auth_client, async_session, tmp_path, monkeypatch, fake_redis
):
    """POST /provision on a grok agent routes through the adapter to bootstrap_grok_agent."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    agent = await _make_grok_agent(async_session)

    fake_result = {
        "token": "grok-token-once",
        "env_path": str(tmp_path / ".mc" / "agents" / "grok" / "agent.env"),
        "plist_loaded": True,
        "plist_already": False,
        "tmux_session": "grok",
        "workspace_path": str(tmp_path / ".mc" / "workspaces" / "grok"),
    }
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.bootstrap_grok_agent",
        new=AsyncMock(return_value=fake_result),
    ) as mocked:
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/provision")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "provisioned"
    assert body["token"] == "grok-token-once"
    assert body["tmux_session"] == "grok"
    mocked.assert_awaited_once()


def test_agent_create_accepts_host_harnesses():
    """AgentCreate validator accepts host-only harnesses (grok, hermes) — not just
    cli-bridge ones — since a grok host agent can only set harness at create time."""
    from app.routers.agents import AgentCreate

    rid = str(uuid.uuid4())
    for h in ("grok", "hermes", "claude", "openclaude", "omp"):
        ac = AgentCreate(name="A", harness=h, agent_runtime="host", runtime_id=rid)
        assert ac.harness == h
    # Unknown harness still rejected.
    with pytest.raises(Exception):
        AgentCreate(name="A", harness="bogus", agent_runtime="host", runtime_id=rid)


@pytest.mark.asyncio
async def test_grok_provision_rejects_openai_runtime(
    auth_client, async_session, tmp_path, monkeypatch, fake_redis
):
    """A grok agent bound to an openai runtime is a clean 422 (protocol mismatch)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    rt = Runtime(slug="spark", display_name="Spark", runtime_type="vllm_docker",
                 endpoint="http://192.0.2.10:8000/v1", model_identifier="Qwen", enabled=True)
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(id=uuid.uuid4(), name="Grok", agent_runtime="host", harness="grok",
                  runtime_id=rt.id, provision_status="local")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    with _patch_redis(fake_redis):
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/provision")
    assert resp.status_code == 422, resp.text
