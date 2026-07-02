"""Memory-Indexing — Auto-Embedding und Qdrant-Upsert bei Memory-Create.

Wird aufgerufen von:
- POST /api/v1/knowledge (routers/memory.py)
- POST /api/v1/boards/{id}/memory (routers/memory.py)
- POST /api/v1/agent/memory (routers/agent_scoped.py)

Fail-soft: Wenn Spark oder Qdrant down sind, wird ein WARNING geloggt aber
der DB-Insert geht trotzdem durch. Backfill kann spaeter fehlende Embeddings
nachziehen.
"""
import logging
from typing import Optional

from app.models.memory import BoardMemory

# NOTE: embedding_service und qdrant_service werden lazy importiert in
# index_memory() und delete_memory_index(), weil qdrant_client nur im Docker
# Container installiert ist. layer_for() funktioniert ohne Qdrant-Import und
# kann lokal in Tests genutzt werden.

logger = logging.getLogger("mc.memory_indexing")

# Mapping memory_type → Layer
SEMANTIC_TYPES = {"knowledge", "reference", "research"}
EPISODIC_TYPES = {"journal", "weekly_review", "insight", "task_log"}
AGENT_TYPES = {"lesson"}


async def _enqueue_embedding_retry(memory_id, attempt: int = 1) -> bool:
    """Pusht den Memory-Eintrag in die Phase-5 MSY-04 Retry-Queue.

    Lazy-Import um Zirkularitaet zu vermeiden — embedding_retry importiert
    selbst Lazy aus diesem Modul (layer_for) im _process_one-Pfad.

    Returns True wenn enqueued, False bei Cap-Hit oder Redis-Fehler. Der Caller
    (index_memory except branch) behandelt False als "BoardMemory bleibt im DB,
    aber kein weiterer Retry getrackt" — fail-soft to fail-soft.
    """
    from app.services.embedding_retry import enqueue
    return await enqueue(memory_id, attempt=attempt)


async def _find_merge_candidate(
    layer: str,
    vector: list[float],
    board_id,
    agent_id,
    threshold: float,
) -> Optional[str]:
    """Phase 5 MSY-02 D-06: nearest-neighbour cosine-similarity check.

    Returns the ``memory_id`` (str) of the highest-scoring Qdrant hit if the
    score is at or above ``threshold`` (cosine ≥ 0.9 default), else None.

    qdrant_service.query already configures Distance.COSINE at collection
    create (qdrant_service.py:81), so the ``score`` field on the returned
    hit IS the cosine similarity in the [-1, 1] range. No re-computation
    needed.

    Catches Qdrant exceptions (returns None) — MSY-04 fail-soft alignment:
    a Qdrant outage during cosine-merge detection MUST NOT block the write
    path. The caller (index_memory) treats None as "no merge candidate"
    and proceeds with the normal upsert.
    """
    from app.services.qdrant_service import qdrant_service

    try:
        if layer == "agent" and agent_id:
            hits = await qdrant_service.query(
                layer="agent",
                vector=vector,
                top_k=1,
                agent_id=str(agent_id),
            )
        else:
            hits = await qdrant_service.query(
                layer=layer,
                vector=vector,
                top_k=1,
                board_id=str(board_id) if board_id else None,
            )
    except Exception as e:
        logger.warning("merge_candidate qdrant query failed: %s", e)
        return None

    if not hits:
        return None
    top = hits[0]
    score = float(top.get("score", 0.0))
    if score >= threshold:
        return str(top["memory_id"])
    return None


def layer_for(memory: BoardMemory) -> Optional[str]:
    """Leitet den Memory-Layer aus Typ + Scope ab.

    - lesson mit agent_id → agent
    - SEMANTIC_TYPES → semantic
    - EPISODIC_TYPES → episodic
    - alles andere → None (kein Auto-Index)
    """
    mtype = (memory.memory_type or "").lower()
    if mtype in AGENT_TYPES and memory.agent_id:
        return "agent"
    if mtype in SEMANTIC_TYPES:
        return "semantic"
    if mtype in EPISODIC_TYPES:
        return "episodic"
    return None


async def index_memory(memory: BoardMemory) -> Optional[str]:
    """Erzeugt Embedding + Qdrant Upsert. Returnt den genutzten Layer oder None.

    Fail-soft: Exceptions werden geloggt, nicht weitergegeben.
    """
    layer = layer_for(memory)
    if layer is None:
        return None

    text_parts = []
    if memory.title:
        text_parts.append(memory.title)
    if memory.content:
        text_parts.append(memory.content)
    text = "\n".join(text_parts).strip()
    if not text:
        return None

    # Lazy-Import (Qdrant-Modul nur im Docker-Container installiert)
    from app.services.embedding_service import embedding_service
    from app.services.qdrant_service import qdrant_service

    try:
        vec = await embedding_service.embed(text)
    except Exception as e:
        logger.warning(
            "Embedding failed for memory %s (layer=%s): %s — DB-Insert bleibt, Qdrant uebersprungen, retry enqueued",
            memory.id, layer, e,
        )
        # Phase 5 MSY-04: Retry-Queue statt nur log+drop. Cap-overflow wird
        # innerhalb _enqueue_embedding_retry behandelt (WARN + skip; Memory
        # bleibt persistiert, nur eben ohne Retry-Tracking).
        try:
            await _enqueue_embedding_retry(memory.id, attempt=1)
        except Exception as ee:
            logger.warning("Embedding retry enqueue failed for %s: %s", memory.id, ee)
        return None

    # Phase 5 MSY-02: cosine merge-candidate flagging.
    # Runs AFTER embed success, BEFORE qdrant.upsert — the new memory's
    # embedding is checked against the nearest existing entry in the same
    # layer; if cosine ≥ settings.memory_merge_threshold (default 0.9),
    # we flag the new entry with merge_candidate_id pointing at the hit.
    # The caller in routers/memory.py has already committed the row, so we
    # open a fresh AsyncSession to persist the update — same fail-soft
    # discipline as the embed path (any failure logs WARN, never raises).
    try:
        from app.config import settings
        candidate_id = await _find_merge_candidate(
            layer=layer,
            vector=vec,
            board_id=memory.board_id,
            agent_id=memory.agent_id,
            threshold=settings.memory_merge_threshold,
        )
        if candidate_id and str(candidate_id) != str(memory.id):
            from app.database import engine
            from sqlmodel.ext.asyncio.session import AsyncSession as _Session
            import uuid as _uuid
            async with _Session(engine, expire_on_commit=False) as _s:
                m = await _s.get(BoardMemory, memory.id)
                if m and m.merge_candidate_id is None:
                    m.merge_candidate_id = _uuid.UUID(candidate_id)
                    _s.add(m)
                    await _s.commit()
                    # Reflect the update on the caller's in-memory object so
                    # downstream code sees the candidate id without a refetch.
                    memory.merge_candidate_id = m.merge_candidate_id
                    logger.info(
                        "merge_candidate flagged: memory %s → candidate %s (score >= %.2f)",
                        memory.id, candidate_id, settings.memory_merge_threshold,
                    )
    except Exception as e:
        logger.warning("merge_candidate flagging failed for %s: %s", memory.id, e)

    payload = {
        "memory_type": memory.memory_type,
        "agent_id": str(memory.agent_id) if memory.agent_id else None,
        "board_id": str(memory.board_id) if memory.board_id else None,
        "title": memory.title or "",
        "content_preview": (memory.content or "")[:500],
        "created_at": memory.created_at.timestamp() if memory.created_at else 0.0,
        "tags": memory.tags or [],
    }

    try:
        await qdrant_service.upsert(
            layer=layer,
            memory_id=str(memory.id),
            vector=vec,
            payload=payload,
        )
    except Exception as e:
        logger.warning(
            "Qdrant upsert failed for memory %s (layer=%s): %s",
            memory.id, layer, e,
        )
        return None

    logger.debug("Indexed memory %s in layer %s", memory.id, layer)
    return layer


async def delete_memory_index(memory_id: str, layer: Optional[str] = None) -> None:
    """Entfernt Qdrant-Eintrag. Wenn layer unbekannt, versucht alle drei."""
    # Lazy-Import (siehe index_memory)
    from app.services.qdrant_service import qdrant_service
    layers = [layer] if layer else ["semantic", "agent", "episodic"]
    for layer_name in layers:
        try:
            await qdrant_service.delete(layer_name, memory_id)
        except Exception as e:
            logger.debug("Delete from %s failed (ok if not present): %s", layer_name, e)
