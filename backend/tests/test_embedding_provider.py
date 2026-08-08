"""EmbeddingProvider — one contract, one model source, two clients.

Before this PR two clients embedded independently (``embedding_service`` and
``SparkClient``), each with its own copy of the model name, and the embedding
service froze a ``Settings()`` instance in its constructor so no runtime
override could ever reach it. Both are pinned here.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services import ai_provider_config, embedding_provider
from app.services.embedding_provider import (
    EMBED_DIM,
    EmbeddingProvider,
    active_embedding_provider,
    all_embedding_providers,
    embedding_provider_catalog,
    get_embedding_provider,
)
from app.services.embedding_service import embedding_service
from app.services.spark_client import SparkClient

AI_KEYS = ["ai_embeddings_provider", "ai_embeddings_url", "ai_embeddings_model"]


@pytest.fixture(autouse=True)
def _restore_settings():
    before = {k: getattr(settings, k) for k in AI_KEYS}
    yield
    for k, v in before.items():
        setattr(settings, k, v)


def _mock_post(json_data, capture: dict | None = None):
    """An httpx client whose ``post`` records what it was asked to send."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()

    async def post(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        return resp

    client = MagicMock()
    client.post = post
    client.aclose = AsyncMock()
    return client


# ── 1. Both clients satisfy the same Protocol ────────────────────────────


def test_both_embedding_clients_satisfy_the_protocol():
    for candidate in (*all_embedding_providers(), SparkClient()):
        assert isinstance(candidate, EmbeddingProvider), candidate


def test_registry_and_settings_allowlist_cannot_drift():
    """INVARIANT from the module docstring: every registered key is a value the
    settings allowlist accepts, and vice versa."""
    assert {p.key for p in all_embedding_providers()} == set(
        ai_provider_config.EMBEDDING_PROVIDERS
    )


def test_lookup_and_catalog_answer_for_the_ui():
    assert get_embedding_provider("spark").key == "spark"
    assert get_embedding_provider("nope") is None
    assert get_embedding_provider(None) is None
    catalog = embedding_provider_catalog()
    active = [c for c in catalog if c["active"]]
    assert len(active) == 1 and active[0]["key"] == "spark"
    # Only the active entry reports a concrete target — the others would be
    # guesses about a config that isn't in effect.
    assert active[0]["model"] == settings.spark_embedding_model
    assert all(c["model"] is None for c in catalog if not c["active"])


def test_the_duplicate_model_constant_is_gone():
    """SparkClient used to carry its own copy of the embedding model name."""
    assert not hasattr(SparkClient, "EMBEDDING_MODEL")
    assert SparkClient().embedding_model == ai_provider_config.embeddings_model()


# ── 2. The import-freeze regression ──────────────────────────────────────


@pytest.mark.asyncio
async def test_embedding_service_reads_the_override_at_call_time():
    """The old service froze ``Settings()`` in __init__, so a settings-page
    save silently required a restart. It must see the change immediately."""
    assert embedding_service.url == settings.spark_embedding_url

    settings.ai_embeddings_url = "http://192.0.2.55:9/v1/embeddings"
    settings.ai_embeddings_model = "switched-live"

    assert embedding_service.url == "http://192.0.2.55:9/v1/embeddings"
    assert embedding_service.model == "switched-live"


@pytest.mark.asyncio
async def test_embedding_service_follows_the_selected_provider():
    settings.ai_embeddings_provider = "ollama_cloud"
    assert embedding_service.provider.key == "ollama_cloud"
    assert active_embedding_provider().key == "ollama_cloud"
    assert embedding_service.url.startswith("https://ollama.com")


# ── 3. What actually goes on the wire ────────────────────────────────────


@pytest.mark.asyncio
async def test_spark_provider_sends_the_resolved_model_and_no_auth():
    """The GPU box is keyless by design — a Bearer header there would be the
    ADR-056 bug in a new place."""
    capture: dict = {}
    provider = embedding_provider.SparkEmbeddingProvider()
    with patch.object(provider, "_get_client", AsyncMock(return_value=_mock_post(
        {"data": [{"embedding": [0.1] * EMBED_DIM}]}, capture
    ))):
        vec = await provider.embed("hallo")

    assert len(vec) == EMBED_DIM
    assert capture["url"] == settings.spark_embedding_url
    assert capture["json"]["model"] == settings.spark_embedding_model
    assert "Authorization" not in capture["headers"]


@pytest.mark.asyncio
async def test_ollama_cloud_provider_sends_the_named_key(monkeypatch):
    capture: dict = {}
    settings.ai_embeddings_provider = "ollama_cloud"
    provider = embedding_provider.OllamaCloudEmbeddingProvider()

    monkeypatch.setattr(
        ai_provider_config, "get_ollama_api_key", AsyncMock(return_value="oll-TESTONLY")
    )
    with patch.object(provider, "_get_client", AsyncMock(return_value=_mock_post(
        {"data": [{"embedding": [0.1] * EMBED_DIM}]}, capture
    ))):
        await provider.embed("hallo")

    assert capture["url"] == "https://ollama.com/v1/embeddings"
    assert capture["headers"]["Authorization"] == "Bearer oll-TESTONLY"


@pytest.mark.asyncio
async def test_ollama_cloud_without_a_key_still_sends_no_header(monkeypatch):
    capture: dict = {}
    provider = embedding_provider.OllamaCloudEmbeddingProvider()
    monkeypatch.setattr(
        ai_provider_config, "get_ollama_api_key", AsyncMock(return_value=None)
    )
    with patch.object(provider, "_get_client", AsyncMock(return_value=_mock_post(
        {"data": [{"embedding": [0.1] * EMBED_DIM}]}, capture
    ))):
        await provider.embed("hallo")
    assert "Authorization" not in capture["headers"]


@pytest.mark.asyncio
async def test_batch_keeps_input_order_and_truncates_extremes():
    capture: dict = {}
    provider = embedding_provider.SparkEmbeddingProvider()
    with patch.object(provider, "_get_client", AsyncMock(return_value=_mock_post(
        {"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]}, capture
    ))):
        out = await provider.embed_batch(["a", "b" * 9000])

    assert out == [[1.0], [2.0]]
    assert len(capture["json"]["input"][1]) == 8000


@pytest.mark.asyncio
async def test_empty_batch_makes_no_call():
    provider = embedding_provider.SparkEmbeddingProvider()
    with patch.object(provider, "_get_client", AsyncMock(side_effect=AssertionError)):
        assert await provider.embed_batch([]) == []


@pytest.mark.asyncio
async def test_empty_text_is_rejected_before_the_call():
    with pytest.raises(ValueError):
        await embedding_service.embed("   ")


@pytest.mark.asyncio
async def test_a_wrong_dimension_warns_but_still_returns(caplog):
    provider = embedding_provider.SparkEmbeddingProvider()
    with patch.object(provider, "_get_client", AsyncMock(return_value=_mock_post(
        {"data": [{"embedding": [0.1] * 1024}]}
    ))), caplog.at_level("WARNING", logger="mc.embedding_provider"):
        vec = await provider.embed("hallo")
    assert len(vec) == 1024
    assert any("1024" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_is_available_reports_instead_of_raising():
    provider = embedding_provider.SparkEmbeddingProvider()
    with patch.object(provider, "embed", AsyncMock(side_effect=ConnectionError("down"))):
        assert await provider.is_available() is False
    with patch.object(provider, "embed", AsyncMock(return_value=[0.0] * EMBED_DIM)):
        assert await provider.is_available() is True


@pytest.mark.asyncio
async def test_embed_failure_reaches_the_caller():
    """Memory indexing decides between abort and fail-soft — a swallowed error
    (or worse, a zero vector) would take that decision away from it."""
    with patch.object(
        embedding_provider.SparkEmbeddingProvider, "embed",
        AsyncMock(side_effect=ConnectionError("Spark ist aus")),
    ):
        with pytest.raises(ConnectionError):
            await embedding_service.embed("hallo")
