"""Embedding Service — the singleton every memory path embeds through.

Since 2026-04-11 (Boss-Autonomy / Memory-Overhaul Phase 3): embeddings are
generated for all semantic/agent/episodic memory entries and stored in Qdrant.

Since the provider-integrations PR this module is a thin façade: WHICH backend
serves an embedding is decided by ``ai_provider_config`` (env default ->
app_settings override -> secret) and executed by the provider registry in
``embedding_provider.py``. Default stays the GPU box, so a plain install
behaves exactly as before.

Two things changed that callers should know about:

- **No frozen settings.** The old constructor held its own ``Settings()``
  instance, so a runtime override (settings page, secrets save) could never
  reach it — the process had to be restarted. Everything is resolved per call
  against the live singleton now.
- **No second model constant.** The model name comes from
  ``ai_provider_config.embeddings_model()``; ``SparkClient`` reads the same
  function instead of its own copy.

Failure policy is unchanged: ``embed`` raises when the endpoint is
unreachable, and the caller (memory indexing, vault embedding, retry loop)
decides between aborting and fail-soft.

Usage:
    from app.services.embedding_service import embedding_service
    vec = await embedding_service.embed("how did we handle vercel deploys")
    assert len(vec) == 768
"""
import logging

from app.services.embedding_provider import (
    EMBED_DIM,
    active_embedding_provider,
    close_embedding_providers,
)

logger = logging.getLogger("mc.embedding")

__all__ = ["EMBED_DIM", "EmbeddingService", "embedding_service"]


class EmbeddingService:
    """Delegates to whichever provider is active AT CALL TIME."""

    @property
    def provider(self):
        return active_embedding_provider()

    @property
    def url(self) -> str:
        return self.provider.url()

    @property
    def model(self) -> str:
        return self.provider.model()

    async def embed(self, text: str) -> list[float]:
        """Returns a 768-dim vector for the input text.

        Raises (httpx.HTTPError / ValueError) if the provider is unreachable or
        the input is empty. Caller decides whether to abort the memory insert
        or continue best-effort.
        """
        try:
            return await self.provider.embed(text)
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001 — logged, then re-raised unchanged
            logger.warning(
                "embedding via %s failed: %s (text len=%d)",
                self.provider.key, e, len(text or ""),
            )
            raise

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embedding — both providers accept a list in ``input``."""
        return await self.provider.embed_batch(texts)

    async def is_available(self) -> bool:
        """Cheap probe with a 2s cap (D-19, Phase 5 MSY-04).

        Used by EmbeddingRetryLoop._drain_once to decide whether to attempt a
        drain or skip the cycle; the cap keeps a tick bounded even when the
        provider is hung rather than cleanly down.
        """
        return await self.provider.is_available()

    async def close(self):
        await close_embedding_providers()


embedding_service = EmbeddingService()
