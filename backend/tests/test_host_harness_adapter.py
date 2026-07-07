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


def test_env_value_roundtrip_is_idempotent():
    """read(write(x)) == x for every value — including values with quotes.

    Regression: the old reader did `.strip("'")` which left `'"'"'` sequences
    intact, so any quoted value re-escaped and grew ~3× per model-drift sync.
    A 64-char agent token ballooned to 13 KB and stopped authenticating, which
    silently fell the agent's comments back to the operator endpoint ('👤 Du').
    """
    from app.services.agent_bootstrap import _format_env_file, _unquote_env_value

    for val in [
        "2e3f61e44cb83a5e4e38dc04509e6ce9cd8bcf0c46788d494dbaa4f3bec1017f",  # clean hex
        "has'quote",
        "many''quotes''here",
        "http://100.67.20.66:8000/v1",
        "",
    ]:
        line = _format_env_file({"K": val})
        _, _, raw = line.strip().partition("=")
        assert _unquote_env_value(raw) == val


@pytest.mark.asyncio
async def test_sync_host_agent_model_token_stable_across_repeated_syncs(
    async_session, tmp_path, monkeypatch
):
    """Repeated model-drift syncs must not grow the token line (13 KB bug)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "hermes"
    d.mkdir(parents=True)
    token = "2e3f61e44cb83a5e4e38dc04509e6ce9cd8bcf0c46788d494dbaa4f3bec1017f"
    (d / "agent.env").write_text(f"MC_AGENT_TOKEN='{token}'\nOPENAI_MODEL='old'\n")

    rt = _mk_rt(async_session)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Hermes", role="developer", agent_runtime="host",
                  harness="hermes", runtime_id=rt.id, slug="hermes")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.host_harness_adapter import sync_host_agent_model
    for _ in range(6):
        await sync_host_agent_model(agent, rt, session=async_session)

    env = (d / "agent.env").read_text()
    assert f"MC_AGENT_TOKEN='{token}'" in env
    # The full file stays tiny — no exponential quote accumulation.
    assert len(env) < 400
