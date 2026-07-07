"""Host-safe in-place runtime switch (ADR-060, Task 6).

A host agent that owns a HostHarnessAdapter (currently only Hermes) can switch
its LLM runtime *in place*: kill → re-render agent.env (OPENAI_* only, token
preserved) → restart the single host session. Because the reload is strictly
sequential there is never a second parallel instance, so the ``single_instance``
hard-block must NOT fire for this path — while it must still block binding a
*second* / adapter-less agent onto a single_instance runtime (regression
coverage lives in ``test_agent_runtime_switch_single_instance.py``).

Adaptation note (vs the task-6 brief sketch):
  - The brief's ``SwitchResult(ok=..., agent_id=..., new_runtime_id=...)`` does
    not match the real dataclass — we assert on the real fields
    (``new_runtime``/``old_runtime`` summaries, ``harness``) instead.
  - The incompatible-protocol runtime uses a realistic ``anthropic-claude-*``
    slug: ``harness_compat.runtime_protocol`` classifies the anthropic wire
    protocol via that slug prefix (the seed convention for Claude OAuth
    runtimes), which is what makes the existing compat guard reject it. The
    brief's ``claude-cloud`` slug would have been misclassified as openai.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services import agent_runtime_switch as sw
from app.services import sse as sse_mod


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


async def _mk_runtime(session, *, slug, rtype="openai_compatible", single=False,
                      endpoint="https://ollama.com/v1", model="kimi-k2.6"):
    rt = Runtime(slug=slug, display_name=slug, runtime_type=rtype,
                 endpoint=endpoint, model_identifier=model, enabled=True)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    rt.single_instance = single
    return rt


async def _mk_hermes(session, rt):
    agent = Agent(name="Hermes", role="developer", agent_runtime="host",
                  harness="hermes", runtime_id=rt.id, slug="hermes")
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_host_inplace_switch_updates_binding_and_preserves_token(
    async_session, fake_redis, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "hermes"
    d.mkdir(parents=True)
    (d / "agent.env").write_text(
        "MC_AGENT_TOKEN='keep'\nOPENAI_BASE_URL='http://old'\nOPENAI_MODEL='old'\n"
    )
    spark = await _mk_runtime(async_session, slug="hermes-vllm", rtype="hermes",
                              single=True, endpoint="http://192.0.2.10:8000/v1", model="qwen")
    ollama = await _mk_runtime(async_session, slug="ollama-cloud")
    agent = await _mk_hermes(async_session, spark)

    with (
        patch.object(sw, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.host_harness_adapter.HermesAdapter.reload",
              new=AsyncMock(return_value={"ok": True})) as mock_reload,
    ):
        result = await sw.switch_agent_runtime(async_session, agent, ollama.id)

    await async_session.refresh(agent)
    assert agent.runtime_id == ollama.id
    env = (d / "agent.env").read_text()
    assert "MC_AGENT_TOKEN='keep'" in env
    assert "https://ollama.com/v1" in env
    mock_reload.assert_awaited_once()
    # Real SwitchResult shape (not the brief's ok=/agent_id= sketch).
    assert result.dry_run is False
    assert result.new_runtime["slug"] == "ollama-cloud"
    assert result.harness == "hermes"


@pytest.mark.asyncio
async def test_host_switch_incompatible_protocol_rejected(async_session, fake_redis):
    spark = await _mk_runtime(async_session, slug="hermes-vllm", rtype="hermes",
                              endpoint="http://192.0.2.10:8000/v1", model="qwen")
    # Realistic anthropic slug so runtime_protocol() classifies it as the
    # anthropic wire protocol → incompatible with the openai hermes adapter.
    anthropic_rt = await _mk_runtime(async_session, slug="anthropic-claude-cloud", rtype="cloud",
                                     endpoint="https://api.anthropic.com", model="claude-opus-4-8")
    agent = await _mk_hermes(async_session, spark)
    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)):
        with pytest.raises((sw.RuntimeIncompatibleError, sw.AgentNotSwitchableError)):
            await sw.switch_agent_runtime(async_session, agent, anthropic_rt.id)


@pytest.mark.asyncio
async def test_host_switch_busy_raises_409(async_session, fake_redis):
    spark = await _mk_runtime(async_session, slug="hermes-vllm", rtype="hermes",
                              endpoint="http://192.0.2.10:8000/v1", model="qwen")
    ollama = await _mk_runtime(async_session, slug="ollama-cloud")
    agent = await _mk_hermes(async_session, spark)
    agent.current_task_id = uuid.uuid4()
    await async_session.commit()
    with patch.object(sw, "get_redis", _fake_get_redis(fake_redis)):
        with pytest.raises(sw.AgentBusyError):
            await sw.switch_agent_runtime(async_session, agent, ollama.id)
