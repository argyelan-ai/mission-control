"""Shared client for DGX-Spark vLLM Qwen3.6-35B + nomic-embed-v1.5.

Reachable on the LAN at 192.0.2.10. We assume the Spark is on the same
network and reachable; the client surfaces a SparkUnreachableError with the
target URL so operators can diagnose quickly.

Used by:
  - vault_title_backfill (W2)
  - vault_wikilink_backfill (W3-B)
  - vault_similarity_edges (W3-A, embeddings only)

Integration tests (pytest.mark.integration) hit the real Spark endpoint and
require LAN reachability to 192.0.2.10. Run with: pytest -m integration
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SparkUnreachableError(RuntimeError):
    """Raised when the Spark host cannot be contacted."""


class SparkClient:
    """LLM completions + embeddings against the GPU box.

    Satisfies ``services.embedding_provider.EmbeddingProvider`` (key/label/
    embed/embed_batch/is_available) so the vault backfills and the memory
    pipeline speak ONE contract. The embedding model is no longer a constant
    here: it comes from ``ai_provider_config.embeddings_model()``, the same
    single source ``embedding_service`` reads — the previous second copy of
    the name was one edit away from two vector shapes in one collection.
    """

    key = "spark"
    label = "GPU-Box (OpenAI-kompatibel)"

    @property
    def embedding_model(self) -> str:
        from app.services import ai_provider_config

        return ai_provider_config.embeddings_model()

    def __init__(
        self,
        llm_url: str | None = None,
        embedding_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        from app.config import settings

        self.llm_url = (llm_url or settings.spark_llm_url).rstrip("/")
        # spark_embedding_url is the full POST URL ".../v1/embeddings"; strip the
        # "/embeddings" suffix so self.embedding_url is the base URL (.../v1).
        emb = embedding_url or settings.spark_embedding_url
        if emb.endswith("/embeddings"):
            emb = emb.rsplit("/embeddings", 1)[0]
        self.embedding_url = emb.rstrip("/")
        self.timeout = timeout

    async def _resolve_llm_model(self) -> str:
        """Resolve the currently active LLM model identifier.

        Delegates to ``runtime_model_resolver`` which reads the DB and falls
        back to probing ``/v1/models`` if the runtime row has no value. On
        any failure, returns the configured settings default so we never
        crash a request just because resolution failed.
        """
        from app.config import settings
        from app.services.runtime_model_resolver import get_active_spark_model

        try:
            resolved = await get_active_spark_model()
            if resolved:
                return resolved
        except Exception as exc:  # noqa: BLE001
            logger.warning("spark_client: model resolver failed: %s", exc)
        return settings.spark_llm_model

    async def health_check(self) -> dict[str, Any]:
        llm_model = await self._resolve_llm_model()
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            try:
                llm_resp = await cli.get(f"{self.llm_url}/models")
                llm_ready = llm_resp.status_code == 200 and any(
                    m["id"] == llm_model for m in llm_resp.json().get("data", [])
                )
            except httpx.HTTPError:
                llm_ready = False

            try:
                emb_resp = await cli.post(
                    f"{self.embedding_url}/embeddings",
                    json={"model": self.embedding_model, "input": "ping"},
                )
                emb_ready = emb_resp.status_code == 200 and len(
                    emb_resp.json()["data"][0]["embedding"]
                ) == 768
            except httpx.HTTPError:
                emb_ready = False

        return {
            "llm_model": llm_model,
            "llm_url": self.llm_url,
            "llm_ready": llm_ready,
            "embedding_model": self.embedding_model,
            "embedding_url": self.embedding_url,
            "embedding_ready": emb_ready,
        }

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        system: str | None = None,
        model: str | None = None,
    ) -> str:
        """``model`` pins one specific identifier; None (the default) keeps the
        DB-driven resolver, which is what every existing caller wants."""
        from app.services.runtime_model_resolver import (
            get_spark_vllm_runtime,
            invalidate_and_reprobe,
            session_scope,
        )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        llm_model = model or await self._resolve_llm_model()

        async def _post(model_name: str) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                return await cli.post(
                    f"{self.llm_url}/chat/completions",
                    json={
                        "model": model_name,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        # Qwen3 thinking mode returns content=null; disable it.
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )

        try:
            resp = await _post(llm_model)
            # 404 from vLLM == model name mismatch (recipe swap). Re-probe + retry once.
            if resp.status_code == 404:
                logger.warning(
                    "spark_client: 404 for model %r — re-probing /v1/models", llm_model
                )
                async with session_scope() as session:
                    runtime = await get_spark_vllm_runtime(session)
                    if runtime is not None:
                        fresh = await invalidate_and_reprobe(session, runtime.slug)
                        if fresh and fresh != llm_model:
                            logger.info(
                                "spark_client: retrying with refreshed model %r", fresh
                            )
                            resp = await _post(fresh)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if content is None:
                # Fallback: extract from reasoning field if thinking leaked through
                content = resp.json()["choices"][0]["message"].get("reasoning", "") or ""
            return content
        except httpx.HTTPError as e:
            raise SparkUnreachableError(
                f"Spark vLLM at {self.llm_url} not reachable: {e}"
            ) from e

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            try:
                resp = await cli.post(
                    f"{self.embedding_url}/embeddings",
                    json={"model": self.embedding_model, "input": text},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
            except httpx.HTTPError as e:
                raise SparkUnreachableError(
                    f"Spark embeddings at {self.embedding_url} not reachable: {e}"
                ) from e

    async def is_available(self) -> bool:
        """Bounded probe, part of the EmbeddingProvider contract. Never raises."""
        import asyncio

        try:
            await asyncio.wait_for(self.embed("ping"), timeout=2.0)
            return True
        except Exception:  # noqa: BLE001 — a probe reports, it does not raise
            return False

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Single batched call for better throughput."""
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            try:
                resp = await cli.post(
                    f"{self.embedding_url}/embeddings",
                    json={"model": self.embedding_model, "input": texts},
                )
                resp.raise_for_status()
                return [d["embedding"] for d in resp.json()["data"]]
            except httpx.HTTPError as e:
                raise SparkUnreachableError(
                    f"Spark embeddings at {self.embedding_url} not reachable: {e}"
                ) from e
