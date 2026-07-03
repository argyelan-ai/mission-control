"""Phase 16 — Tests for build_runtime_env helper.

D-14: Anthropic runtime → CLAUDE_CODE_OAUTH_TOKEN, NO OPENAI_*-keys.
D-15: openclaude/lmstudio/vllm/openai_compatible/unsloth → OPENAI_BASE_URL + OPENAI_MODEL.
D-16: ollama-cloud → OPENAI shim path (slug does not start with anthropic-claude-).
D-17: Helper extracted from internal.py — testable.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.models.runtime import Runtime


@pytest.mark.asyncio
async def test_build_runtime_env_anthropic(async_session):
    """Anthropic slug → CLAUDE_CODE_OAUTH_TOKEN, no OPENAI_*-keys (D-14)."""
    from app.routers.internal import build_runtime_env

    rt = Runtime(
        slug="anthropic-claude-sonnet",
        display_name="Claude Sonnet",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com",
        model_identifier="claude-sonnet-4-6",
        enabled=True,
    )

    with patch(
        "app.routers.internal.get_secret_plaintext_by_key",
        new=AsyncMock(return_value="oauth-token-xyz"),
    ):
        env = await build_runtime_env(rt, async_session)

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-xyz"
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_MODEL" not in env


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
