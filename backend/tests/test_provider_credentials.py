"""ADR-056 (amended 2026-07-05) — Tests for resolve_provider_credentials.

Single source for provider auth material used by both the internal bootstrap
and the .env render, so the two paths can never drift.

OpenAI-protocol order: agent.secret_id > runtime.api_key_secret_id. No global
vault fallback — removed per ADR-056 Finding 5 (a global "ollama_api_key"
fallback let any openai-protocol runtime, including keyless local ones,
silently inherit a paid cloud key). Anthropic protocol uses the global OAuth
token ("claude_code_oauth_token"), NO OPENAI_* keys.
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
async def test_no_secrets_bound_returns_empty_and_skips_global_vault(async_session):
    """Neither agent.secret_id nor runtime.api_key_secret_id set → no
    OPENAI_API_KEY at all, and the global vault ("ollama_api_key") must never
    be touched — that's the fallback ADR-056 Finding 5 removed."""
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")) as by_key:
        creds = await resolve_provider_credentials(async_session, _agent(), _rt())
    assert creds == {}
    by_key.assert_not_awaited()


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
    is skipped, runtime secret (stage 2) still resolves."""
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
async def test_no_runtime_and_no_agent_secret_returns_empty(async_session):
    """Agent WITHOUT a runtime (runtime=None) and without a bound secret gets
    no OPENAI_API_KEY — no global fallback kicks in."""
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")) as by_key:
        creds = await resolve_provider_credentials(async_session, _agent(), None)
    assert creds == {}
    by_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_anthropic_missing_oauth_returns_empty(async_session):
    rt = _rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth")
    with patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value=None)):
        creds = await resolve_provider_credentials(async_session, _agent(), rt)
    assert creds == {}


@pytest.mark.asyncio
async def test_dangling_agent_secret_id_warns_and_falls_back_to_runtime(async_session, caplog):
    """agent.secret_id set but stage 1 resolves to None → falls back to
    runtime.api_key_secret_id (stage 2) AND logs exactly one warning naming
    the agent. No global vault lookup happens."""
    sid = uuid.uuid4()
    rid = uuid.uuid4()
    agent = _agent(secret_id=sid)
    with caplog.at_level("WARNING", logger="app.services.harness_compat"), \
         patch("app.services.harness_compat.get_secret_plaintext_by_id",
               AsyncMock(side_effect=lambda s, secret_id: "runtime-key" if secret_id == rid else None)), \
         patch("app.services.harness_compat.get_secret_plaintext_by_key",
               AsyncMock(return_value="global-key")) as by_key:
        creds = await resolve_provider_credentials(
            async_session, agent, _rt(api_key_secret_id=rid))
    assert creds == {"OPENAI_API_KEY": "runtime-key"}
    by_key.assert_not_awaited()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert agent.name in warnings[0].getMessage()
