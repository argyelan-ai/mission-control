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
    # "openclaw" is the retired Phase-29 gateway runtime (ADR-039): a value
    # that can never gain an adapter. It replaced "openclaude" here on
    # 2026-07-28, when openclaude/omp were registered so every CLI type exists
    # as a host agent too (see test_host_harness_catalog.py's invariant).
    assert get_adapter("openclaw") is None
    assert get_adapter(None) is None


@pytest.mark.asyncio
async def test_grok_registry_lookup_and_protocol():
    """ADR-066: grok is a registered host adapter with the fixed 'grok' protocol."""
    from app.services.host_harness_adapter import get_adapter, GrokAdapter
    a = get_adapter("grok")
    assert isinstance(a, GrokAdapter)
    assert a.harness == "grok"
    assert a.protocol == "grok"


@pytest.mark.asyncio
async def test_grok_adapter_build_env_has_no_provider_env(async_session):
    """grok reads its provider from its own xAI OAuth — agent.env carries only MC_*."""
    from app.services.host_harness_adapter import get_adapter
    rt = Runtime(slug="grok-cloud", display_name="Grok Build", runtime_type="grok",
                 endpoint="https://cli-chat-proxy.grok.com", model_identifier="grok-4.5",
                 enabled=True)
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Grok", role="developer", agent_runtime="host",
                  harness="grok", runtime_id=rt.id)
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    env = await get_adapter("grok").build_agent_env(agent, rt, "tok-xyz", session=async_session)
    assert env["MC_AGENT_TOKEN"] == "tok-xyz"
    assert "MC_BASE_URL" in env
    assert not any(k.startswith("OPENAI_") for k in env)
    assert not any(k.startswith("ANTHROPIC_") for k in env)


@pytest.mark.asyncio
async def test_grok_compat_matrix():
    """A grok runtime only matches the grok harness; openai/anthropic runtimes 422."""
    from app.services.harness_compat import is_compatible, runtime_protocol
    grok_rt = Runtime(slug="grok-cloud", runtime_type="grok",
                      endpoint="https://cli-chat-proxy.grok.com", enabled=True)
    openai_rt = Runtime(slug="spark", runtime_type="vllm_docker",
                        endpoint="http://x/v1", enabled=True)
    assert runtime_protocol(grok_rt) == "grok"
    assert is_compatible("grok", grok_rt) is True
    assert is_compatible("grok", openai_rt) is False   # openai runtime, grok harness
    assert is_compatible("hermes", grok_rt) is False   # grok runtime, openai harness
    assert is_compatible("omp", grok_rt) is False


@pytest.mark.asyncio
async def test_sync_host_agent_model_skips_grok(async_session, tmp_path, monkeypatch):
    """sync must NOT inject OPENAI_* into a grok agent.env (protocol-fixed, ADR-066)."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "grok"
    d.mkdir(parents=True)
    (d / "agent.env").write_text("MC_AGENT_TOKEN='keepme'\nMC_BASE_URL='http://backend'\n")
    rt = Runtime(slug="grok-cloud", display_name="Grok Build", runtime_type="grok",
                 endpoint="https://cli-chat-proxy.grok.com", model_identifier="grok-4.5",
                 enabled=True)
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Grok", role="developer", agent_runtime="host",
                  harness="grok", runtime_id=rt.id, slug="grok")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.host_harness_adapter import sync_host_agent_model
    await sync_host_agent_model(agent, rt, session=async_session)

    env = (d / "agent.env").read_text()
    assert "MC_AGENT_TOKEN='keepme'" in env
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_MODEL" not in env


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


@pytest.mark.asyncio
async def test_sync_host_agent_model_skips_kimi(async_session, tmp_path, monkeypatch):
    """kimi is protocol-fixed too — no provider model env may be injected."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "kimi"
    d.mkdir(parents=True)
    (d / "agent.env").write_text("MC_AGENT_TOKEN='keepme'\n")
    rt = Runtime(slug="kimi-cloud", display_name="Kimi", runtime_type="kimi",
                 endpoint="https://api.kimi.com/coding/v1",
                 model_identifier="kimi-k2.7", enabled=True)
    async_session.add(rt)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Kimi", role="developer", agent_runtime="host",
                  harness="kimi", runtime_id=rt.id, slug="kimi")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.host_harness_adapter import sync_host_agent_model
    await sync_host_agent_model(agent, rt, session=async_session)

    env = (d / "agent.env").read_text()
    assert "MC_AGENT_TOKEN='keepme'" in env
    assert "OPENAI_MODEL" not in env
    assert "ANTHROPIC_MODEL" not in env


# ── harness "claude" / boss-host (model sanitation 2026-07-25) ──────────────


def _mk_anthropic_rt(session, model="claude-test-model-1"):
    rt = Runtime(
        slug="anthropic-claude-oauth",
        display_name="Claude OAuth",
        runtime_type="anthropic_cloud",
        endpoint="https://api.anthropic.com",
        model_identifier=model,
        enabled=True,
    )
    session.add(rt)
    return rt


@pytest.mark.asyncio
async def test_claude_registry_lookup_and_protocol():
    """Boss' harness must own an adapter — that is the gate every propagation
    path checks (`get_adapter(...) is not None`)."""
    from app.services.host_harness_adapter import get_adapter, ClaudeHostAdapter

    a = get_adapter("claude")
    assert isinstance(a, ClaudeHostAdapter)
    assert a.harness == "claude"
    assert a.protocol == "anthropic"
    # NOT a singleton: the wizard stages arbitrary claude host agents.
    assert a.singleton_slug is None
    # No bespoke bootstrap — routers/agents.py must keep using the generic
    # host_provisioning staging path for claude host agents.
    assert a.supports_bootstrap is False
    with pytest.raises(NotImplementedError):
        await a.bootstrap(None, None, None)


@pytest.mark.asyncio
async def test_other_host_adapters_still_support_bootstrap():
    from app.services.host_harness_adapter import get_adapter

    for harness in ("hermes", "grok", "kimi"):
        assert get_adapter(harness).supports_bootstrap is True


@pytest.mark.asyncio
async def test_claude_env_dir_maps_legacy_boss_slug():
    """Boss' agent.env lives in ~/.mc/agents/boss-host/, not .../boss/."""
    from app.services.host_harness_adapter import get_adapter

    adapter = get_adapter("claude")
    assert adapter.env_dir(Agent(name="Boss", role="lead", slug="boss")) == "boss-host"
    # Any other claude host agent uses its own slug unchanged.
    assert adapter.env_dir(Agent(name="Alice", role="dev", slug="alice")) == "alice"


@pytest.mark.asyncio
async def test_sync_host_agent_model_writes_anthropic_model_for_boss(
    async_session, tmp_path, monkeypatch
):
    """The whole point: runtime.model_identifier lands in boss-host's agent.env
    as ANTHROPIC_MODEL, while MC_AGENT_TOKEN and other keys survive."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "boss-host"
    d.mkdir(parents=True)
    (d / "agent.env").write_text(
        "MC_AGENT_TOKEN='keepme'\n"
        "MC_API_URL='http://backend:8000'\n"
        "MC_AGENT_NAME='Boss'\n"
    )

    rt = _mk_anthropic_rt(async_session)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Boss", role="lead", agent_runtime="host",
                  harness="claude", runtime_id=rt.id, slug="boss")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.host_harness_adapter import sync_host_agent_model
    await sync_host_agent_model(agent, rt, session=async_session)

    env = (d / "agent.env").read_text()
    assert "ANTHROPIC_MODEL='claude-test-model-1'" in env
    assert "MC_AGENT_TOKEN='keepme'" in env
    assert "MC_API_URL='http://backend:8000'" in env
    assert "MC_AGENT_NAME='Boss'" in env
    # anthropic harness gets no OpenAI provider env.
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_MODEL" not in env
    # The slug-named directory must NOT be created — nothing on the host reads it.
    assert not (tmp_path / ".mc" / "agents" / "boss").exists()


@pytest.mark.asyncio
async def test_sync_host_agent_model_without_model_identifier_writes_no_pin(
    async_session, tmp_path, monkeypatch
):
    """No model_identifier → no ANTHROPIC_MODEL. start-claude.sh then lets the
    CLI use its account default instead of a stale pin."""
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    d = tmp_path / ".mc" / "agents" / "boss-host"
    d.mkdir(parents=True)
    (d / "agent.env").write_text("MC_AGENT_TOKEN='keepme'\n")

    rt = _mk_anthropic_rt(async_session, model=None)
    await async_session.commit()
    await async_session.refresh(rt)
    agent = Agent(name="Boss", role="lead", agent_runtime="host",
                  harness="claude", runtime_id=rt.id, slug="boss")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.host_harness_adapter import sync_host_agent_model
    await sync_host_agent_model(agent, rt, session=async_session)

    env = (d / "agent.env").read_text()
    assert "ANTHROPIC_MODEL" not in env
    assert "MC_AGENT_TOKEN='keepme'" in env


@pytest.mark.asyncio
async def test_marked_for_sync_covers_host_agents_with_an_adapter_only(
    async_session,
):
    """mark_agents_for_sync flags host agents exactly when their harness has an
    adapter — the registry is the gate.

    Rewritten 2026-07-28: this used to read "claude yes, openclaude/omp no".
    openclaude/omp are registered now, and being flagged is CORRECT for them —
    they are openai-protocol hosts whose agent.env must follow a model change
    just like Boss's does. The negative case therefore moved to a harness that
    genuinely has no adapter ("openclaw", the retired Phase-29 runtime).
    """
    from app.services.runtime_propagation import mark_agents_for_sync

    rt = _mk_anthropic_rt(async_session)
    await async_session.commit()
    await async_session.refresh(rt)
    boss = Agent(name="Boss", role="lead", agent_runtime="host",
                 harness="claude", runtime_id=rt.id, slug="boss")
    generic = Agent(name="Generic", role="dev", agent_runtime="host",
                    harness="openclaude", runtime_id=rt.id, slug="generic")
    adapterless = Agent(name="Legacy", role="dev", agent_runtime="host",
                        harness="openclaw", runtime_id=rt.id, slug="legacy")
    async_session.add(boss)
    async_session.add(generic)
    async_session.add(adapterless)
    await async_session.commit()

    flagged = await mark_agents_for_sync(async_session, rt)

    await async_session.refresh(boss)
    await async_session.refresh(generic)
    await async_session.refresh(adapterless)
    assert flagged == 2
    assert boss.pending_runtime_sync is True
    assert generic.pending_runtime_sync is True
    assert adapterless.pending_runtime_sync is not True


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
        "http://100.100.200.50:8000/v1",
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
    # deliberately low-entropy dummy — a realistic hex token trips the gitleaks CI gate
    token = "aa11" * 16
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
