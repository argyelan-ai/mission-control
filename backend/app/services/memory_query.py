"""Memory Query Helper — shared core for user- and agent-scoped endpoints.

Extracted from `routers/memory.py` and `routers/agent_scoped.py` (Phase 3),
so the query flow (embed → Qdrant → hybrid fallback) is maintained in one
place (I3 from code review 2026-04-11).

Both routers stay thin:
    results = await run_memory_query(session, query, layers, top_k, agent_id, board_id)

Semantics:
- layers: list from {"semantic","agent","episodic"}, other values are ignored.
- agent layer is only queried if agent_id is set (otherwise []).
- Embedding fail → keyword fallback via ILIKE, only on memory_types matching
  the layer (layer_for).
- Output is idempotent: same query + same index → same order.
"""
import logging
from typing import Optional

from sqlmodel import select, or_ as sql_or
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.memory import BoardMemory

logger = logging.getLogger("mc.memory_query")

VALID_LAYERS = {"semantic", "agent", "episodic"}


class InvalidQueryError(ValueError):
    """Raised when query payload is empty or layers are all invalid."""


async def run_memory_query(
    session: AsyncSession,
    query: str,
    layers: list[str],
    top_k: int = 5,
    agent_id: Optional[str] = None,
    board_id: Optional[str] = None,
) -> dict:
    """Hybrid vector/keyword search across the 3 memory layers.

    Returns:
        {
            "query": str,
            "agent_id": str | None,
            "board_id": str | None,
            "fallback": bool,          # True on keyword fallback
            "results": {
                "semantic": [hit, ...],
                "agent":    [hit, ...],
                "episodic": [hit, ...],
            },
        }

    Raises:
        InvalidQueryError: if query is empty or top_k is invalid or no
                           layers match.
    """
    if not query or not query.strip():
        raise InvalidQueryError("Leere query")
    if top_k < 1 or top_k > 50:
        raise InvalidQueryError("top_k muss zwischen 1 und 50 liegen")

    requested = [l for l in layers if l in VALID_LAYERS]
    if not requested:
        raise InvalidQueryError("Keine gueltigen layers (erlaubt: semantic/agent/episodic)")

    # Lazy import: embedding_service first (has no hard deps).
    # qdrant_service only AFTER the successful embed — so the keyword
    # fallback can also run without the qdrant-client package (tests).
    from app.services.embedding_service import embedding_service

    try:
        vec = await embedding_service.embed(query)
    except Exception as e:
        logger.warning("Memory query embedding failed, falling back to keyword: %s", e)
        return await _keyword_fallback(session, query, requested, top_k)

    from app.services.qdrant_service import qdrant_service

    results_by_layer: dict[str, list[dict]] = {}
    for layer in requested:
        try:
            if layer == "agent":
                if not agent_id:
                    results_by_layer[layer] = []
                    continue
                hits = await qdrant_service.query(
                    layer="agent",
                    vector=vec,
                    top_k=top_k,
                    agent_id=agent_id,
                )
            else:
                hits = await qdrant_service.query(
                    layer=layer,
                    vector=vec,
                    top_k=top_k,
                    board_id=board_id,
                )
            results_by_layer[layer] = [
                {
                    "memory_id": h["memory_id"],
                    "score": h["score"],
                    "title": h["payload"].get("title", ""),
                    "content_preview": h["payload"].get("content_preview", ""),
                    "memory_type": h["payload"].get("memory_type"),
                    "tags": h["payload"].get("tags", []),
                    "source": "qdrant",
                }
                for h in hits
            ]
        except Exception as e:
            logger.warning("Layer %s query failed: %s", layer, e)
            results_by_layer[layer] = []

    return {
        "query": query,
        "agent_id": agent_id,
        "board_id": board_id,
        "fallback": False,
        "results": results_by_layer,
    }


async def _keyword_fallback(
    session: AsyncSession,
    query: str,
    requested: list[str],
    top_k: int,
) -> dict:
    """ILIKE fallback when the embedding service is unreachable."""
    from app.services.memory_indexing import layer_for

    results_by_layer: dict[str, list[dict]] = {l: [] for l in requested}
    q = f"%{query.lower()}%"
    stmt = (
        select(BoardMemory)
        .where(
            sql_or(
                BoardMemory.title.ilike(q),  # type: ignore[union-attr]
                BoardMemory.content.ilike(q),  # type: ignore[union-attr]
            )
        )
        .limit(top_k * len(requested))
    )
    rows = (await session.exec(stmt)).all()
    for row in rows:
        layer = layer_for(row)
        if layer and layer in requested and len(results_by_layer[layer]) < top_k:
            results_by_layer[layer].append({
                "memory_id": str(row.id),
                "score": 0.0,
                "title": row.title or "",
                "content_preview": (row.content or "")[:500],
                "memory_type": row.memory_type,
                "tags": row.tags or [],
                "source": "keyword_fallback",
            })
    return {
        "query": query,
        "fallback": True,
        "results": results_by_layer,
    }
