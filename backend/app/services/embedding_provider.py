"""EmbeddingProvider — the contract every embedding backend implements.

Same shape as the chat-adapter registry (ADR-072,
``services/chat_adapter.py``) and the host-harness registry (ADR-064): a
``Protocol`` + a dict registry + lookup/catalog helpers. Deliberately the same
mechanics, not a third one.

── Warum überhaupt ────────────────────────────────────────────────────────
The contract already existed de facto, it was just written down twice.
``VaultEmbeddings`` takes "any dgx_client that has ``async embed(text)``"
(vault_embeddings.py), and TWO clients satisfied it: the
``embedding_service`` singleton and ``SparkClient.embed`` — each with its own
copy of the model name. Two copies of a model constant is one drift away from
vectors of two different shapes landing in the same Qdrant collection. Now the
model name has exactly one source (``ai_provider_config.embeddings_model``)
and both clients go through this registry.

Failure policy: ``embed``/``embed_batch`` RAISE (the caller — memory indexing,
vault embedding — already decides between abort and fail-soft, and a silent
zero-vector would be far worse than an exception). ``is_available`` never
raises; it is a probe.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from app.services import ai_provider_config

logger = logging.getLogger("mc.embedding_provider")

# Every provider in the registry must return vectors of this dimension —
# ``memory_vault`` and the three ``memory_*`` collections are created with it.
EMBED_DIM = 768


@runtime_checkable
class EmbeddingProvider(Protocol):
    """One embedding backend. Everything provider-specific — and nothing else."""

    key: str    # "spark" | "ollama_cloud" — the value in AI_EMBEDDINGS_PROVIDER
    label: str  # display name for the settings UI / diagnostics

    async def embed(self, text: str) -> list[float]:
        """One vector for one text. Raises on transport/HTTP failure."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One call, many vectors — order matches the input."""
        ...

    async def is_available(self) -> bool:
        """Cheap bounded probe. Never raises."""
        ...


class BaseEmbeddingProvider:
    """Shared half: the OpenAI-compatible ``/v1/embeddings`` request both
    providers speak. A subclass supplies the URL, the model and the headers."""

    key: str = ""
    label: str = ""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def url(self) -> str:  # pragma: no cover — overridden
        raise NotImplementedError

    def model(self) -> str:  # pragma: no cover — overridden
        raise NotImplementedError

    async def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def timeout(self) -> float:
        from app.config import settings

        return settings.spark_embedding_timeout

    async def _get_client(self) -> httpx.AsyncClient:
        # One kept-alive client per provider, as the embedding service did
        # before this refactor — memory indexing embeds one row at a time and
        # a fresh TCP+TLS handshake per row is a real cost.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout())
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, payload_input: Any) -> dict:
        client = await self._get_client()
        resp = await client.post(
            self.url(),
            json={"model": self.model(), "input": payload_input},
            headers=await self.headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("Leerer Embedding-Input")
        data = await self._post(text[:8000])  # truncate extreme cases
        vec = data["data"][0]["embedding"]
        if len(vec) != EMBED_DIM:
            logger.warning(
                "Embedding hat unerwartete Dim %d (erwartet %d) — Modell/Provider geaendert?",
                len(vec), EMBED_DIM,
            )
        return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post([t[:8000] for t in texts])
        return [item["embedding"] for item in data["data"]]

    async def is_available(self) -> bool:
        import asyncio

        try:
            await asyncio.wait_for(self.embed("ping"), timeout=2.0)
            return True
        except Exception:  # noqa: BLE001 — a probe reports, it does not raise
            return False


class SparkEmbeddingProvider(BaseEmbeddingProvider):
    """The GPU box (LM Studio / vLLM, OpenAI-compatible). Keyless by design —
    it is a machine on your own network, not a paid API."""

    key = "spark"
    label = "GPU-Box (OpenAI-kompatibel)"

    def url(self) -> str:
        return ai_provider_config.embeddings_url()

    def model(self) -> str:
        return ai_provider_config.embeddings_model()


class OllamaCloudEmbeddingProvider(BaseEmbeddingProvider):
    """ollama.com — the ONLY Ollama MC talks to. Local Ollama on the Mac is
    deliberately not an option (it panics the kernel on this hardware).

    This is the explicit ``ollama_api_key`` consumer ADR-056 Finding 5 left
    room for: a named function asks for the key by name, and no runtime ever
    sees it.
    """

    key = "ollama_cloud"
    label = "Ollama Cloud (ollama.com)"

    def url(self) -> str:
        return ai_provider_config.embeddings_url()

    def model(self) -> str:
        return ai_provider_config.embeddings_model()

    async def headers(self) -> dict[str, str]:
        base = {"Content-Type": "application/json"}
        key = await ai_provider_config.get_ollama_api_key()
        if key:
            base["Authorization"] = f"Bearer {key}"
        return base


# ── Registry ──────────────────────────────────────────────────────────────
#
# INVARIANT: every key here is also a value in
# ``ai_provider_config.EMBEDDING_PROVIDERS`` (asserted in
# tests/test_ai_provider_config.py), so the settings allowlist and the
# registry can never drift apart.

_PROVIDERS: dict[str, EmbeddingProvider] | None = None


def _registry() -> dict[str, EmbeddingProvider]:
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = {
            "spark": SparkEmbeddingProvider(),
            "ollama_cloud": OllamaCloudEmbeddingProvider(),
        }
    return _PROVIDERS


def all_embedding_providers() -> list[EmbeddingProvider]:
    return list(_registry().values())


def get_embedding_provider(key: str | None) -> EmbeddingProvider | None:
    if not key:
        return None
    return _registry().get(key)


def active_embedding_provider() -> EmbeddingProvider:
    """The provider the operator selected — spark when nothing is pinned."""
    return _registry()[ai_provider_config.embeddings_provider_key()]


async def close_embedding_providers() -> None:
    """Shutdown hook — closes every provider's kept-alive HTTP client."""
    for provider in all_embedding_providers():
        close = getattr(provider, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as e:  # noqa: BLE001 — shutdown must not fail
                logger.warning("embedding provider %s close failed: %s", provider.key, e)


def embedding_provider_catalog() -> list[dict[str, Any]]:
    """The registry rendered for the settings UI — the registry answers, the
    UI asks (same principle as ``chat_channel_catalog``)."""
    active = ai_provider_config.embeddings_provider_key()
    return [
        {
            "key": p.key,
            "label": p.label,
            "active": p.key == active,
            "url": p.url() if p.key == active else None,
            "model": p.model() if p.key == active else None,
        }
        for p in all_embedding_providers()
    ]
