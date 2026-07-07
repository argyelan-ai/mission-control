import uuid
import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


def _mk_rt(session):
    rt = Runtime(slug="hermes-vllm", display_name="Hermes vLLM", runtime_type="hermes",
                 endpoint="http://192.0.2.10:8000/v1", model_identifier="nvidia/Qwen3.6", enabled=True)
    session.add(rt)
    return rt


@pytest.mark.asyncio
async def test_registry_lookup():
    from app.services.host_harness_adapter import get_adapter, HermesAdapter
    a = get_adapter("hermes")
    assert isinstance(a, HermesAdapter)
    assert a.protocol == "openai"
    assert get_adapter("openclaude") is None
    assert get_adapter(None) is None


@pytest.mark.asyncio
async def test_hermes_adapter_build_env_has_openai_no_anthropic(async_session):
    from app.services.host_harness_adapter import get_adapter
    rt = _mk_rt(async_session)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Hermes", role="developer", agent_runtime="host",
                  harness="hermes", runtime_id=rt.id)
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    env = await get_adapter("hermes").build_agent_env(agent, rt, "tok123", session=async_session)
    assert env["OPENAI_BASE_URL"] == "http://192.0.2.10:8000/v1"
    assert env["OPENAI_MODEL"] == "nvidia/Qwen3.6"
    assert env["MC_AGENT_TOKEN"] == "tok123"
    assert not any(k.startswith("ANTHROPIC_") for k in env)


@pytest.mark.asyncio
async def test_sync_host_agent_model_preserves_token(async_session, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "hermes"
    d.mkdir(parents=True)
    (d / "agent.env").write_text(
        "MC_AGENT_TOKEN='keepme'\nOPENAI_BASE_URL='http://old'\nOPENAI_MODEL='old'\n"
    )
    rt = _mk_rt(async_session)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Hermes", role="developer", agent_runtime="host",
                  harness="hermes", runtime_id=rt.id, slug="hermes")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.host_harness_adapter import sync_host_agent_model
    await sync_host_agent_model(agent, rt, session=async_session)

    env = (d / "agent.env").read_text()
    assert "MC_AGENT_TOKEN='keepme'" in env
    assert "http://192.0.2.10:8000/v1" in env
    assert "OPENAI_MODEL='nvidia/Qwen3.6'" in env
