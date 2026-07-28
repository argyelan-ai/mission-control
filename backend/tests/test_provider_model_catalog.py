"""Provider model catalog (services/model_catalog.py + /api/v1/models/catalog).

NOTE on the filename: ``tests/test_model_catalog.py`` was already taken by the
pre-existing LM-Studio/HuggingFace catalog-search suite, so this one is named
after the feature ("provider model catalog") instead of overwriting it.

No network: every adapter runs against an httpx.MockTransport, mirroring the
pattern in tests/test_cli_versions.py. Redis is fakeredis via the conftest
fixture.
"""

from __future__ import annotations

import json

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
    assert "grok-4.5" in [m["id"] for m in result.models]
    assert "cli-chat-proxy.grok.com" in str(seen[0].url)
    assert "api.x.ai" not in str(seen[0].url)
    # The probe result is UNIONED with the manifest, never replaced by it —
    # covered in detail by the "manifest union" block further down.
    assert [m["id"] for m in result.models][0] == "grok-4.5"


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
    assert "grok-4.5" in [m["id"] for m in result.models]
    assert seen == []


# ── Grok: manifest UNION (the composer-2.5-fast guard) ───────────────────────
# Regression guard for the whole point of _MANIFEST_UNION_PROTOCOLS: the Grok
# CLI knows model slugs no HTTP surface reports, so a WORKING probe must not be
# able to delete them from the catalog.


@pytest.mark.asyncio
async def test_grok_probe_is_unioned_with_manifest_not_replaced_by_it(
    session, monkeypatch
):
    """Probe says grok-4.5, manifest additionally knows composer-2.5-fast →
    both are served. Measured on 2026-07-28: cli-chat-proxy /v1/models reports
    exactly one model while the CLI binary ships more."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "grok-4.5"}]}))
    monkeypatch.setattr(model_catalog, "read_grok_token", lambda: "grok-oauth-test")

    await add_runtime(
        session, slug="grok-cloud", runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "grok"
    )
    result = await model_catalog.discover_provider(session, target)

    ids = [m["id"] for m in result.models]
    assert result.status == model_catalog.STATUS_OK
    assert "grok-4.5" in ids
    assert "composer-2.5-fast" in ids, (
        "a working probe must never remove manifest-only models for grok — "
        "see _MANIFEST_UNION_PROTOCOLS"
    )
    # Live data wins the ordering; manifest-only entries are appended.
    assert ids[0] == "grok-4.5"


@pytest.mark.asyncio
async def test_grok_union_deduplicates_and_live_entry_wins(session, monkeypatch):
    """A model present in BOTH sources appears exactly once, with the live row."""
    mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"data": [{"id": "grok-4.5", "name": "Live Grok"}]}
        ),
    )
    monkeypatch.setattr(model_catalog, "read_grok_token", lambda: "grok-oauth-test")

    await add_runtime(
        session, slug="grok-cloud", runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "grok"
    )
    result = await model_catalog.discover_provider(session, target)

    ids = [m["id"] for m in result.models]
    assert ids.count("grok-4.5") == 1
    assert len(ids) == len(set(ids)), f"duplicate model ids: {ids}"
    live = next(m for m in result.models if m["id"] == "grok-4.5")
    assert live["raw_provider"] == "grok"
    assert live["display_name"] == "Live Grok"


@pytest.mark.asyncio
async def test_manifest_union_is_opt_in_per_protocol(session, monkeypatch):
    """Anthropic must NOT get the union treatment: for every provider except
    grok the live API is better informed than a file in this repo, so a stale
    manifest entry would invent models that no longer exist."""
    mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"data": [{"id": "claude-opus-5"}]}),
    )
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})

    target = await anthropic_target(session)
    result = await model_catalog.discover_provider(session, target)

    assert [m["id"] for m in result.models] == ["claude-opus-5"]
    # The manifest lists several more — none of them may leak in.
    assert "claude-haiku-4-5" not in [m["id"] for m in result.models]
    assert model_catalog._MANIFEST_UNION_PROTOCOLS == frozenset({"grok"})


def test_composer_is_flagged_cli_only_and_not_bindable():
    """composer-2.5-fast is documented but NOT offered: the CLI proxy answers
    HTTP 400 'Model not found' for it (measured 2026-07-28)."""
    entry = next(
        m for m in model_catalog._manifest_models("grok") if m["id"] == "composer-2.5-fast"
    )
    assert entry["cli_only"] is True
    assert entry["note"], "a cli_only entry must explain itself in the UI"
    assert model_catalog.manifest_cli_only_ids("grok") == {"composer-2.5-fast"}
    # grok-4.5 is drivable and must stay bindable.
    assert "grok-4.5" not in model_catalog.manifest_cli_only_ids("grok")
    assert model_catalog.manifest_cli_only_ids("anthropic") == set()


@pytest.mark.asyncio
async def test_cli_only_models_are_not_counted_as_new(session, monkeypatch, fake_redis):
    """A model that can never be bound must not sit in the "neu" badge forever."""
    use_fake_redis(monkeypatch, fake_redis)
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "grok-4.5"}]}))
    monkeypatch.setattr(model_catalog, "read_grok_token", lambda: "grok-oauth-test")

    await add_runtime(
        session, slug="grok-cloud", runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
        model_identifier="grok-4.5",
    )
    providers = await model_catalog.build_catalog(session, force=True)
    grok = next(p for p in providers if p["key"] == "grok")

    composer = next(m for m in grok["models"] if m["id"] == "composer-2.5-fast")
    assert composer["cli_only"] is True
    assert composer["bound"] is False
    assert grok["new_count"] == 0, "cli_only entries must not inflate the new badge"


@pytest.mark.asyncio
async def test_bind_rejects_cli_only_model(auth_client, session, monkeypatch):
    """Binding composer-2.5-fast would create a runtime that 400s on first use."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "grok-4.5"}]}))
    monkeypatch.setattr(model_catalog, "read_grok_token", lambda: "grok-oauth-test")
    await add_runtime(
        session, slug="grok-cloud", runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com",
    )

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "grok", "model_id": "composer-2.5-fast"},
    )
    assert resp.status_code == 422
    assert "CLI" in resp.json()["detail"]

    # The drivable sibling stays bindable — the guard must be surgical.
    ok = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "grok", "model_id": "grok-4.5"},
    )
    assert ok.status_code == 201


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


# ── Kimi: the CLI's own config as PRIMARY, token-independent source ──────────
# The access token lives ~900 s, so the HTTP probe is only current right after a
# Kimi agent logged in. config.toml is always readable — it is the same file
# `kimi provider list --json` reads.

KIMI_CONFIG_TOML = """\
default_model = "kimi-code/k3"

[providers."managed:kimi-code"]
kind = "managed"

[models."kimi-code/kimi-for-coding"]
provider = "managed:kimi-code"
model = "kimi-for-coding"
max_context_size = 262144
display_name = "K2.7 Coding"

[models."kimi-code/kimi-for-coding-highspeed"]
provider = "managed:kimi-code"
model = "kimi-for-coding-highspeed"
max_context_size = 262144
display_name = "K2.7 Coding Highspeed"

[models."kimi-code/k3"]
provider = "managed:kimi-code"
model = "k3"
max_context_size = 1048576
display_name = "K3"

[models."kimi-code/k3-256k"]
provider = "managed:kimi-code"
model = "k3-256k"
max_context_size = 262144
display_name = "K3-256k"
"""


def write_kimi_config(tmp_path, slug: str = "kimi", body: str = KIMI_CONFIG_TOML):
    cfg_dir = tmp_path / ".mc" / "agents" / slug / "kimi-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.toml"
    path.write_text(body)
    return path


def test_kimi_cli_config_yields_all_four_models(monkeypatch, tmp_path):
    """The live shape on this host (kimi-code 0.29.x): four models, incl. the
    one the old hand-written manifest was missing."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    write_kimi_config(tmp_path)

    models = model_catalog.read_kimi_cli_models()

    assert [m["id"] for m in models] == [
        "kimi-for-coding",
        "kimi-for-coding-highspeed",
        "k3",
        "k3-256k",
    ]
    by_id = {m["id"]: m for m in models}
    # The bare `model` value is the id — NOT the table key "kimi-code/k3":
    # runtime.model_identifier carries the bare form and `bound` compares strings.
    assert by_id["k3"]["display_name"] == "K3"
    assert by_id["k3"]["context_window"] == 1048576
    assert by_id["kimi-for-coding-highspeed"]["display_name"] == "K2.7 Coding Highspeed"
    assert by_id["k3-256k"]["context_window"] == 262144


@pytest.mark.asyncio
async def test_kimi_serves_config_models_when_token_is_gone(session, monkeypatch, tmp_path):
    """THE regression this feature exists for: an expired/absent token must no
    longer empty the Kimi catalog."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    write_kimi_config(tmp_path)
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    # No credentials/kimi-code.json anywhere → read_kimi_token raises.

    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "kimi"
    )
    result = await model_catalog.discover_provider(session, target)

    assert len(result.models) == 4
    assert "kimi-for-coding-highspeed" in [m["id"] for m in result.models]
    # Honest status: not "ok" (no live confirmation) and not "manifest_fallback"
    # (this did not come from our hand-typed file).
    assert result.status == model_catalog.STATUS_CLI_CONFIG
    assert result.reason == model_catalog.STATUS_CREDENTIAL_MISSING
    assert seen == [], "no HTTP call may be attempted without a token"


@pytest.mark.asyncio
async def test_kimi_serves_config_models_when_token_is_rejected(
    session, monkeypatch, tmp_path
):
    """Same for a token that exists but the provider 401s (expired ~900 s TTL)."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    write_kimi_config(tmp_path)
    mock_httpx(monkeypatch, lambda r: httpx.Response(401, json={"error": "expired"}))
    monkeypatch.setattr(model_catalog, "read_kimi_token", lambda: "stale-token")

    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "kimi"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_CLI_CONFIG
    assert result.reason == model_catalog.STATUS_UNREACHABLE
    assert len(result.models) == 4


@pytest.mark.asyncio
async def test_kimi_http_augments_config_and_dedupes(session, monkeypatch, tmp_path):
    """Both sources alive: no duplicates, HTTP-only models are added, and the
    config's display_name / context window are backfilled onto the live rows
    (the endpoint returns bare ids)."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    write_kimi_config(tmp_path)
    mock_httpx(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"data": [{"id": "k3"}, {"id": "k4-preview"}]}
        ),
    )
    monkeypatch.setattr(model_catalog, "read_kimi_token", lambda: "fresh-token")

    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "kimi"
    )
    result = await model_catalog.discover_provider(session, target)

    ids = [m["id"] for m in result.models]
    assert result.status == model_catalog.STATUS_OK
    assert len(ids) == len(set(ids)), f"duplicate model ids: {ids}"
    assert set(ids) == {
        "k3", "k4-preview", "kimi-for-coding", "kimi-for-coding-highspeed", "k3-256k",
    }
    by_id = {m["id"]: m for m in result.models}
    assert by_id["k3"]["display_name"] == "K3"  # backfilled from the config
    assert by_id["k3"]["context_window"] == 1048576
    assert by_id["k4-preview"]["display_name"] is None  # unknown stays unknown


def test_kimi_config_newest_agent_file_wins(monkeypatch, tmp_path):
    """Several agents may hold their own config.toml — the freshest wins,
    mirroring read_kimi_token()'s newest-expiry rule."""
    import os

    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    old = write_kimi_config(
        tmp_path,
        slug="kimi-old",
        body='[models."kimi-code/k3"]\nmodel = "k3"\ndisplay_name = "ALT"\n',
    )
    new = write_kimi_config(
        tmp_path,
        slug="kimi-new",
        body='[models."kimi-code/k3"]\nmodel = "k3"\ndisplay_name = "NEU"\n',
    )
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    models = model_catalog.read_kimi_cli_models()
    assert [m["display_name"] for m in models] == ["NEU"]


def test_kimi_broken_config_is_not_fatal(monkeypatch, tmp_path):
    """A corrupt config must degrade to "no config models", never raise — the
    HTTP path still has to be able to carry the provider."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    write_kimi_config(tmp_path, body="this is = not [valid toml")
    assert model_catalog.read_kimi_cli_models() == []


def test_kimi_config_absent_yields_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    assert model_catalog.read_kimi_cli_models() == []


@pytest.mark.asyncio
async def test_kimi_without_config_and_without_token_still_falls_back(
    session, monkeypatch, tmp_path
):
    """Nothing on disk at all → the old manifest behaviour is untouched."""
    monkeypatch.setattr(model_catalog.settings, "home_host", str(tmp_path))
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))

    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1",
    )
    target = next(
        t for t in await model_catalog.build_provider_targets(session) if t.key == "kimi"
    )
    result = await model_catalog.discover_provider(session, target)

    assert result.status == model_catalog.STATUS_MANIFEST_FALLBACK
    assert result.reason == model_catalog.STATUS_CREDENTIAL_MISSING
    assert seen == []


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
    # The Kimi CLI ships four models; the manifest used to list only three.
    kimi_ids = {m["id"] for m in manifest["kimi"]["models"]}
    assert kimi_ids == {"kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"}


def test_manifest_documents_why_grok_is_manifest_driven():
    """Guard for the real deliverable: the next person must not be able to
    delete the grok block as 'stale handwork' without reading why it exists."""
    with open(model_catalog._MANIFEST_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    comment = " ".join(raw["_comment"])
    assert "composer-2.5-fast" in comment
    assert "_MANIFEST_UNION_PROTOCOLS" in comment
    assert raw["grok"].get("manifest_is_authoritative") is True
    # And the module itself carries the same warning.
    assert "MANIFEST-DRIVEN" in (model_catalog.__doc__ or "").upper()
    assert "composer-2.5-fast" in (model_catalog.__doc__ or "")


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


# ── Naming + dedupe on bind (2026-07-28) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_derives_the_display_name_instead_of_the_raw_id(
    auth_client, session, monkeypatch
):
    """`anthropic-claude-opus-5` was created with display_name "claude-opus-5" —
    a raw id next to hand-written labels is exactly how the registry started
    looking duplicated."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await anthropic_target(session)

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "anthropic", "model_id": "claude-opus-5"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["runtime"]["display_name"] == "Claude Opus 5 (Anthropic Pro/Max)"


@pytest.mark.asyncio
async def test_bind_derives_display_name_for_every_provider(
    auth_client, session, monkeypatch
):
    """Not an Anthropic special case — ollama, grok and kimi bind the same way."""
    monkeypatch.setattr(model_catalog, "read_grok_token", lambda: "grok-oauth")
    monkeypatch.setattr(model_catalog, "read_kimi_token", lambda: "kimi-token")
    patch_creds(monkeypatch, {"OPENAI_API_KEY": "sk-openai"})
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))

    await add_runtime(
        session, slug="ollama-cloud", runtime_type="cloud",
        endpoint="https://ollama.com/v1", model_identifier="glm-5.1",
    )
    await add_runtime(
        session, slug="grok-cloud", runtime_type="grok",
        endpoint="https://cli-chat-proxy.grok.com", model_identifier="grok-4.5",
    )
    await add_runtime(
        session, slug="kimi-cloud", runtime_type="kimi",
        endpoint="https://api.kimi.com/coding/v1", model_identifier="kimi-code/k3",
    )

    for provider_key, model_id, slug, name in [
        ("openai:ollama-cloud", "glm-5.2", "ollama-cloud-glm-5-2", "GLM 5.2 (Ollama Cloud)"),
        ("grok", "grok-5", "grok-5", "Grok 5 (xAI Cloud)"),
        # No "Kimi" in the name: the id is `k3-256k` and the rule invents
        # nothing — the vendor is already in the provider label.
        ("kimi", "k3-256k", "kimi-k3-256k", "K3 256K (Moonshot Cloud)"),
    ]:
        resp = await auth_client.post(
            "/api/v1/models/catalog/bind",
            json={"provider_key": provider_key, "model_id": model_id},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == slug
        assert body["runtime"]["display_name"] == name


@pytest.mark.asyncio
async def test_bind_does_not_duplicate_a_row_that_already_drives_the_model(
    auth_client, session, monkeypatch
):
    """The dedupe guard: identity is (endpoint, model_identifier), not the slug.

    The seeded row `anthropic-claude-opus` drives `claude-opus-4-8` under a slug
    the rule would never derive. Binding that same model from the catalog must
    return that row, not create `anthropic-claude-opus-4-8` beside it.
    """
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=anthropic_body()))
    patch_creds(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-test"})
    await anthropic_target(session)  # slug anthropic-claude-opus, claude-opus-4-8

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "anthropic", "model_id": "claude-opus-4-8"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["created"] is False
    assert body["slug"] == "anthropic-claude-opus"

    rows = (
        await session.exec(
            select(Runtime).where(Runtime.model_identifier == "claude-opus-4-8")
        )
    ).all()
    assert len(rows) == 1, "a second row for the same model on the same endpoint"


@pytest.mark.asyncio
async def test_bind_dedupe_ignores_a_trailing_slash_on_the_endpoint(
    auth_client, session, monkeypatch
):
    """`https://ollama.com/v1` and `https://ollama.com/v1/` are one endpoint."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    patch_creds(monkeypatch, {"OPENAI_API_KEY": "sk-openai"})

    await add_runtime(
        session, slug="ollama-cloud", runtime_type="cloud",
        endpoint="https://ollama.com/v1", model_identifier="glm-5.1",
    )
    # A second row for the SAME model, written with a trailing slash.
    await add_runtime(
        session, slug="ollama-legacy", runtime_type="cloud",
        endpoint="https://ollama.com/v1/", model_identifier="glm-5.2",
    )

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "openai:ollama-cloud", "model_id": "glm-5.2"},
    )
    assert resp.status_code == 201
    assert resp.json()["created"] is False
    assert resp.json()["slug"] == "ollama-legacy"


@pytest.mark.asyncio
async def test_bind_still_creates_a_row_for_the_same_model_on_another_endpoint(
    auth_client, session, monkeypatch
):
    """The guard keys on endpoint AND model — `glm-5.2` at Ollama Cloud and
    `glm-5.2` on a local vLLM are two different runtimes and must stay two rows."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    patch_creds(monkeypatch, {"OPENAI_API_KEY": "sk-openai"})

    await add_runtime(
        session, slug="ollama-cloud", runtime_type="cloud",
        endpoint="https://ollama.com/v1", model_identifier="glm-5.1",
    )
    await add_runtime(
        session, slug="local-vllm", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", model_identifier="glm-5.2",
    )

    resp = await auth_client.post(
        "/api/v1/models/catalog/bind",
        json={"provider_key": "openai:ollama-cloud", "model_id": "glm-5.2"},
    )
    assert resp.status_code == 201
    assert resp.json()["created"] is True
    assert resp.json()["slug"] == "ollama-cloud-glm-5-2"
