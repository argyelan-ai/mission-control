"""Memory-Query Helper — gemeinsamer Core fuer user- und agent-scoped Endpoints.

Extrahiert aus `routers/memory.py` und `routers/agent_scoped.py` (Phase 3),
damit Query-Flow (Embed → Qdrant → Hybrid-Fallback) an einer Stelle gepflegt
wird (I3 aus Code-Review 2026-04-11).

Beide Router bleiben duenn:
    results = await run_memory_query(session, query, layers, top_k, agent_id, board_id)

Semantik:
- layers: Liste aus {"semantic","agent","episodic"}, andere Werte werden ignoriert.
- agent layer wird nur abgefragt wenn agent_id gesetzt (sonst []).
- Embedding-Fail → Keyword-Fallback via ILIKE, nur auf memory_types die zum
  Layer passen (layer_for).
- Output ist idempotent: gleicher Query + gleicher Index → gleiche Reihenfolge.
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
    """Hybrid Vektor-/Keyword-Suche ueber die 3 Memory-Layer.

    Returns:
        {
            "query": str,
            "agent_id": str | None,
            "board_id": str | None,
            "fallback": bool,          # True bei Keyword-Fallback
            "results": {
                "semantic": [hit, ...],
                "agent":    [hit, ...],
                "episodic": [hit, ...],
            },
        }

    Raises:
        InvalidQueryError: wenn query leer oder top_k ungueltig oder
                           keine layers matchen.
    """
    if not query or not query.strip():
        raise InvalidQueryError("Leere query")
    if top_k < 1 or top_k > 50:
        raise InvalidQueryError("top_k muss zwischen 1 und 50 liegen")

    requested = [l for l in layers if l in VALID_LAYERS]
    if not requested:
        raise InvalidQueryError("Keine gueltigen layers (erlaubt: semantic/agent/episodic)")

    # Lazy-Import: embedding_service zuerst (hat keine harten Deps).
    # qdrant_service erst NACH dem erfolgreichen embed — so kann der
    # Keyword-Fallback auch ohne qdrant-client package laufen (Tests).
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
    """ILIKE-Fallback wenn Embedding-Service nicht erreichbar."""
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
