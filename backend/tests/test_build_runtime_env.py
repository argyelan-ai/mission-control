"""Phase 16 — Tests for build_runtime_env helper.

D-14: Anthropic runtime → CLAUDE_CODE_OAUTH_TOKEN, NO OPENAI_*-keys.
D-15: openclaude/lmstudio/vllm/openai_compatible/unsloth → OPENAI_BASE_URL + OPENAI_MODEL.
D-16: ollama-cloud → OPENAI shim path (slug does not start with anthropic-claude-).
D-17: Helper extracted from internal.py — testable.

B3 (Workstream W1-C, ADR-056 follow-up): harness-first resolution —
agent.harness (if set) decides the branch, derive_harness(runtime) is the
fallback for legacy NULL-harness rows. See tests below the D-14..D-17 block.
"""
import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime


@pytest.mark.asyncio
async def test_build_runtime_env_anthropic(async_session):
    """Anthropic runtime → empty dict here (ADR-056).

    Provider auth (CLAUDE_CODE_OAUTH_TOKEN) moved ENTIRELY into
    resolve_provider_credentials so the bootstrap + .env paths share one
    source and can't drift. build_runtime_env no longer loads the OAuth
    token; it returns empty for anthropic runtimes (no OPENAI_* keys, no
    BASE_URL/MODEL). See tests/test_provider_credentials.py::test_anthropic_oauth.
    """
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-sonnet-4-6",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env == {}


@pytest.mark.asyncio
async def test_build_runtime_env_openai_shim(async_session):
    """Non-anthropic runtime → OPENAI_BASE_URL + OPENAI_MODEL (D-15)."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-coder-lms",
        display_name="Qwen Coder",
        runtime_type="lmstudio",
        endpoint="http://192.0.2.10:1234/v1",
        model_identifier="qwen3-coder-next",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == "http://192.0.2.10:1234/v1"
    assert env["OPENAI_MODEL"] == "qwen3-coder-next"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.asyncio
async def test_build_runtime_env_ollama_cloud_uses_shim(async_session):
    """ollama-cloud (slug does NOT start with anthropic-claude-) → OPENAI shim (D-16)."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="ollama-cloud",
        display_name="Ollama Cloud",
        runtime_type="openai_compatible",
        endpoint="https://ollama.com/v1",
        model_identifier="glm-5.1:cloud",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == "https://ollama.com/v1"
    assert env["OPENAI_MODEL"] == "glm-5.1:cloud"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.asyncio
async def test_build_runtime_env_disabled_or_none_returns_empty(async_session):
    """runtime=None or enabled=False → empty dict."""
    from app.routers.internal import build_runtime_env

    env_none = await build_runtime_env(None, async_session)
    assert env_none == {}

    rt_disabled = Runtime(
        slug="disabled-rt",
        display_name="Disabled",
        runtime_type="lmstudio",
        endpoint="http://example.com/v1",
        model_identifier="some-model",
        enabled=False,
    )
    env_disabled = await build_runtime_env(rt_disabled, async_session)
    assert env_disabled == {}


@pytest.mark.asyncio
async def test_build_runtime_env_no_model_identifier(async_session):
    """No model_identifier (NULL) → OPENAI_BASE_URL set, OPENAI_MODEL missing."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="incomplete-rt",
        display_name="Incomplete",
        runtime_type="lmstudio",
        endpoint="http://localhost:9000/v1",
        model_identifier=None,
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == "http://localhost:9000/v1"
    assert "OPENAI_MODEL" not in env


# ── B3: harness-first resolution ────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_runtime_env_agent_harness_openclaude_wins_over_anthropic_runtime(async_session):
    """agent.harness="openclaude" bound to an anthropic-typed runtime → env
    follows the HARNESS (OPENAI_*), not the runtime's own protocol. This is
    an intentionally mismatched combo (compatibility validation lives
    elsewhere) — build_runtime_env must not silently paper over it."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-sonnet-4-6",
        enabled=True,
    )
    agent = Agent(name="Mismatched", agent_runtime="cli-bridge", harness="openclaude")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OPENAI_BASE_URL"] == "https://api.anthropic.com"
    assert env["OPENAI_MODEL"] == "claude-sonnet-4-6"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.asyncio
async def test_build_runtime_env_agent_harness_claude_wins_over_openai_runtime(async_session):
    """agent.harness="claude" bound to an openai-protocol runtime → env
    follows the HARNESS (empty — Anthropic auth resolved elsewhere), NOT
    OPENAI_BASE_URL/MODEL from the runtime."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-coder-lms",
        display_name="Qwen Coder",
        runtime_type="lmstudio",
        endpoint="http://192.0.2.10:1234/v1",
        model_identifier="qwen3-coder-next",
        enabled=True,
    )
    agent = Agent(name="Mismatched2", agent_runtime="cli-bridge", harness="claude")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env == {}


@pytest.mark.asyncio
async def test_build_runtime_env_agent_harness_omp_wins(async_session):
    """agent.harness="omp" on a plain openai_compatible runtime_type (not
    literally "omp") → still gets the omp env shape (same two keys as
    openclaude here, but exercises the harness branch explicitly)."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-omp-alias",
        display_name="Qwen via omp",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.20:8000/v1",
        model_identifier="qwen3.6-35b",
        enabled=True,
    )
    agent = Agent(name="OmpAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OPENAI_BASE_URL"] == "http://192.0.2.20:8000/v1"
    assert env["OPENAI_MODEL"] == "qwen3.6-35b"


@pytest.mark.asyncio
async def test_build_runtime_env_null_harness_falls_back_to_runtime_type(async_session):
    """Regression guard: agent.harness=None (legacy row) → falls back to
    derive_harness(runtime), reproducing the exact pre-B3 behavior for every
    existing branch (anthropic / openclaude / omp)."""
    from app.routers.internal import build_runtime_env

    anthropic_rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-sonnet-4-6",
        enabled=True,
    )
    openai_rt = Runtime(
        slug="qwen-coder-lms",
        display_name="Qwen Coder",
        runtime_type="lmstudio",
        endpoint="http://192.0.2.10:1234/v1",
        model_identifier="qwen3-coder-next",
        enabled=True,
    )
    omp_rt = Runtime(
        slug="omp-runtime",
        display_name="omp",
        runtime_type="omp",
        endpoint="http://192.0.2.30:8000/v1",
        model_identifier="qwen3.6-35b",
        enabled=True,
    )
    agent = Agent(name="LegacyNullHarness", agent_runtime="cli-bridge", harness=None)

    assert await build_runtime_env(anthropic_rt, async_session, agent=agent) == {}

    env_openai = await build_runtime_env(openai_rt, async_session, agent=agent)
    assert env_openai["OPENAI_BASE_URL"] == "http://192.0.2.10:1234/v1"
    assert env_openai["OPENAI_MODEL"] == "qwen3-coder-next"

    env_omp = await build_runtime_env(omp_rt, async_session, agent=agent)
    assert env_omp["OPENAI_BASE_URL"] == "http://192.0.2.30:8000/v1"
    assert env_omp["OPENAI_MODEL"] == "qwen3.6-35b"


@pytest.mark.asyncio
async def test_build_runtime_env_no_agent_arg_falls_back_to_runtime_type(async_session):
    """Regression guard: callers that don't pass `agent` at all (e.g. the
    Hermes .env render path) keep working exactly as before via
    derive_harness(runtime)."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-coder-lms",
        display_name="Qwen Coder",
        runtime_type="lmstudio",
        endpoint="http://192.0.2.10:1234/v1",
        model_identifier="qwen3-coder-next",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == "http://192.0.2.10:1234/v1"
    assert env["OPENAI_MODEL"] == "qwen3-coder-next"


# ── Fix 4 (W2-A, audit item): per-runtime OMP_TURN_IDLE_TIMEOUT ─────────
#
# Slow local runtimes (vllm_docker, lmstudio, unsloth, openai_compatible
# on local hosts) need 600s instead of the omp bridge's 300s default —
# a long write on a self-hosted model routinely exceeds 300s and gets
# SIGKILLed mid-write. Cloud/fast runtimes keep the tighter default
# (no override -> bridge's own OMP_TURN_IDLE_TIMEOUT default applies).


@pytest.mark.asyncio
async def test_build_runtime_env_omp_vllm_docker_gets_slow_timeout(async_session):
    """runtime_type=vllm_docker (self-hosted, no exception) -> 600s override."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-vllm",
        display_name="Qwen vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="qwen3.6-35b",
        enabled=True,
    )
    agent = Agent(name="SlowLocalAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OMP_TURN_IDLE_TIMEOUT"] == "600"


@pytest.mark.asyncio
async def test_build_runtime_env_omp_lmstudio_and_unsloth_get_slow_timeout(async_session):
    """runtime_type=lmstudio / unsloth -> 600s override (both self-hosted-only types)."""
    from app.routers.internal import build_runtime_env

    for rtype in ("lmstudio", "unsloth"):
        rt = Runtime(
            slug=f"local-{rtype}",
            display_name=f"Local {rtype}",
            runtime_type=rtype,
            endpoint="http://192.0.2.10:1234/v1",
            model_identifier="some-model",
            enabled=True,
        )
        agent = Agent(name=f"Agent-{rtype}", agent_runtime="cli-bridge", harness="omp")

        env = await build_runtime_env(rt, async_session, agent=agent)

        assert env["OMP_TURN_IDLE_TIMEOUT"] == "600", f"runtime_type={rtype} must get slow timeout"


@pytest.mark.asyncio
async def test_build_runtime_env_omp_openai_compatible_local_host_gets_slow_timeout(async_session):
    """runtime_type=openai_compatible BOUND to a physical host (host_id set)
    -> genuinely local/self-hosted -> 600s override."""
    import uuid

    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="local-openai-compatible",
        display_name="Local OpenAI-compatible",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.20:8000/v1",
        model_identifier="qwen3.6-35b",
        enabled=True,
        host_id=uuid.uuid4(),
    )
    agent = Agent(name="LocalOpenAICompatAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OMP_TURN_IDLE_TIMEOUT"] == "600"


@pytest.mark.asyncio
async def test_build_runtime_env_omp_cloud_runtime_untouched(async_session):
    """runtime_type=omp itself but pointed at a cloud/HTTP-only endpoint
    (no host_id, e.g. a hosted API reachable purely by URL) does NOT get
    the slow-local override -- default omp bridge timeout (300s) applies,
    key is simply absent."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="omp-cloud-runtime",
        display_name="omp (cloud-hosted)",
        runtime_type="omp",
        endpoint="https://managed-omp-endpoint.example.com/v1",
        model_identifier="some-cloud-model",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert "OMP_TURN_IDLE_TIMEOUT" not in env


@pytest.mark.asyncio
async def test_build_runtime_env_omp_openai_compatible_cloud_untouched(async_session):
    """runtime_type=openai_compatible with NO host_id (managed cloud API,
    e.g. Ollama Cloud) even under an explicit omp harness override -> stays
    untouched, no slow-local timeout injected."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="ollama-cloud-omp",
        display_name="Ollama Cloud (omp harness)",
        runtime_type="openai_compatible",
        endpoint="https://ollama.com/v1",
        model_identifier="glm-5.1:cloud",
        enabled=True,
    )
    agent = Agent(name="CloudOmpAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert "OMP_TURN_IDLE_TIMEOUT" not in env


@pytest.mark.asyncio
async def test_build_runtime_env_anthropic_untouched_by_omp_timeout(async_session):
    """Sanity: anthropic/claude-harness runtimes never get OMP_TURN_IDLE_TIMEOUT
    at all -- the key is omp-bridge-specific."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="anthropic-claude-sonnet-2",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-sonnet-4-6",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert "OMP_TURN_IDLE_TIMEOUT" not in env
