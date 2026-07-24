"""Kimi Code CLI als Host-Harness (2026-07-24, boss-host pattern).

Mirrors test_grok_provisioning.py: agent.env carries only MC_* control-plane
vars (OAuth lives as FILES in the per-agent KIMI_CODE_HOME — no provider env,
no token env), tmux_session is 'kimi-host', and the provision endpoint routes
through the HostHarnessAdapter registry to bootstrap_kimi_agent.
"""
from __future__ import annotations

import stat
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

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


def _make_kimi_runtime() -> Runtime:
    return Runtime(
        slug="kimi-cloud",
        display_name="Kimi Code (Moonshot Cloud)",
        runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
        model_identifier="kimi-code/k3",
        enabled=True,
        single_instance=True,
    )


async def _make_kimi_agent(async_session) -> Agent:
    runtime = _make_kimi_runtime()
    async_session.add(runtime)
    await async_session.commit()
    await async_session.refresh(runtime)

    agent = Agent(
        id=uuid.uuid4(),
        name="Kimi",
        agent_runtime="host",
        harness="kimi",
        runtime_id=runtime.id,
        provision_status="local",
        workspace_path="/Users/testuser/.mc/agents/kimi",
        model="kimi-code/k3",
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_bootstrap_renders_env_file_mc_only(async_session, tmp_path, monkeypatch, fake_redis):
    """bootstrap_kimi_agent writes agent.env mode 600 with MC_* keys and NO provider env."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with _patch_redis(fake_redis), patch(
        "app.services.agent_bootstrap.subprocess.run", return_value=proc,
    ):
        from app.services.agent_bootstrap import bootstrap_kimi_agent
        agent = await _make_kimi_agent(async_session)
        runtime = await async_session.get(Runtime, agent.runtime_id)
        result = await bootstrap_kimi_agent(async_session, agent, runtime)

    env_path = tmp_path / ".mc" / "agents" / "kimi" / "agent.env"
    assert env_path.exists()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    content = env_path.read_text()
    assert "MC_AGENT_TOKEN=" in content
    # Nudge+Pull-Env-Kontrakt: mc-cli/poll.sh lesen MC_API_URL.
    assert "MC_API_URL=" in content
    # Kimi liest seinen Provider aus den eigenen OAuth-Dateien — kein
    # Provider-Env, kein Token-Env.
    assert "OPENAI_BASE_URL" not in content
    assert "ANTHROPIC" not in content
    assert "KIMI_API_KEY" not in content

    assert result["tmux_session"] == "kimi-host"
    assert result["plist_loaded"] is True
    assert result["token"]

    # KIMI_CODE_HOME-Struktur angelegt (credentials bleibt leer bis /login).
    assert (tmp_path / ".mc" / "agents" / "kimi" / "kimi-config" / "credentials").is_dir()
    assert (tmp_path / ".mc" / "agents" / "kimi" / "kimi-config" / "oauth").is_dir()

    await async_session.refresh(agent)
    assert agent.provision_status == "provisioned"
    assert agent.workspace_path == str(tmp_path / ".mc" / "workspaces" / "kimi")


@pytest.mark.asyncio
async def test_kimi_adapter_registered_as_singleton():
    from app.services.host_harness_adapter import HOST_ADAPTERS, get_adapter

    adapter = get_adapter("kimi")
    assert adapter is HOST_ADAPTERS["kimi"]
    assert adapter.protocol == "kimi"
    assert adapter.singleton_slug == "kimi"


@pytest.mark.asyncio
async def test_kimi_singleton_guard_rejects_foreign_slug(
    auth_client, async_session, tmp_path, monkeypatch, fake_redis
):
    """harness=kimi auf einem Agent mit anderem Slug → 422 (Singleton-Guard),
    bevor irgendein File angefasst wird (Muster hermes-Clobber 2026-07-12)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    runtime = _make_kimi_runtime()
    async_session.add(runtime)
    await async_session.commit()
    await async_session.refresh(runtime)

    agent = Agent(
        id=uuid.uuid4(),
        name="Dev",
        agent_runtime="host",
        harness="kimi",
        runtime_id=runtime.id,
        provision_status="local",
        model="kimi-code/k3",
    )
    async_session.add(agent)
    await async_session.commit()

    with _patch_redis(fake_redis):
        resp = await auth_client.post(f"/api/v1/agents/{agent.id}/provision")
    assert resp.status_code == 422
    assert "Singleton" in resp.json()["detail"]
    assert not (tmp_path / ".mc" / "agents" / "kimi" / "agent.env").exists()
