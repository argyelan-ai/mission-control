"""Embedding Service — Spark LM Studio primary, Ollama fallback.

Since 2026-04-11 (Boss-Autonomy / Memory-Overhaul Phase 3): embeddings are
generated for all semantic/agent/episodic memory entries and stored in Qdrant.

Primary: Spark DGX (192.0.2.10:1234) via LM Studio OpenAI-compat
         Model: text-embedding-nomic-embed-text-v1.5 (768-dim)

Fallback: None automatic — if Spark is down, embedding fails
         and the caller must accept that (the memory is still saved in
         DB, just without vector search until the next backfill).

Usage:
    from app.services.embedding_service import embedding_service
    vec = await embedding_service.embed("how did we handle vercel deploys")
    assert len(vec) == 768
"""
import logging
from typing import Optional

import httpx

from app.config import Settings

logger = logging.getLogger("mc.embedding")

EMBED_DIM = 768


class EmbeddingService:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._settings = Settings()

    @property
    def url(self) -> str:
        return self._settings.spark_embedding_url

    @property
    def model(self) -> str:
        return self._settings.spark_embedding_model

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.spark_embedding_timeout,
            )
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Returns a 768-dim vector for the input text.

        Raises httpx.HTTPError if Spark is unreachable. Caller decides
        whether to abort the memory insert or continue best-effort.
        """
        if not text or not text.strip():
            raise ValueError("Leerer Embedding-Input")

        client = await self._get_client()
        try:
            resp = await client.post(
                self.url,
                json={"model": self.model, "input": text[:8000]},  # truncate extreme cases
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data["data"][0]["embedding"]
            if len(vec) != EMBED_DIM:
                logger.warning(
                    "Embedding hat unerwartete Dim %d (erwartet %d) — Spark-Modell geaendert?",
                    len(vec), EMBED_DIM,
                )
            return vec
        except httpx.HTTPError as e:
            logger.warning("Spark embedding failed: %s (text len=%d)", e, len(text))
            raise

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embedding — Spark/LM Studio supports lists in input."""
        if not texts:
            return []
        client = await self._get_client()
        resp = await client.post(
            self.url,
            json={"model": self.model, "input": [t[:8000] for t in texts]},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]

    async def is_available(self) -> bool:
        """Cheap probe with 2s timeout (D-19, Phase 5 MSY-04).

        Replaces the previous health_check() which did not bound time. Used by
        EmbeddingRetryLoop._drain_once to decide whether to attempt drain or
        skip the cycle. The 2-second cap ensures the retry loop tick stays
        bounded even when Spark/LM Studio is hung rather than cleanly down.
        """
        import asyncio as _asyncio
        try:
            await _asyncio.wait_for(self.embed("ping"), timeout=2.0)
            return True
        except Exception:
            return False

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


embedding_service = EmbeddingService()
