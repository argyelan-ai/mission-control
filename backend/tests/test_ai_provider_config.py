"""AI provider settings — the three-layer contract for MC's OWN AI functions.

Pins, per function: env default -> app_settings override -> secret for the auth
material; the allowlist as the security boundary; that an override reaches the
RUNNING process (the import-freeze regression that made the old embedding
service ignore every setting); and the ADR-056 boundary — the two new keys are
read by named MC consumers only, never by an agent runtime.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import Settings, settings
from app.models.app_setting import AppSetting
from app.services import ai_provider_config
from tests.conftest import test_engine

AI_KEYS = [
    "ai_embeddings_provider",
    "ai_embeddings_url",
    "ai_embeddings_model",
    "ai_embeddings_cloud_url",
    "ai_embeddings_cloud_model",
    "ai_insights_provider",
    "ai_insights_model",
    "spark_embedding_url",
]


@pytest.fixture(autouse=True)
def _restore_settings():
    """Every test may patch the live singleton — put the env defaults back."""
    before = {k: getattr(settings, k) for k in AI_KEYS}
    yield
    for k, v in before.items():
        setattr(settings, k, v)


@pytest.fixture(autouse=True)
def _own_session_goes_to_the_test_db(monkeypatch):
    """``ai_provider_config._secret`` opens its own session via
    ``app.database.async_session_maker``. Without this it would talk to the
    developer's real Postgres — the running Mission Control."""
    monkeypatch.setattr("app.database.engine", test_engine)


async def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


# ── 1. Three layers, per function ────────────────────────────────────────


@pytest.mark.asyncio
async def test_defaults_are_todays_behaviour():
    """No rows anywhere: embeddings go to the GPU box with the model the
    embedding service hardcoded before this PR."""
    env = Settings()
    assert env.ai_embeddings_provider == "spark"
    assert env.ai_insights_provider == "spark"
    assert ai_provider_config.embeddings_url() == settings.spark_embedding_url
    assert ai_provider_config.embeddings_model() == settings.spark_embedding_model
    assert ai_provider_config.embeddings_model() == "text-embedding-nomic-embed-text-v1.5"


@pytest.mark.asyncio
async def test_saved_provider_overrides_the_running_config():
    """Cloud arm: switching the provider AND its own URL/model — the
    self-hosted fields stay untouched, so switching back is one click."""
    async with await _session() as s:
        await ai_provider_config.save_ai_provider_settings(
            s,
            {
                "ai_embeddings_provider": "cloud",
                "ai_embeddings_cloud_url": "https://api.example.com/v1/embeddings",
                "ai_embeddings_cloud_model": "nomic-ai/nomic-embed-text-v1.5",
            },
        )
    assert settings.ai_embeddings_provider == "cloud"
    assert ai_provider_config.embeddings_provider_key() == "cloud"
    assert ai_provider_config.embeddings_url() == "https://api.example.com/v1/embeddings"
    assert ai_provider_config.embeddings_model() == "nomic-ai/nomic-embed-text-v1.5"


@pytest.mark.asyncio
async def test_explicit_url_and_model_beat_the_provider_default():
    async with await _session() as s:
        await ai_provider_config.save_ai_provider_settings(
            s,
            {
                "ai_embeddings_url": "http://192.0.2.77:1234/v1/embeddings",
                "ai_embeddings_model": "my-own-embed",
            },
        )
    assert ai_provider_config.embeddings_url() == "http://192.0.2.77:1234/v1/embeddings"
    assert ai_provider_config.embeddings_model() == "my-own-embed"


@pytest.mark.asyncio
async def test_arm_fields_do_not_leak_into_the_other_arm():
    """Per-arm config is the whole point of the cleanup: a self-hosted URL on
    the cloud arm (or vice versa) would silently poison a provider switch."""
    settings.spark_embedding_url = "http://mini:8090/v1/embeddings"
    settings.ai_embeddings_cloud_url = "https://api.example.com/v1/embeddings"

    settings.ai_embeddings_provider = "spark"
    assert ai_provider_config.embeddings_url() == "http://mini:8090/v1/embeddings"

    settings.ai_embeddings_provider = "cloud"
    assert ai_provider_config.embeddings_url() == "https://api.example.com/v1/embeddings"


@pytest.mark.asyncio
async def test_legacy_ollama_cloud_embeddings_row_degrades_to_self_hosted():
    """ollama.com hosts no embedding models — the retired arm's stored row
    must degrade to the self-hosted default instead of breaking startup, AND
    it must not be reported as an override: a pinned badge on a value that is
    actually the env default would lie to the operator."""
    async with await _session() as s:
        s.add(AppSetting(key="ai_embeddings_provider", value="ollama_cloud"))
        await s.commit()
        await ai_provider_config.apply_ai_provider_overrides(s)
        assert ai_provider_config.embeddings_provider_key() == "spark"
        assert "ai_embeddings_provider" not in await ai_provider_config.stored_overrides(s)


@pytest.mark.asyncio
async def test_apply_without_rows_keeps_env_defaults():
    env_default = Settings().ai_insights_provider
    async with await _session() as s:
        await ai_provider_config.apply_ai_provider_overrides(s)
    assert settings.ai_insights_provider == env_default


@pytest.mark.asyncio
async def test_unknown_key_is_rejected_and_nothing_is_written():
    async with await _session() as s:
        with pytest.raises(ValueError):
            await ai_provider_config.save_ai_provider_settings(
                s, {"secret_key": "boom", "ai_insights_provider": "off"}
            )
        assert (await s.exec(select(AppSetting))).all() == []
    assert settings.ai_insights_provider == "spark"


@pytest.mark.asyncio
async def test_invalid_provider_value_is_rejected_and_nothing_is_written():
    """The allowlist covers values, not only keys — 'ollama' (local!) must not
    slip through as a provider name."""
    async with await _session() as s:
        with pytest.raises(ValueError):
            await ai_provider_config.save_ai_provider_settings(
                s, {"ai_embeddings_provider": "ollama"}
            )
        assert (await s.exec(select(AppSetting))).all() == []
    assert settings.ai_embeddings_provider == "spark"


@pytest.mark.asyncio
async def test_a_garbage_row_degrades_to_the_env_default():
    """A row written before an allowlist change must not take startup down."""
    async with await _session() as s:
        s.add(AppSetting(key="ai_insights_provider", value="does-not-exist"))
        await s.commit()
        await ai_provider_config.apply_ai_provider_overrides(s)
    assert settings.ai_insights_provider == "spark"


@pytest.mark.asyncio
async def test_channel_config_does_not_warn_about_ai_rows(caplog):
    """Both pages share one KV table; an AI row is not an 'unknown key'."""
    from app.services.channel_config import stored_overrides as channel_overrides

    async with await _session() as s:
        s.add(AppSetting(key="ai_embeddings_provider", value="spark"))
        s.add(AppSetting(key="totally_unknown_key", value="x"))
        await s.commit()
        with caplog.at_level("WARNING", logger="mc.channel_config"):
            await channel_overrides(s)
    messages = [r.getMessage() for r in caplog.records]
    assert not any("ai_embeddings_provider" in m for m in messages)
    assert any("totally_unknown_key" in m for m in messages)


# ── 2. The secrets layer — named consumers only (ADR-056) ────────────────


@pytest.mark.asyncio
async def test_hf_headers_are_empty_without_a_token():
    """No token = today's behaviour exactly: anonymous, public repos."""
    assert await ai_provider_config.hf_auth_headers() == {}


@pytest.mark.asyncio
async def test_hf_headers_carry_the_stored_token():
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(s, key="hf_token", value="hf_TESTONLY")
    assert await ai_provider_config.hf_auth_headers() == {
        "Authorization": "Bearer hf_TESTONLY"
    }


@pytest.mark.asyncio
async def test_ollama_key_reaches_its_named_consumer():
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(s, key="ollama_api_key", value="oll-TESTONLY")
    assert await ai_provider_config.get_ollama_api_key() == "oll-TESTONLY"


@pytest.mark.asyncio
async def test_embedding_keys_reach_their_named_consumers():
    """Both embeddings keys (optional self-hosted bearer, cloud bearer) follow
    the same ADR-056 pattern: absent = None, stored = the named accessor."""
    from app.services.secrets_helper import upsert_secret_by_key

    assert await ai_provider_config.get_embeddings_api_key() is None
    assert await ai_provider_config.get_embeddings_cloud_api_key() is None
    async with await _session() as s:
        await upsert_secret_by_key(s, key="embeddings_api_key", value="emb-TESTONLY")
        await upsert_secret_by_key(
            s, key="embeddings_cloud_api_key", value="cloud-TESTONLY"
        )
    assert await ai_provider_config.get_embeddings_api_key() == "emb-TESTONLY"
    assert await ai_provider_config.get_embeddings_cloud_api_key() == "cloud-TESTONLY"


@pytest.mark.asyncio
async def test_agent_runtime_credentials_never_see_the_new_keys(async_session):
    """ADR-056 Finding 5 boundary, asserted from the other side: with ALL
    MC-function secrets stored (hf, ollama, both embeddings keys), a keyless
    openai-protocol runtime still gets nothing.

    tests/test_provider_credentials.py pins the same law by mocking the
    lookups; this one pins it against a real vault so a future 'convenience'
    fallback in harness_compat cannot hide behind a mock.
    """
    from app.models.agent import Agent
    from app.models.runtime import Runtime
    from app.services.harness_compat import resolve_provider_credentials
    from app.services.secrets_helper import upsert_secret_by_key

    await upsert_secret_by_key(async_session, key="hf_token", value="hf_TESTONLY")
    await upsert_secret_by_key(async_session, key="ollama_api_key", value="oll-TESTONLY")
    await upsert_secret_by_key(
        async_session, key="embeddings_api_key", value="emb-TESTONLY"
    )
    await upsert_secret_by_key(
        async_session, key="embeddings_cloud_api_key", value="cloud-TESTONLY"
    )

    runtime = Runtime(
        id=uuid.uuid4(), slug="local-vllm", display_name="local",
        runtime_type="vllm_docker", enabled=True,
    )
    agent = Agent(name="boundary-test", agent_runtime="cli-bridge")
    creds = await resolve_provider_credentials(async_session, agent, runtime)
    assert creds == {}


def test_harness_compat_does_not_import_the_provider_config():
    """A structural guard: the agent-runtime credential path must not gain
    access to MC's function keys by import, either."""
    import inspect

    from app.services import harness_compat

    source = inspect.getsource(harness_compat)
    assert "ai_provider_config" not in source
    assert "hf_token" not in source


# ── 3. Endpoints ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_returns_values_and_choices(auth_client: AsyncClient):
    resp = await auth_client.get("/api/v1/ai-providers/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["values"]["ai_embeddings_provider"] == "spark"
    assert body["choices"]["ai_embeddings_provider"] == ["spark", "cloud"]
    assert body["choices"]["ai_insights_provider"] == ["spark", "ollama_cloud", "off"]
    assert [p["key"] for p in body["embedding_providers"]] == ["spark", "cloud"]


@pytest.mark.asyncio
async def test_get_settings_never_carries_key_material(auth_client: AsyncClient):
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(s, key="hf_token", value="hf_SUPERSECRET")
        await upsert_secret_by_key(s, key="ollama_api_key", value="oll-SUPERSECRET")

    resp = await auth_client.get("/api/v1/ai-providers/settings")
    assert resp.status_code == 200, resp.text
    assert "SUPERSECRET" not in resp.text
    assert resp.json()["state"] == {
        "hf_token_set": True,
        "ollama_api_key_set": True,
        "embeddings_api_key_set": False,
        "embeddings_cloud_api_key_set": False,
        "ollama_key_required": False,
        "embeddings_cloud_key_required": False,
    }


@pytest.mark.asyncio
async def test_put_settings_applies_immediately(auth_client: AsyncClient):
    resp = await auth_client.put(
        "/api/v1/ai-providers/settings",
        json={"settings": {"ai_insights_provider": "off"}},
    )
    assert resp.status_code == 200, resp.text
    assert settings.ai_insights_provider == "off"
    assert resp.json()["effective"] == {"ai_insights_provider": "off"}


@pytest.mark.asyncio
async def test_put_settings_rejects_unknown_key_and_invalid_value(
    auth_client: AsyncClient,
):
    assert (
        await auth_client.put(
            "/api/v1/ai-providers/settings", json={"settings": {"secret_key": "nope"}}
        )
    ).status_code == 422
    assert (
        await auth_client.put(
            "/api/v1/ai-providers/settings",
            json={"settings": {"ai_embeddings_provider": "ollama"}},
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_endpoints_require_admin(client: AsyncClient):
    from app.auth import create_access_token
    from app.models.user import User

    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=user_id, email="viewer-ai@mc.local", name="V", role="viewer",
                is_active=True,
            )
        )
        await s.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(user_id), 'viewer')}"}
    assert (
        await client.get("/api/v1/ai-providers/settings", headers=headers)
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/ai-providers/settings", json={"settings": {}}, headers=headers
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/ai-providers/huggingface/test-connection", headers=headers
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/ai-providers/embeddings/test-connection", headers=headers
        )
    ).status_code == 403


# ── 4. Test-button contracts: always 200, failure is a state ─────────────


@pytest.mark.asyncio
async def test_hf_test_without_token_is_an_ok_state(auth_client: AsyncClient):
    resp = await auth_client.post("/api/v1/ai-providers/huggingface/test-connection")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "token_set": False, "connected": False, "username": None,
        "error": None, "anonymous_ok": True,
    }


@pytest.mark.asyncio
async def test_hf_test_reports_a_rejected_token_without_echoing_it(
    auth_client: AsyncClient, monkeypatch
):
    from app.routers import ai_providers as ai_router
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(s, key="hf_token", value="hf_REVOKED_TOKEN")

    async def fake_whoami(token: str):
        return 401, {}

    monkeypatch.setattr(ai_router, "_hf_whoami", fake_whoami)
    resp = await auth_client.post("/api/v1/ai-providers/huggingface/test-connection")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_set"] is True and body["connected"] is False
    assert "401" in body["error"]
    assert "REVOKED" not in resp.text


@pytest.mark.asyncio
async def test_hf_test_reports_a_working_token(auth_client: AsyncClient, monkeypatch):
    from app.routers import ai_providers as ai_router
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(s, key="hf_token", value="hf_GOOD")

    async def fake_whoami(token: str):
        assert token == "hf_GOOD"
        return 200, {"name": "some-account"}

    monkeypatch.setattr(ai_router, "_hf_whoami", fake_whoami)
    body = (
        await auth_client.post("/api/v1/ai-providers/huggingface/test-connection")
    ).json()
    assert body["connected"] is True and body["username"] == "some-account"


@pytest.mark.asyncio
async def test_hf_test_turns_a_transport_error_into_a_state(
    auth_client: AsyncClient, monkeypatch
):
    from app.routers import ai_providers as ai_router
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(s, key="hf_token", value="hf_GOOD")

    async def boom(token: str):
        raise RuntimeError("DNS ist weg")

    monkeypatch.setattr(ai_router, "_hf_whoami", boom)
    resp = await auth_client.post("/api/v1/ai-providers/huggingface/test-connection")
    assert resp.status_code == 200, resp.text
    assert "DNS ist weg" in resp.json()["error"]


@pytest.mark.asyncio
async def test_embeddings_test_reports_the_dimension_mismatch(
    auth_client: AsyncClient, monkeypatch
):
    """A provider that answers with the wrong vector size would silently
    poison the 768-dim collections — 'reachable' is not the answer we need."""
    from app.services import embedding_provider

    async def wrong_size(self, text: str):
        return [0.0] * 1024

    monkeypatch.setattr(
        embedding_provider.BaseEmbeddingProvider, "embed", wrong_size, raising=True
    )
    body = (
        await auth_client.post("/api/v1/ai-providers/embeddings/test-connection")
    ).json()
    assert body["connected"] is True
    assert body["dimension"] == 1024 and body["expected_dimension"] == 768
    assert "1024" in body["error"]


@pytest.mark.asyncio
async def test_embeddings_test_reports_not_configured_as_guided_state(
    auth_client: AsyncClient,
):
    """THE fresh-install state: no URL anywhere. The test button must answer
    200 with a guided message — never a 500 — and url must be empty proof
    that no endpoint was even attempted."""
    settings.spark_embedding_url = ""
    settings.ai_embeddings_url = ""
    resp = await auth_client.post("/api/v1/ai-providers/embeddings/test-connection")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False
    assert body["url"] == ""
    assert "keinen Endpunkt konfiguriert" in (body["error"] or "")


@pytest.mark.asyncio
async def test_embeddings_test_turns_an_outage_into_a_state(
    auth_client: AsyncClient, monkeypatch
):
    from app.services import embedding_provider

    async def down(self, text: str):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(
        embedding_provider.BaseEmbeddingProvider, "embed", down, raising=True
    )
    resp = await auth_client.post("/api/v1/ai-providers/embeddings/test-connection")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False and "Connection refused" in body["error"]
    assert body["provider"] == "spark"
