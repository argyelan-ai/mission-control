"""ADR-056 — Tests for resolve_provider_credentials (3-stage key routing).

Single source for provider auth material used by both the internal bootstrap
and the .env render, so the two paths can never drift.

OpenAI-protocol order: agent.secret_id > runtime.api_key_secret_id >
global vault fallback ("ollama_api_key"). Anthropic protocol uses the
global OAuth token ("claude_code_oauth_token"), NO OPENAI_* keys.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.harness_compat import resolve_provider_credentials


def _rt(slug="cloud-rt", runtime_type="cloud", api_key_secret_id=None):
    return Runtime(id=uuid.uuid4(), slug=slug, display_name=slug,
                   runtime_type=runtime_type, enabled=True,
                   api_key_secret_id=api_key_secret_id)

def _agent(secret_id=None):
    return Agent(name="cred-test", agent_runtime="cli-bridge", secret_id=secret_id)


@pytest.mark.asyncio
async def test_agent_secret_wins(async_session):
    sid = uuid.uuid4()
    with patch("app.services.harness_compat.get_secret_plaintext_by_id",
               AsyncMock(return_value="agent-key")) as by_id, \
         patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")):
        creds = await resolve_provider_credentials(async_session, _agent(secret_id=sid), _rt())
    assert creds == {"OPENAI_API_KEY": "agent-key"}
    by_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_secret_second(async_session):
    rid = uuid.uuid4()
    async def fake_by_id(session, secret_id):
        return "runtime-key" if secret_id == rid else None
    with patch("app.services.harness_compat.get_secret_plaintext_by_id", side_effect=fake_by_id), \
         patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")):
        creds = await resolve_provider_credentials(
            async_session, _agent(), _rt(api_key_secret_id=rid))
    assert creds == {"OPENAI_API_KEY": "runtime-key"}


@pytest.mark.asyncio
async def test_global_fallback_last(async_session):
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")):
        creds = await resolve_provider_credentials(async_session, _agent(), _rt())
    assert creds == {"OPENAI_API_KEY": "global-key"}


@pytest.mark.asyncio
async def test_anthropic_oauth(async_session):
    rt = _rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth")
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="oauth-token")) as by_key:
        creds = await resolve_provider_credentials(async_session, _agent(), rt)
    assert creds == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"}
    assert by_key.await_args.args[1] == "claude_code_oauth_token"


@pytest.mark.asyncio
async def test_empty_when_nothing_found(async_session):
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value=None)):
        creds = await resolve_provider_credentials(async_session, _agent(), _rt())
    assert creds == {}


@pytest.mark.asyncio
async def test_agent_none_skips_stage_one(async_session):
    """agent=None (bootstrap has no agent context at one call site) → stage 1
    is skipped, runtime secret / global fallback still resolve."""
    rid = uuid.uuid4()
    async def fake_by_id(session, secret_id):
        return "runtime-key" if secret_id == rid else None
    with patch("app.services.harness_compat.get_secret_plaintext_by_id", side_effect=fake_by_id), \
         patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")):
        creds = await resolve_provider_credentials(
            async_session, None, _rt(api_key_secret_id=rid))
    assert creds == {"OPENAI_API_KEY": "runtime-key"}


@pytest.mark.asyncio
async def test_no_runtime_keeps_ollama_fallback(async_session):
    """Agent WITHOUT a runtime (runtime=None) still gets the ollama fallback as
    OPENAI_API_KEY — preserves today's bootstrap behaviour."""
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="ollama-key")) as by_key:
        creds = await resolve_provider_credentials(async_session, _agent(), None)
    assert creds == {"OPENAI_API_KEY": "ollama-key"}
    assert by_key.await_args.args[1] == "ollama_api_key"


@pytest.mark.asyncio
async def test_anthropic_missing_oauth_returns_empty(async_session):
    rt = _rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth")
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value=None)):
        creds = await resolve_provider_credentials(async_session, _agent(), rt)
    assert creds == {}
