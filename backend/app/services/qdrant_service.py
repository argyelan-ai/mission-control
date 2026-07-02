"""Qdrant Service — Vektorsuche fuer das 3-stufige Memory-System.

Phase 3 (2026-04-11): 3 getrennte Collections pro Memory-Layer:

- memory_semantic: wiederverwendbares Wissen (knowledge, reference, research).
  Global + board-scoped, keine Recency-Bevorzugung.
- memory_agent: Agent-private Lessons + Peer-Lessons. Immer gefiltert nach
  agent_id im payload.
- memory_episodic: zeitgebundene Events (journal, weekly_review, insight,
  task_log). Recency-Score via created_at im payload.

Payload-Schema (pro Point):
    memory_id: UUID (DB primary key, string)
    memory_type: str  (knowledge, lesson, journal, ...)
    agent_id: str | None
    board_id: str | None
    title: str | None
    content_preview: str (erste 500 Chars)
    created_at: float (unix timestamp, fuer recency boost)
    tags: list[str]

Usage:
    from app.services.qdrant_service import qdrant_service
    await qdrant_service.ensure_collections()
    await qdrant_service.ensure_payload_indexes()  # MEM-04: self-healing payload indexes
    await qdrant_service.upsert("semantic", memory_id=..., vector=..., payload=...)
    hits = await qdrant_service.query("semantic", vector=..., top_k=5, filters={"board_id": ...})
"""
import asyncio
import logging
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import Settings

logger = logging.getLogger("mc.qdrant")

EMBED_DIM = 768

LAYER_COLLECTIONS = {
    "semantic": "memory_semantic",
    "agent": "memory_agent",
    "episodic": "memory_episodic",
}


class QdrantService:
    def __init__(self):
        self._client: Optional[AsyncQdrantClient] = None
        self._init_lock = asyncio.Lock()
        self._collections_ready = False
        self._settings = Settings()

    async def _get_client(self) -> AsyncQdrantClient:
        if self._client is None:
            self._client = AsyncQdrantClient(
                host=self._settings.qdrant_host,
                port=self._settings.qdrant_port,
            )
        return self._client

    async def ensure_collections(self) -> None:
        """Create collections if not exist. Idempotent, double-checked locking."""
        if self._collections_ready:
            return  # fast path — no lock contention on hot path
        async with self._init_lock:
            if self._collections_ready:
                return
            client = await self._get_client()
            existing = {c.name for c in (await client.get_collections()).collections}
            for layer, coll_name in LAYER_COLLECTIONS.items():
                if coll_name in existing:
                    continue
                logger.info("Creating Qdrant collection: %s", coll_name)
                await client.create_collection(
                    collection_name=coll_name,
                    vectors_config=qmodels.VectorParams(
                        size=EMBED_DIM,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
                await client.create_payload_index(
                    collection_name=coll_name,
                    field_name="memory_id",
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
                if layer == "agent":
                    await client.create_payload_index(
                        collection_name=coll_name,
                        field_name="agent_id",
                        field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    )
                if layer in ("semantic", "episodic"):
                    await client.create_payload_index(
                        collection_name=coll_name,
                        field_name="board_id",
                        field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    )
            self._collections_ready = True

    async def ensure_payload_indexes(self) -> None:
        """MEM-04: idempotent — ensure agent_id + board_id keyword indexes
        exist on all three memory collections (memory_semantic, memory_agent,
        memory_episodic). Adding a duplicate index is a no-op in Qdrant.

        Why this is separate from ensure_collections():
        ensure_collections() only runs the per-layer index code when CREATING
        a new collection (`if coll_name in existing: continue` at line 73).
        Existing collections that pre-date the index code never received it.
        This method walks the full {collection × field} matrix on EVERY
        startup so partial-index state self-heals.

        Source: qdrant_client.AsyncQdrantClient.create_payload_index
        supports wait=True since v1.7 — returns ack only after the index
        is built.
        """
        await self.ensure_collections()  # idempotent fast-path
        client = await self._get_client()
        DESIRED = [
            ("memory_semantic", "agent_id"),
            ("memory_semantic", "board_id"),
            ("memory_agent",    "agent_id"),
            ("memory_agent",    "board_id"),
            ("memory_episodic", "agent_id"),
            ("memory_episodic", "board_id"),
        ]
        for coll, field in DESIRED:
            try:
                await client.create_payload_index(
                    collection_name=coll,
                    field_name=field,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            except Exception as e:
                # Pre-existing index OR transient failure — both safe to log+continue.
                # Qdrant returns 200 with "already exists" for idempotent re-creates,
                # but the client lib may surface this as an exception in some versions.
                logger.debug(
                    "Payload index %s.%s already present or error: %s",
                    coll, field, e,
                )

    async def upsert(
        self,
        layer: str,
        memory_id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        """Upsert single point. Uses memory_id as the point-ID too (deterministic)."""
        coll = LAYER_COLLECTIONS.get(layer)
        if not coll:
            raise ValueError(f"Unknown layer: {layer}")
        await self.ensure_collections()
        client = await self._get_client()
        await client.upsert(
            collection_name=coll,
            points=[
                qmodels.PointStruct(
                    id=memory_id,
                    vector=vector,
                    payload={**payload, "memory_id": memory_id},
                )
            ],
        )

    async def delete(self, layer: str, memory_id: str) -> None:
        coll = LAYER_COLLECTIONS.get(layer)
        if not coll:
            return
        try:
            client = await self._get_client()
            await client.delete(
                collection_name=coll,
                points_selector=qmodels.PointIdsList(points=[memory_id]),
            )
        except Exception as e:
            logger.warning("Qdrant delete failed for %s/%s: %s", layer, memory_id, e)

    async def query(
        self,
        layer: str,
        vector: list[float],
        top_k: int = 5,
        board_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> list[dict]:
        """Vector search in one layer.

        Filter semantics:
        - semantic: optional board_id restricts to that board + globals
        - agent: agent_id MUST be set (private layer)
        - episodic: optional board_id

        Returns list of {memory_id, score, payload} dicts, sorted by score desc.
        """
        coll = LAYER_COLLECTIONS.get(layer)
        if not coll:
            raise ValueError(f"Unknown layer: {layer}")

        filters = None
        must = []
        if layer == "agent":
            if not agent_id:
                raise ValueError("agent layer query requires agent_id")
            must.append(qmodels.FieldCondition(
                key="agent_id",
                match=qmodels.MatchValue(value=agent_id),
            ))
        if board_id and layer in ("semantic", "episodic"):
            must.append(qmodels.FieldCondition(
                key="board_id",
                match=qmodels.MatchValue(value=board_id),
            ))
        if must:
            filters = qmodels.Filter(must=must)

        client = await self._get_client()
        # Phase C (2026-04-11): fuer episodic layer mehr holen um Recency-
        # Boost anwenden zu koennen (Re-Rank auf Client-Seite).
        search_limit = top_k * 3 if layer == "episodic" else top_k
        try:
            response = await client.query_points(
                collection_name=coll,
                query=vector,
                query_filter=filters,
                limit=search_limit,
                with_payload=True,
            )
            results = response.points
        except Exception as e:
            logger.warning("Qdrant query_points failed for %s: %s", layer, e)
            return []

        hits = [
            {
                "memory_id": str(r.id),
                "score": float(r.score),
                "payload": r.payload or {},
            }
            for r in results
        ]

        # Recency-Boost nur fuer episodic — time-sensitive events
        if layer == "episodic" and hits:
            import time as _time
            now = _time.time()
            # 30 days decay: frische Events bekommen voll, 30-Tage-alt halbiert
            DECAY_SEC = 30 * 24 * 3600
            RECENCY_WEIGHT = 0.25  # max 25% Boost fuer ganz frische Events
            for h in hits:
                created = float(h["payload"].get("created_at", 0) or 0)
                if created > 0:
                    age = max(0.0, now - created)
                    recency = max(0.0, 1.0 - (age / DECAY_SEC))  # 1.0 (jetzt) → 0.0 (30d+)
                    h["score"] = h["score"] + RECENCY_WEIGHT * recency
                    h["_recency_boost"] = RECENCY_WEIGHT * recency
            # Re-sort und auf top_k truncaten
            hits.sort(key=lambda h: h["score"], reverse=True)
            hits = hits[:top_k]

        return hits

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None


qdrant_service = QdrantService()
