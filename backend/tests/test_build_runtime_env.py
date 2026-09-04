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


def assert_no_provider_leak(env: dict) -> None:
    """The invariant for every claude/anthropic branch result.

    The claude branch may carry ANTHROPIC_MODEL (see
    ``assert_anthropic_model_pin``), but must NEVER carry OPENAI_* shim keys
    (that would point the claude binary at the wrong endpoint) and must never
    carry the Anthropic OAuth token — auth is resolved centrally in
    resolve_provider_credentials (ADR-056) so the bootstrap and .env paths
    share one source and can't drift. See
    tests/test_provider_credentials.py::test_anthropic_oauth.
    """
    leaked = [k for k in env if k.startswith("OPENAI_")]
    assert not leaked, f"OPENAI_* key(s) leaked into the claude branch: {leaked}"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env


def assert_anthropic_model_pin(env: dict, expected: str) -> None:
    """ANTHROPIC_MODEL must be exactly runtime.model_identifier — no rewriting."""
    assert env.get("ANTHROPIC_MODEL") == expected


@pytest.mark.asyncio
async def test_build_runtime_env_anthropic(async_session):
    """Anthropic runtime → ANTHROPIC_MODEL only, nothing else (ADR-056).

    Changed 2026-07-25 (model-sanitation): the claude branch used to return an
    EMPTY dict. Containerised claude got its model from settings.json, but HOST
    claude (boss-host) had no settings.json render and therefore had to pin its
    model by hand in start-claude.sh — a pin that could never follow a runtime
    switch. build_runtime_env now emits runtime.model_identifier as
    ANTHROPIC_MODEL so both worlds read one source of truth.

    The protection is unchanged and asserted explicitly: no OPENAI_* keys, no
    Anthropic OAuth token / API key / base URL.
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

    assert_no_provider_leak(env)
    assert_anthropic_model_pin(env, "claude-sonnet-4-6")
    assert set(env) == {"ANTHROPIC_MODEL"}


@pytest.mark.asyncio
async def test_build_runtime_env_anthropic_without_model_identifier_emits_no_pin(
    async_session,
):
    """model_identifier=None → NO ANTHROPIC_MODEL key at all.

    Deliberate: absent pin means the claude CLI uses its account default, which
    follows new releases on its own. That is strictly safer than writing an
    empty or stale value, and it is what makes "no pin" representable.
    """
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="anthropic-claude-unpinned",
        display_name="Claude (unpinned)",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier=None,
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert "ANTHROPIC_MODEL" not in env
    assert_no_provider_leak(env)
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
    """agent.harness="claude" bound to an openai-protocol runtime → env follows
    the HARNESS, i.e. the anthropic shape (ANTHROPIC_MODEL only, auth resolved
    elsewhere) and explicitly NOT OPENAI_BASE_URL/OPENAI_MODEL from the runtime.

    The model still comes from runtime.model_identifier — the harness decides
    the KEY NAME, the runtime row decides the VALUE."""
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

    assert_no_provider_leak(env)
    assert_anthropic_model_pin(env, "qwen3-coder-next")
    assert set(env) == {"ANTHROPIC_MODEL"}


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
    existing branch (anthropic / openclaude / omp).

    "pre-B3 behavior" for the anthropic branch means "no OPENAI_* shim and no
    auth token" — since 2026-07-25 that branch additionally carries
    ANTHROPIC_MODEL from runtime.model_identifier (see
    test_build_runtime_env_anthropic); the fallback path must produce the same
    shape as the explicit-harness path."""
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

    env_anthropic = await build_runtime_env(anthropic_rt, async_session, agent=agent)
    assert_no_provider_leak(env_anthropic)
    assert_anthropic_model_pin(env_anthropic, "claude-sonnet-4-6")
    assert set(env_anthropic) == {"ANTHROPIC_MODEL"}

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

    assert env["OMP_TURN_IDLE_TIMEOUT"] == "1800"


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

        assert env["OMP_TURN_IDLE_TIMEOUT"] == "1800", f"runtime_type={rtype} must get slow timeout"


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

    assert env["OMP_TURN_IDLE_TIMEOUT"] == "1800"


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


# ── omp context window from runtime row (entrypoint no longer hardcodes it) ──
#
# The omp-bridge entrypoint used to hardcode contextWindow: 262144 / maxTokens:
# 65536 in models.yml. After a recipe switch to a smaller model that stale
# window leaked through: omp sized turns to 262k and requested the full window
# as output, exceeding the served model's real cap (HTTP 400) and breaking
# multi-turn. build_runtime_env now sources the window from the runtime row.


@pytest.mark.asyncio
async def test_build_runtime_env_omp_context_window_from_runtime(async_session):
    """max_context_len on the runtime -> OMP_CONTEXT_WINDOW + a half-window,
    32k-capped OMP_MAX_TOKENS so omp's models.yml matches the served model."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="minimax-reap-omp",
        display_name="MiniMax REAP",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="MiniMax-M2.7-REAP-Spark",
        max_context_len=65536,
        enabled=True,
    )
    agent = Agent(name="ReapAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OMP_CONTEXT_WINDOW"] == "65536"
    # half the window (32768) is at the 32k cap -> 32768
    assert env["OMP_MAX_TOKENS"] == "32768"


@pytest.mark.asyncio
async def test_build_runtime_env_omp_max_tokens_capped_for_large_window(async_session):
    """A large window (Qwen 262k) still caps output at 32k, not half (131k),
    so a single turn can't eat the whole context."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-large-ctx",
        display_name="Qwen 262k",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="qwen3.6-35b",
        max_context_len=262144,
        enabled=True,
    )
    agent = Agent(name="QwenAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OMP_CONTEXT_WINDOW"] == "262144"
    assert env["OMP_MAX_TOKENS"] == "32768"


@pytest.mark.asyncio
async def test_build_runtime_env_omp_no_context_len_omits_keys(async_session):
    """max_context_len/preferred both unset -> keys omitted so the entrypoint's
    :-262144 / :-32768 defaults apply (backward compatible)."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="omp-no-ctx",
        display_name="No ctx",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="some-model",
        enabled=True,
    )
    agent = Agent(name="NoCtxAgent", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert "OMP_CONTEXT_WINDOW" not in env
    assert "OMP_MAX_TOKENS" not in env


# ── Per-runtime OMP_TASK_DEADLINE (03.09.2026) ─────────────────────────────
#
# The omp bridge's per-task wall clock (default 1 h since this change) killed
# a working 20-minute security audit on a local model at 1200 s. Local models
# are slower than cloud harnesses on the same task (28 min for a cloud
# harness) -> slow local runtimes get a 2 h deadline; cloud keeps the default.


@pytest.mark.asyncio
async def test_build_runtime_env_omp_slow_local_gets_long_task_deadline(async_session):
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="glm-vllm",
        display_name="GLM vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="glm-flash",
        enabled=True,
    )
    agent = Agent(name="SlowLocalAgent2", agent_runtime="cli-bridge", harness="omp")

    env = await build_runtime_env(rt, async_session, agent=agent)

    assert env["OMP_TASK_DEADLINE"] == "7200"


@pytest.mark.asyncio
async def test_build_runtime_env_omp_cloud_keeps_default_task_deadline(async_session):
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="omp-cloud-runtime-2",
        display_name="omp (cloud-hosted)",
        runtime_type="omp",
        endpoint="https://managed-omp-endpoint.example.com/v1",
        model_identifier="some-cloud-model",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert "OMP_TASK_DEADLINE" not in env
