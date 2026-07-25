"""Provider model catalog (services/model_catalog.py + /api/v1/models/catalog).

NOTE on the filename: ``tests/test_model_catalog.py`` was already taken by the
pre-existing LM-Studio/HuggingFace catalog-search suite, so this one is named
after the feature ("provider model catalog") instead of overwriting it.

No network: every adapter runs against an httpx.MockTransport, mirroring the
pattern in tests/test_cli_versions.py. Redis is fakeredis via the conftest
fixture.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services import model_catalog


# ── Helpers ──────────────────────────────────────────────────────────────────


def mock_httpx(monkeypatch, handler):
    """Route every model_catalog HTTP call through `handler`.

    Returns a list that collects the requests, so tests can assert on headers
    and on the NUMBER of attempts (retry vs. no-retry).
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", fake_async_client)
    # Retry backoff must not add real seconds to the suite.
    monkeypatch.setattr(model_catalog, "_RETRY_BACKOFF", 0)
    return seen


def use_fake_redis(monkeypatch, fake_redis):
    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(model_catalog, "get_redis", _get_redis)


def patch_creds(monkeypatch, creds: dict):
    async def fake_creds(*_args, **_kwargs):
        return creds

    monkeypatch.setattr(model_catalog, "resolve_provider_credentials", fake_creds)


async def add_runtime(session: AsyncSession, **kwargs) -> Runtime:
    defaults = {
        "display_name": kwargs.get("slug", "rt"),
        "runtime_type": "cloud",
        "endpoint": "https://example.invalid/v1",
    }
    defaults.update(kwargs)
    rt = Runtime(**defaults)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def anthropic_target(session: AsyncSession) -> model_catalog.ProviderTarget:
    await add_runtime(
        session,
        slug="anthropic-claude-opus",
        runtime_type="cloud",
        endpoint="https://api.anthropic.com/v1/messages",
        model_identifier="claude-opus-4-8",
    )
    targets = await model_catalog.build_provider_targets(session)
    return next(t for t in targets if t.key == "anthropic")


def anthropic_body() -> dict:
    return {
        "data": [
            {
                "type": "model",
                "id": "claude-opus-5",
                "display_name": "Claude Opus 5",
                "created_at": "2026-07-25T00:00:00Z",
            },
            {
                "type": "model",
                "id": "claude-opus-4-8",
                "display_name": "Claude Opus 4.8",
                "created_at": "2026-05-01T00:00:00Z",
            },
        ]
    }


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """The dev machine this suite runs on has REAL ~/.grok and ~/.mc credential
    files. Never let a test read them (same class of accident as the
    vault-pollution guard in conftest.py)."""
    monkeypatch.setattr(model_catalog.settings, "home_host", "/nonexistent-test-home")


# ── Anthropic adapter ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_sends_bearer_and_version_header(session, monkeypatch):
    """The OAuth token MUST go out as `Authorization: Bearer` (as `x-api-key`
    the API answers 401) and `anthropic-version` is mandatory."""
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_OK
    assert [m["id"] for m in result.models] == ["claude-opus-5", "claude-opus-4-8"]
    assert result.models[0]["display_name"] == "Claude Opus 5"

    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == model_catalog.ANTHROPIC_MODELS_URL
    assert req.headers["authorization"] == "Bearer sk-oauth-test"
    assert req.headers["anthropic-version"] == model_catalog.ANTHROPIC_VERSION
    assert "x-api-key" not in req.headers


@pytest.mark.asyncio
async def test_anthropic_without_vault_token_is_credential_missing(session, monkeypatch):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    # The manifest still carries models, so the status reads manifest_fallback
    # but the ORIGINAL reason survives in `error`.
    assert result.status == model_catalog.STATUS_MANIFEST_FALLBACK
    assert result.reason == model_catalog.STATUS_CREDENTIAL_MISSING
    assert "claude_code_oauth_token" in (result.error or "")
    assert result.models  # must not be a silent empty list
    assert seen == []  # no HTTP call at all without a credential


# ── Grok adapter ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grok_uses_cli_proxy_not_x_ai(session, monkeypatch):
    """The grok harness can only drive what its own CLI proxy exposes — the
    api.x.ai catalog (~10 models) is deliberately NOT the source."""
    seen = mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"data": [{"id": "grok-4.5"}]}),
    )
    monkeypatch.setattr(model_catalog, "read_grok_token", lambda: "grok-oauth-test")

    await add_runtime(
        session,
        slug="grok-cloud",
        runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
        model_identifier="grok-4.5",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "grok"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_OK
    assert [m["id"] for m in result.models] == ["grok-4.5"]
    assert "cli-chat-proxy.grok.com" in str(seen[0].url)
    assert "api.x.ai" not in str(seen[0].url)


@pytest.mark.asyncio
async def test_grok_token_unreadable_degrades_to_manifest(session, monkeypatch):
    """Reality inside Docker: ~/.grok/auth.json is not mounted into the backend."""
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    await add_runtime(
        session,
        slug="grok-cloud",
        runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "grok"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_MANIFEST_FALLBACK
    # The badge says "stale", but the underlying cause stays inspectable.
    assert result.reason == model_catalog.STATUS_CREDENTIAL_MISSING
    assert "nicht lesbar" in (result.error or "")
    assert [m["id"] for m in result.models] == ["grok-4.5"]
    assert seen == []


# ── Kimi adapter ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_success(session, monkeypatch):
    seen = mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"data": [{"id": "kimi-for-coding"}, {"id": "k3"}, {"id": "k3-256k"}]},
        ),
    )
    monkeypatch.setattr(model_catalog, "read_kimi_token", lambda: "kimi-token")

    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "kimi"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_OK
    assert [m["id"] for m in result.models] == ["kimi-for-coding", "k3", "k3-256k"]
    assert str(seen[0].url) == model_catalog.KIMI_MODELS_URL
    assert seen[0].headers["authorization"] == "Bearer kimi-token"


@pytest.mark.asyncio
async def test_kimi_token_is_never_cached(session, monkeypatch, fake_redis):
    """The Kimi access token lives ~900 s — it must be read from disk on EVERY
    probe. Only the model list is allowed into Redis."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "k3"}]}))
    use_fake_redis(monkeypatch, fake_redis)

    reads: list[int] = []

    def counting_reader():
        reads.append(1)
        return f"kimi-token-{len(reads)}"

    monkeypatch.setattr(model_catalog, "read_kimi_token", counting_reader)

    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "kimi"
    )

    await model_catalog.get_provider_catalog(session, target, force=True)
    await model_catalog.get_provider_catalog(session, target, force=True)

    assert len(reads) == 2, "token must be re-read, not reused from a cache"

    cached = await fake_redis.get(model_catalog.RedisKeys.model_catalog_provider("kimi"))
    assert cached is not None
    assert "kimi-token" not in cached  # no credential material in the cache


@pytest.mark.asyncio
async def test_kimi_reads_newest_credential_file(monkeypatch, tmp_path):
    """Several agents may hold their own Kimi OAuth file — the one with the
    latest expiry wins."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    for slug, token, expires in (("kimi", "old", 100), ("kimi2", "new", 999)):
        cred_dir = tmp_path / ".mc" / "agents" / slug / "kimi-config" / "credentials"
        cred_dir.mkdir(parents=True)
        (cred_dir / "kimi-code.json").write_text(
            f'{{"access_token": "{token}", "expires_at": {expires}}}'
        )

    assert model_catalog.read_kimi_token() == "new"


@pytest.mark.asyncio
async def test_kimi_missing_credentials_raises_credential_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    with pytest.raises(model_catalog._CredentialUnavailable):
        model_catalog.read_kimi_token()


# ── OpenAI adapter ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_adapter_probes_runtime_endpoint(session, monkeypatch):
    seen = mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"data": [{"id": "glm-5.1"}]}),
    )
    patch_creds(monkeypatch, {"OPENAI_API_KEY": "sk-openai"})

    await add_runtime(
        session, slug="ollama-cloud", runtime_type="cloud",
        endpoint="https://ollama.com/v1", model_identifier="glm-5.1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session)
        if t.key == "openai:ollama-cloud"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_OK
    assert str(seen[0].url) == "https://ollama.com/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-openai"


@pytest.mark.asyncio
async def test_openai_adapter_without_key_still_probes(session, monkeypatch):
    """Local vLLM / LM Studio serve /v1/models keyless — a missing key is not
    a credential error here."""
    seen = mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3"}]}),
    )
    patch_creds(monkeypatch, {})

    await add_runtime(
        session, slug="qwen-general", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session)
        if t.key == "openai:qwen-general"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_OK
    assert "authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_offline_local_runtime_is_unreachable_not_empty(session, monkeypatch):
    """A local runtime that is simply switched off must be visibly offline —
    an empty model list would read as "this provider has nothing"."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_httpx(monkeypatch, boom)
    patch_creds(monkeypatch, {})

    await add_runtime(
        session, slug="qwen-general", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session)
        if t.key == "openai:qwen-general"
    )
    result = await model_catalog.discover_provider(session, target)

    # No manifest entry exists for the openai protocol (every endpoint differs),
    # so this stays a hard "unreachable" with a reason attached.
    assert result.status == model_catalog.STATUS_UNREACHABLE
    assert result.models == []
    assert "ConnectError" in (result.error or "")


# ── Error handling: retry policy ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_401_is_not_retried(session, monkeypatch):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(401, json={"error": "nope"}))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "expired"})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    assert len(seen) == 1, "a rejected credential must not be retried"
    assert "401" in (result.error or "")
    # Manifest carries anthropic models → shown as stale rather than blank,
    # with the credential problem preserved in `reason`.
    assert result.status == model_catalog.STATUS_MANIFEST_FALLBACK
    assert result.reason == model_catalog.STATUS_CREDENTIAL_MISSING
    assert any(m["id"] == "claude-opus-5" for m in result.models)


@pytest.mark.asyncio
async def test_401_without_manifest_is_credential_missing(session, monkeypatch):
    """Same 401, but for a protocol the manifest doesn't cover (openai) — the
    status must then say credential_missing rather than manifest_fallback."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(401, json={"error": "nope"}))
    patch_creds(monkeypatch, {"OPENAI_API_KEY": "stale"})

    await add_runtime(
        session, slug="ollama-cloud", runtime_type="cloud",
        endpoint="https://ollama.com/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session)
        if t.key == "openai:ollama-cloud"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_CREDENTIAL_MISSING
    assert result.models == []


@pytest.mark.asyncio
async def test_500_retries_then_falls_back_to_manifest(session, monkeypatch):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    assert len(seen) == model_catalog._RETRIES == 2
    assert result.status == model_catalog.STATUS_MANIFEST_FALLBACK
    assert result.reason == model_catalog.STATUS_UNREACHABLE
    assert any(m["id"] == "claude-opus-5" for m in result.models)
    assert "500" in (result.error or "")


@pytest.mark.asyncio
async def test_500_then_200_succeeds_on_retry(session, monkeypatch):
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, json=anthropic_body())

    mock_httpx(monkeypatch, flaky)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    assert calls["n"] == 2
    assert result.status == model_catalog.STATUS_OK


@pytest.mark.asyncio
async def test_non_json_200_is_unreachable_not_a_crash(session, monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, text="<html>captive portal"))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_MANIFEST_FALLBACK
    assert result.error


# ── Cache ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_http_call(session, monkeypatch, fake_redis):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    first = await model_catalog.get_provider_catalog(session, target)
    second = await model_catalog.get_provider_catalog(session, target)

    assert len(seen) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["cached_at"] == first["cached_at"]


@pytest.mark.asyncio
async def test_failure_uses_short_negative_ttl(session, monkeypatch, fake_redis):
    mock_httpx(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    await model_catalog.get_provider_catalog(session, target)

    ttl = await fake_redis.ttl(
        model_catalog.RedisKeys.model_catalog_provider("anthropic")
    )
    assert 0 < ttl <= model_catalog.NEGATIVE_CACHE_TTL


@pytest.mark.asyncio
async def test_success_uses_long_ttl(session, monkeypatch, fake_redis):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    await model_catalog.get_provider_catalog(session, target)

    ttl = await fake_redis.ttl(
        model_catalog.RedisKeys.model_catalog_provider("anthropic")
    )
    assert model_catalog.NEGATIVE_CACHE_TTL < ttl <= model_catalog.CACHE_TTL


@pytest.mark.asyncio
async def test_invalidate_cache_forces_reprobe(session, monkeypatch, fake_redis):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    await model_catalog.get_provider_catalog(session, target)
    await model_catalog.invalidate_cache(session)
    await model_catalog.get_provider_catalog(session, target)

    assert len(seen) == 2


# ── Provider targets + bound flag ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bound_flag_reflects_existing_runtime_rows(session, monkeypatch, fake_redis):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    use_fake_redis(monkeypatch, fake_redis)
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    await anthropic_target(session)  # runtime bound to claude-opus-4-8
    providers = await model_catalog.build_catalog(session)
    anthropic = next(p for p in providers if p["key"] == "anthropic")

    by_id = {m["id"]: m for m in anthropic["models"]}
    assert by_id["claude-opus-4-8"]["bound"] is True
    assert by_id["claude-opus-5"]["bound"] is False
    assert anthropic["new_count"] == 1


@pytest.mark.asyncio
async def test_disabled_runtimes_do_not_create_providers(session):
    await add_runtime(
        session, slug="grok-cloud", runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com", enabled=False,
    )
    targets = await model_catalog.build_provider_targets(session)
    assert [t.key for t in targets] == []


@pytest.mark.asyncio
async def test_anthropic_runtimes_collapse_into_one_provider(session):
    await add_runtime(
        session, slug="anthropic-claude-opus", runtime_type="cloud",
        endpoint="https://api.anthropic.com/v1/messages",
    )
    await add_runtime(
        session, slug="anthropic-claude-sonnet", runtime_type="cloud",
        endpoint="https://api.anthropic.com/v1/messages",
    )
    targets = await model_catalog.build_provider_targets(session)
    anthropic = next(t for t in targets if t.key == "anthropic")
    assert sorted(anthropic.runtime_slugs) == [
        "anthropic-claude-opus",
        "anthropic-claude-sonnet",
    ]


@pytest.mark.asyncio
async def test_openai_runtimes_stay_separate_providers(session):
    """Two openai runtimes = two endpoints = two providers. Merging them would
    claim a local vLLM offers Ollama Cloud's models."""
    await add_runtime(
        session, slug="ollama-cloud", runtime_type="cloud",
        endpoint="https://ollama.com/v1",
    )
    await add_runtime(
        session, slug="qwen-general", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
    )
    keys = {t.key for t in await model_catalog.build_provider_targets(session)}
    assert keys == {"openai:ollama-cloud", "openai:qwen-general"}


@pytest.mark.asyncio
async def test_unknown_protocol_runtime_is_skipped(session):
    """hermes has no wire protocol in runtime_protocol() → no discovery target."""
    await add_runtime(
        session, slug="hermes-vllm", runtime_type="hermes",
        endpoint="http://192.0.2.10:8000/v1",
    )
    assert await model_catalog.build_provider_targets(session) == []


# ── Manifest ─────────────────────────────────────────────────────────────────


def test_manifest_ships_verified_ids():
    manifest = model_catalog.read_manifest()
    assert {"anthropic", "grok", "kimi"} <= set(manifest)
    anthropic_ids = {m["id"] for m in manifest["anthropic"]["models"]}
    assert "claude-opus-5" in anthropic_ids
    # No openai entry on purpose — every openai runtime is its own endpoint.
    assert "openai" not in manifest


def test_manifest_unreadable_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(model_catalog, "_MANIFEST_PATH", tmp_path / "missing.json")
    assert model_catalog.read_manifest() == {}
    assert model_catalog._manifest_models("anthropic") == []


# ── Endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_catalog_endpoint(auth_client, session, monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await anthropic_target(session)

    resp = await auth_client.get("/api/v1/models/catalog")
    assert resp.status_code == 200
    data = resp.json()
    anthropic = next(p for p in data["providers"] if p["key"] == "anthropic")
    assert anthropic["status"] == "ok"
    assert anthropic["cached_at"]
    assert data["new_models"] == 1
    assert data["total_models"] == 2


@pytest.mark.asyncio
async def test_catalog_route_does_not_shadow_model_detail(auth_client):
    """`/models/catalog` must not be parsed as `/models/{model_id}`."""
    resp = await auth_client.get("/api/v1/models/catalog")
    assert resp.status_code == 200
    assert "providers" in resp.json()


@pytest.mark.asyncio
async def test_refresh_endpoint_reprobes(auth_client, session, monkeypatch):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await anthropic_target(session)

    await auth_client.get("/api/v1/models/catalog")
    assert len(seen) == 1
    resp = await auth_client.post("/api/v1/models/catalog/refresh")
    assert resp.status_code == 200
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_bind_creates_runtime_inheriting_provider(auth_client, session, monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    template = await anthropic_target(session)

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "anthropic", "model_id": "claude-opus-5"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["slug"] == "anthropic-claude-opus-5"
    assert body["runtime"]["model_identifier"] == "claude-opus-5"
    assert body["runtime"]["endpoint"] == template.endpoint
    assert body["runtime"]["runtime_type"] == "cloud"

    # The new row must be a real runtime the catalog then sees as bound.
    row = (
        await session.exec(select(Runtime).where(Runtime.slug == "anthropic-claude-opus-5"))
    ).first()
    assert row is not None and row.model_identifier == "claude-opus-5"


@pytest.mark.asyncio
async def test_bind_is_idempotent(auth_client, session, monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await anthropic_target(session)

    payload = {"provider_key": "anthropic", "model_id": "claude-opus-5"}
    first = await auth_client.post("/api/v1/models/catalog/bind", json=payload)
    second = await auth_client.post("/api/v1/models/catalog/bind", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["created"] is False


@pytest.mark.asyncio
async def test_bind_slug_collision_is_409(auth_client, session, monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await anthropic_target(session)
    # Same slug the bind would derive, but pointing at a different model.
    await add_runtime(
        session, slug="anthropic-claude-opus-5", runtime_type="cloud",
        endpoint="https://api.anthropic.com/v1/messages",
        model_identifier="something-else",
    )

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "anthropic", "model_id": "claude-opus-5"},
    )
    assert resp.status_code == 409
    assert "something-else" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_bind_unknown_provider_is_404(auth_client):
    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "does-not-exist", "model_id": "x"},
    )
    assert resp.status_code == 404


def test_slug_derivation_avoids_duplicate_prefix():
    from app.routers.models import _derive_slug

    assert _derive_slug("anthropic", "claude-opus-5") == "anthropic-claude-opus-5"
    assert _derive_slug("grok", "grok-4.5") == "grok-4-5"
    assert _derive_slug("kimi", "k3-256k") == "kimi-k3-256k"
    assert _derive_slug("ollama-cloud", "glm-5.1") == "ollama-cloud-glm-5-1"
