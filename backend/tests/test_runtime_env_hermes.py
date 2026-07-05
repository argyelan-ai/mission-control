"""Phase 24 — Tests for build_runtime_env() Hermes branch.

HERM-04: Hermes runtime renders OPENAI_BASE_URL + OPENAI_MODEL, no Anthropic auth.
ADR-029: explicit `runtime_type == "hermes"` branch in build_runtime_env so Phase 25
extension (HERMES_HOME, HERMES_PROFILE etc.) has a hook point.

Tests:
  1. Hermes runtime → OPENAI_BASE_URL set to runtime.endpoint
  2. Hermes runtime → no ANTHROPIC_AUTH_TOKEN, no CLAUDE_CODE_OAUTH_TOKEN
  3. Anthropic runtime → CLAUDE_CODE_OAUTH_TOKEN (regression)
  4. vllm_docker runtime → OPENAI_BASE_URL + OPENAI_MODEL (regression)
  5. None / disabled → empty dict (existing guard)
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime


HERMES_ENDPOINT = "http://192.0.2.10:8000/v1"
HERMES_MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"


@pytest.mark.asyncio
async def test_build_runtime_env_hermes_sets_openai_keys(async_session):
    """Hermes runtime → OPENAI_BASE_URL + OPENAI_MODEL from runtime row."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="hermes-vllm",
        display_name="Hermes vLLM Worker",
        runtime_type="hermes",
        endpoint=HERMES_ENDPOINT,
        model_identifier=HERMES_MODEL,
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == HERMES_ENDPOINT
    assert env["OPENAI_MODEL"] == HERMES_MODEL


@pytest.mark.asyncio
async def test_build_runtime_env_hermes_no_anthropic_tokens(async_session):
    """Hermes runtime must NOT carry Anthropic auth tokens."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="hermes-vllm",
        display_name="Hermes vLLM Worker",
        runtime_type="hermes",
        endpoint=HERMES_ENDPOINT,
        model_identifier=HERMES_MODEL,
        enabled=True,
    )

    # Even if a vault lookup happened, Hermes branch must not query nor set
    # Anthropic tokens. Patch get_secret to track if it gets called.
    with patch(
        "app.routers.internal.get_secret_plaintext_by_key",
        new=AsyncMock(return_value="should-not-leak"),
    ) as mocked:
        env = await build_runtime_env(rt, async_session)

    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # Hermes branch should not even hit the secrets vault for Anthropic keys
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_build_runtime_env_anthropic_regression(async_session):
    """ADR-056: anthropic runtime → empty dict from build_runtime_env.

    CLAUDE_CODE_OAUTH_TOKEN moved into resolve_provider_credentials (single
    source shared with the .env render). build_runtime_env sets no OAuth and
    no OPENAI_* keys for anthropic runtimes. OAuth resolution is covered by
    tests/test_provider_credentials.py::test_anthropic_oauth.
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
async def test_build_runtime_env_vllm_docker_regression(async_session):
    """Regression: Sparky-style vllm_docker runtime still gets OPENAI shim env."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="qwen-coder-vllm",
        display_name="Qwen Coder vLLM",
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8001/v1",
        model_identifier="qwen3-coder",
        enabled=True,
    )

    env = await build_runtime_env(rt, async_session)

    assert env["OPENAI_BASE_URL"] == "http://192.0.2.10:8001/v1"
    assert env["OPENAI_MODEL"] == "qwen3-coder"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.asyncio
async def test_build_runtime_env_hermes_none_or_disabled(async_session):
    """runtime=None or enabled=False → empty dict, even for hermes type."""
    from app.routers.internal import build_runtime_env

    assert await build_runtime_env(None, async_session) == {}

    rt_disabled = Runtime(
        slug="hermes-vllm",
        display_name="Hermes (disabled)",
        runtime_type="hermes",
        endpoint=HERMES_ENDPOINT,
        model_identifier=HERMES_MODEL,
        enabled=False,
    )
    assert await build_runtime_env(rt_disabled, async_session) == {}
