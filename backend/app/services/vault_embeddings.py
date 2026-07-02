"""Vault Embeddings adapter — Spark DGX text-embedding-nomic-embed-text-v1.5
→ Qdrant collection `memory_vault`. Fail-soft on DGX outage (logs only,
no exception bubbled up — index still works without embeddings).

M.2 (2026-05-14): real DGX → Qdrant wiring replaces the M.1 no-op stub
that lived inline in ``app/main.py``. The constructor still takes any
``dgx_client`` (needs ``async embed(text) -> list[float]``) and any
``qdrant_client`` (raw ``qdrant_client.AsyncQdrantClient`` API — uses
``upsert(collection_name=, points=[...])``, ``get_collections()``,
``create_collection(...)``). In production we pass:

- ``dgx_client = embedding_service`` (singleton in ``embedding_service.py``)
- ``qdrant_client = await qdrant_service._get_client()`` (raw AsyncQdrantClient)

The ``memory_vault`` collection is **separate** from the 3-layer
``memory_{semantic,agent,episodic}`` collections used by the legacy
board_memory system (qdrant_service.py). It is auto-created on the
first upsert if missing (768-dim, Cosine).
"""

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

import frontmatter

logger = logging.getLogger("mc.vault_embeddings")

EMBED_DIM = 768


class VaultEmbeddings:
    def __init__(self, dgx_client: Any, qdrant_client: Any, collection: str = "memory_vault"):
        self.dgx = dgx_client
        self.qdrant = qdrant_client
        self.collection = collection
        # Cache so we don't re-issue get_collections() on every upsert.
        # Reset to False if a Qdrant call fails so the next call retries.
        self._collection_ready = False

    async def _ensure_collection(self) -> Optional[str]:
        """Create ``memory_vault`` collection if missing. Returns error string
        on failure (so caller can fold it into the structured response)."""
        if self._collection_ready:
            return None
        try:
            # Lazy import keeps the unit-test mock path clean — tests inject
            # a MagicMock for qdrant_client and don't need qdrant_client.http.
            from qdrant_client.http import models as qmodels
        except Exception as e:  # pragma: no cover — env without qdrant client
            return f"qdrant_client import failed: {e}"

        try:
            existing_resp = await self.qdrant.get_collections()
            existing = {c.name for c in existing_resp.collections}
            if self.collection not in existing:
                logger.info("Creating Qdrant collection: %s (dim=%d)", self.collection, EMBED_DIM)
                await self.qdrant.create_collection(
                    collection_name=self.collection,
                    vectors_config=qmodels.VectorParams(
                        size=EMBED_DIM,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
            self._collection_ready = True
            return None
        except Exception as e:
            return str(e)

    async def upsert(self, file_path: Path, post: frontmatter.Post, vault_path: Path) -> dict[str, Any]:
        rel = str(file_path.relative_to(vault_path))

        # 1) Embed via DGX (Spark LM Studio). Fail-soft.
        try:
            vector = await self.dgx.embed(post.content)
        except Exception as e:
            logger.warning("DGX embed failed for %s: %s — skipping (fail-soft)", rel, e)
            return {"ok": False, "error": f"DGX: {e}", "kind": "dgx_failure"}

        # 2) Ensure collection exists (cheap once cached).
        ensure_err = await self._ensure_collection()
        if ensure_err is not None:
            logger.error("Qdrant ensure_collection failed for %s: %s", self.collection, ensure_err)
            return {"ok": False, "error": f"Qdrant: {ensure_err}", "kind": "qdrant_failure"}

        # 3) Upsert. Point-ID is deterministic from vault-relative path so
        # re-indexing the same file overwrites the prior vector.
        point_id = hashlib.sha256(rel.encode()).hexdigest()[:32]
        try:
            from qdrant_client.http import models as qmodels  # lazy
            point = qmodels.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "path": rel,
                    "id": str(post.metadata.get("id", "")),
                    "agent": post.metadata.get("agent", ""),
                    "type": post.metadata.get("type", ""),
                    "tags": post.metadata.get("tags", []),
                },
            )
        except Exception:
            # Test/mocks: fall back to a plain dict, which the AsyncQdrantClient
            # also accepts and which existing unit tests assert against.
            point = {
                "id": point_id,
                "vector": vector,
                "payload": {
                    "path": rel,
                    "id": str(post.metadata.get("id", "")),
                    "agent": post.metadata.get("agent", ""),
                    "type": post.metadata.get("type", ""),
                    "tags": post.metadata.get("tags", []),
                },
            }

        try:
            await self.qdrant.upsert(
                collection_name=self.collection,
                points=[point],
            )
        except Exception as e:
            # Reset cache so transient outages retry collection-existence next call.
            self._collection_ready = False
            logger.error("Qdrant upsert failed for %s: %s", rel, e)
            return {"ok": False, "error": f"Qdrant: {e}", "kind": "qdrant_failure"}

        return {"ok": True, "point_id": point_id}

    async def delete(self, rel_path: str) -> dict[str, Any]:
        """Remove the vector for a vault path. Fail-soft like upsert — a
        missing Qdrant or non-existent point is not an error from the
        caller's perspective (the note is being deleted regardless).

        Point IDs are derived deterministically from the vault-relative path
        (same scheme as upsert), so callers don't need to know the id.
        """
        point_id = hashlib.sha256(rel_path.encode()).hexdigest()[:32]
        try:
            await self.qdrant.delete(
                collection_name=self.collection,
                points_selector=[point_id],
            )
        except Exception as e:
            # Don't surface this as a hard failure — vault delete must succeed
            # even if Qdrant is offline. Stale vector cleanup is recoverable
            # later via a re-embed pass.
            self._collection_ready = False
            logger.warning("Qdrant delete failed for %s (non-fatal): %s", rel_path, e)
            return {"ok": False, "error": f"Qdrant: {e}", "kind": "qdrant_failure"}

        return {"ok": True, "point_id": point_id}
