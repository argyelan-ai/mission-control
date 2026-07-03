import hashlib
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, func

from app.auth import require_user
from app.database import get_session
from app.models.memory import BoardMemory
from app.redis_client import RedisKeys
from app.services.sse import make_sse_response
from app.utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["memory"])


# ── Phase 5 MSY-03: attachment helpers + validators ─────────────────────────

# Allowlist of MIME types accepted by the attachment uploader (D-12).
# SVG deliberately excluded (XSS via <script>); risk-acceptable for v0.5.
_ALLOWED_MIMES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
})
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file (D-12)
_MAX_FILES_PER_MEMORY = 5  # cap per BoardMemory entry (D-12)


def _attachments_root() -> str:
    """Phase 5 MSY-03: HOME_HOST resolver for the attachments directory.

    NEVER ``expanduser('~')`` standalone (memory feedback rule
    ``feedback_home_host_pattern.md``). The chain is:
    ``HOME_HOST`` env-var (set on Docker containers via the host-side
    docker-compose mount) → ``HOME`` env-var → ``expanduser('~')`` last
    resort. Returns ``${HOME_HOST}/.mc/attachments``.
    """
    home_host = os.environ.get("HOME_HOST") or os.environ.get("HOME") or os.path.expanduser("~")
    return f"{home_host}/.mc/attachments"


def _entry_attachment_dir(entry: BoardMemory) -> str:
    """Per-entry attachment directory: ``{root}/{board_id|_global}/{memory_id}``.

    Global / agent-only entries (board_id is None) live under ``_global``.
    """
    board_segment = str(entry.board_id) if entry.board_id else "_global"
    return os.path.join(_attachments_root(), board_segment, str(entry.id))


# ── Phase 5 MSY-02: hash-dedup helpers ──────────────────────────────────────
# SHA-256 of normalized title+content. Matches the migration 0091 backfill
# formula EXACTLY (single source of truth for the normalization rule):
#   normalized = " ".join(f"{title or ''}\n{content}".lower().split())
# Pitfall 2 (RESEARCH.md): include title to avoid cross-topic collisions
# where two boards happen to share an identical body like "Done.".


def _normalize_content_for_hash(title: str | None, content: str) -> str:
    """Phase 5 MSY-02 D-05 — normalization MUST match migration 0091 backfill."""
    raw = f"{title or ''}\n{content}"
    return " ".join(raw.lower().split())


def _content_hash(title: str | None, content: str) -> str:
    return hashlib.sha256(
        _normalize_content_for_hash(title, content).encode("utf-8")
    ).hexdigest()


# ── Board-scoped schemas (backwards compatible) ─────────────────────────────

class MemoryCreate(BaseModel):
    content: str
    title: str | None = None
    tags: list[str] = []
    memory_type: str = "knowledge"
    source: str = "user"
    is_pinned: bool = False


class MemoryUpdate(BaseModel):
    content: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    memory_type: str | None = None
    is_pinned: bool | None = None
    linked_ids: list[str] | None = None


# ── Knowledge schemas (global/agent scope) ──────────────────────────────────

class KnowledgeCreate(BaseModel):
    content: str
    title: str | None = None
    tags: list[str] = []
    memory_type: str = "knowledge"
    source: str = "user"
    board_id: str | None = None
    agent_id: str | None = None
    is_pinned: bool = False
    linked_ids: list[str] = []
    auto_generated: bool = False


class KnowledgeUpdate(BaseModel):
    content: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    memory_type: str | None = None
    is_pinned: bool | None = None
    linked_ids: list[str] | None = None


# ── Board-scoped endpoints (existing, unchanged behavior) ───────────────────

@router.get("/boards/{board_id}/memory")
async def list_memory(
    board_id: uuid.UUID,
    memory_type: str | None = Query(None),
    source: str | None = Query(None),
    pinned_only: bool = Query(False),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    query = select(BoardMemory).where(BoardMemory.board_id == board_id)
    if memory_type:
        query = query.where(BoardMemory.memory_type == memory_type)
    if source:
        query = query.where(BoardMemory.source == source)
    if pinned_only:
        query = query.where(BoardMemory.is_pinned == True)  # noqa: E712
    query = query.order_by(BoardMemory.is_pinned.desc(), BoardMemory.created_at.desc()).offset(offset).limit(limit)  # type: ignore[attr-defined]
    result = await session.exec(query)
    return result.all()


@router.get("/boards/{board_id}/memory/stream")
async def stream_memory(board_id: uuid.UUID, current_user=Depends(require_user)):
    return make_sse_response([RedisKeys.board_events(str(board_id))])


@router.post("/boards/{board_id}/memory", status_code=status.HTTP_201_CREATED)
async def create_memory(
    board_id: uuid.UUID,
    payload: MemoryCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    memory = BoardMemory(board_id=board_id, **payload.model_dump())
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    # Auto-Index in Qdrant (fail-soft)
    try:
        from app.services.memory_indexing import index_memory
        await index_memory(memory)
    except Exception:
        pass
    return memory


@router.patch("/boards/{board_id}/memory/{memory_id}")
async def update_memory(
    board_id: uuid.UUID,
    memory_id: uuid.UUID,
    payload: MemoryUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    memory = await session.get(BoardMemory, memory_id)
    if not memory or memory.board_id != board_id:
        raise HTTPException(status_code=404, detail="Memory not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(memory, k, v)
    memory.updated_at = utcnow()
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return memory


@router.delete("/boards/{board_id}/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    board_id: uuid.UUID,
    memory_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    memory = await session.get(BoardMemory, memory_id)
    if not memory or memory.board_id != board_id:
        raise HTTPException(status_code=404, detail="Memory not found")
    # Determine layer before delete so the Qdrant cleanup is targeted
    from app.services.memory_indexing import layer_for, delete_memory_index
    _layer = layer_for(memory)
    _mem_id = str(memory.id)
    await session.delete(memory)
    await session.commit()
    try:
        await delete_memory_index(_mem_id, layer=_layer)
    except Exception:
        pass


# ── Knowledge Base endpoints (global / agent / cross-board) ─────────────────

@router.get("/knowledge")
async def list_knowledge(
    memory_type: str | None = Query(None),
    source: str | None = Query(None),
    agent_id: str | None = Query(None),
    board_id: str | None = Query(None),
    auto_generated: bool | None = Query(None),
    pinned_only: bool = Query(False),
    search: str | None = Query(None),
    status: str | None = Query(None, description="Filter by status (draft/published/stale/archived)"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    scope: Literal["global", "board", "agent", "all"] | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """List all knowledge entries across all scopes with filters.

    The ``scope`` query parameter (Phase 5 MSY-05) constrains the result set
    by the scoping columns: ``global`` returns only entries where both
    ``board_id`` and ``agent_id`` are NULL, ``board`` keeps the existing
    ``board_id`` filter, ``agent`` keeps the existing ``agent_id`` filter,
    and ``all`` (or omitted) preserves the legacy unconstrained behaviour.
    Existing ``board_id`` / ``agent_id`` query params keep working
    independently — the frontend passes both ``scope=board`` and
    ``board_id=X`` together, and the backend respects each.
    """
    query = select(BoardMemory)
    if memory_type:
        query = query.where(BoardMemory.memory_type == memory_type)
    if source:
        query = query.where(BoardMemory.source == source)
    if agent_id:
        query = query.where(BoardMemory.agent_id == uuid.UUID(agent_id))
    if board_id:
        query = query.where(BoardMemory.board_id == uuid.UUID(board_id))
    if scope == "global":
        query = query.where(BoardMemory.board_id.is_(None))  # type: ignore[union-attr]
        query = query.where(BoardMemory.agent_id.is_(None))  # type: ignore[union-attr]
    elif scope == "board" and board_id:
        # board_id WHERE clause already applied above; this branch is a
        # no-op safety check (kept explicit so reviewers see the contract).
        pass
    elif scope == "agent" and agent_id:
        # agent_id WHERE clause already applied above; same safety check.
        pass
    # scope == "all" or None → no extra filter (current behaviour).
    # Phase 2: optional status filter (validated allowlist)
    _VALID_STATUS = {"draft", "published", "stale", "archived"}
    if status:
        if status not in _VALID_STATUS:
            raise HTTPException(
                status_code=422,
                detail=f"invalid status: must be one of {sorted(_VALID_STATUS)}",
            )
        query = query.where(BoardMemory.status == status)
    if auto_generated is not None:
        query = query.where(BoardMemory.auto_generated == auto_generated)
    if pinned_only:
        query = query.where(BoardMemory.is_pinned == True)  # noqa: E712
    if search:
        query = query.where(BoardMemory.content.ilike(f"%{search}%"))  # type: ignore[union-attr]
    query = query.order_by(BoardMemory.is_pinned.desc(), BoardMemory.created_at.desc()).offset(offset).limit(limit)  # type: ignore[attr-defined]
    result = await session.exec(query)
    return result.all()


@router.get("/knowledge/timeline")
async def knowledge_timeline(
    days: int = Query(7, le=90),
    agent_id: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Chronological view of journals and weekly reviews."""
    cutoff = utcnow() - timedelta(days=days)
    query = (
        select(BoardMemory)
        .where(BoardMemory.created_at >= cutoff)
        .where(BoardMemory.memory_type.in_(["journal", "weekly_review"]))  # type: ignore[union-attr]
    )
    if agent_id:
        query = query.where(BoardMemory.agent_id == uuid.UUID(agent_id))
    query = query.order_by(BoardMemory.created_at.desc())
    result = await session.exec(query)
    return result.all()


@router.get("/knowledge/stats")
async def knowledge_stats(
    agent_id: str | None = Query(None),
    board_id: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Knowledge statistics: count entries per type."""
    query = select(BoardMemory.memory_type, func.count().label("count")).group_by(BoardMemory.memory_type)
    if agent_id:
        query = query.where(BoardMemory.agent_id == uuid.UUID(agent_id))
    if board_id:
        query = query.where(BoardMemory.board_id == uuid.UUID(board_id))
    result = await session.exec(query)
    stats = {row[0]: row[1] for row in result.all()}
    total = sum(stats.values())
    return {"stats": stats, "total": total}


@router.get("/knowledge/{entry_id}")
async def get_knowledge_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Get single entry with resolved linked documents."""
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    linked = []
    if entry.linked_ids:
        for lid in entry.linked_ids:
            try:
                linked_entry = await session.get(BoardMemory, uuid.UUID(str(lid)))
                if linked_entry:
                    linked.append(linked_entry)
            except (ValueError, AttributeError):
                continue

    return {
        "entry": entry,
        "linked": linked,
    }


@router.post("/knowledge", status_code=status.HTTP_201_CREATED)
async def create_knowledge(
    payload: KnowledgeCreate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Create a knowledge entry (global, agent-scoped, or board-scoped).

    Phase 5 MSY-02 D-05: hash-dedup pre-INSERT. Compute SHA-256 of normalized
    title+content; if a row with the same content_hash already exists, log
    INFO + return the existing row instead of inserting a duplicate. The
    response status code stays 201 (FastAPI route default) but the body
    references the existing entry — clients see the same id on the second
    POST and can detect the dedup via the (idempotent) UUID match.
    """
    data = payload.model_dump()
    if data.get("board_id"):
        data["board_id"] = uuid.UUID(data["board_id"])
    else:
        data["board_id"] = None
    if data.get("agent_id"):
        data["agent_id"] = uuid.UUID(data["agent_id"])
    else:
        data["agent_id"] = None

    # Phase 5 MSY-02: hash-dedup silent skip
    h = _content_hash(data.get("title"), data["content"])
    existing = (
        await session.exec(
            select(BoardMemory).where(BoardMemory.content_hash == h).limit(1)
        )
    ).first()
    if existing:
        logger.info(
            "knowledge: hash-dedup hit for content_hash=%s; returning existing entry %s",
            h, existing.id,
        )
        return existing

    data["content_hash"] = h
    entry = BoardMemory(**data)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    try:
        from app.services.memory_indexing import index_memory
        await index_memory(entry)
    except Exception:
        pass
    # The merge_candidate_id may have been set by index_memory's cosine
    # check; refresh once more so the response body carries the updated value.
    try:
        await session.refresh(entry)
    except Exception:
        pass
    return entry


@router.patch("/knowledge/{entry_id}")
async def update_knowledge(
    entry_id: uuid.UUID,
    payload: KnowledgeUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(entry, k, v)
    entry.updated_at = utcnow()
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


@router.delete("/knowledge/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    # Phase 5 MSY-03 D-16: cascade attachment-directory delete.
    # Captured BEFORE session.delete() so we still have access to entry.attachments
    # + entry.board_id + entry.id for the filesystem cleanup.
    if entry.attachments:
        try:
            shutil.rmtree(_entry_attachment_dir(entry), ignore_errors=True)
        except Exception as e:  # pragma: no cover — defensive belt+braces
            logger.warning(
                "attachment cascade delete failed for entry %s: %s", entry.id, e,
            )
    from app.services.memory_indexing import layer_for, delete_memory_index
    _layer = layer_for(entry)
    _entry_id = str(entry.id)
    await session.delete(entry)
    await session.commit()
    try:
        await delete_memory_index(_entry_id, layer=_layer)
    except Exception:
        pass


# ── Memory Query (user-auth) — Semantic Search via Qdrant ───────────────────
# Phase 3/4, 2026-04-11: Mirrors /api/v1/agent/memory/query but with user-auth
# so the frontend /memory page can use search directly.


class MemoryQueryPayload(BaseModel):
    query: str
    layers: list[str] = ["semantic", "agent", "episodic"]
    top_k: int = 5
    agent_id: str | None = None
    board_id: str | None = None


@router.post("/memory/query")
async def user_memory_query(
    payload: MemoryQueryPayload,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """User-scoped Hybrid Memory Query (vector + keyword fallback).

    Thin wrapper around app.services.memory_query.run_memory_query — shares
    its core with the agent-scoped endpoint in agent_scoped.py.
    """
    from app.services.memory_query import run_memory_query, InvalidQueryError

    try:
        return await run_memory_query(
            session=session,
            query=payload.query,
            layers=payload.layers,
            top_k=payload.top_k,
            agent_id=payload.agent_id,
            board_id=payload.board_id,
        )
    except InvalidQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Phase 5 MSY-03: attachment endpoints ────────────────────────────────────
# Filesystem at ${HOME_HOST}/.mc/attachments/{board_id|_global}/{memory_id}/
# DB stores relative paths only (BoardMemory.attachments JSON, see
# Migration 0091). Inline image previews + auth-gated GET in /memory viewer.


@router.post("/knowledge/{entry_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    entry_id: uuid.UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase 5 MSY-03 D-13: upload an attachment to a knowledge entry.

    Validates: MIME allowlist (5 types) → 415, size cap 10 MB → 413,
    count cap 5 per entry → 400, path-traversal in filename → 400.
    Writes to ``${HOME_HOST}/.mc/attachments/{board_id|_global}/{memory_id}/{sha16}-{name}``.
    """
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")

    if file.content_type not in _ALLOWED_MIMES:
        raise HTTPException(415, f"MIME {file.content_type} not allowed")

    existing = entry.attachments or []
    if len(existing) >= _MAX_FILES_PER_MEMORY:
        raise HTTPException(400, f"Max {_MAX_FILES_PER_MEMORY} attachments per entry")

    contents = await file.read()
    if len(contents) > _MAX_BYTES:
        raise HTTPException(413, "File too large (max 10 MB)")

    # Path-traversal guard MUST run on the raw multipart filename BEFORE
    # ``os.path.basename`` strips the leading directory components — otherwise
    # ``../etc-passwd.png`` slips through as ``etc-passwd.png``. Pitfall 6 +
    # explicit operator-aware test (test_attachment_path_traversal_rejected).
    raw_name = file.filename or "file"
    if ".." in raw_name or "/" in raw_name or "\\" in raw_name:
        raise HTTPException(400, "Invalid filename")
    safe_orig = os.path.basename(raw_name)

    file_dir = _entry_attachment_dir(entry)
    os.makedirs(file_dir, exist_ok=True)

    sha = hashlib.sha256(contents).hexdigest()[:16]
    fname = f"{sha}-{safe_orig}"

    target = os.path.join(file_dir, fname)
    real_dir = os.path.realpath(file_dir)
    real_target = os.path.realpath(target)
    if not real_target.startswith(real_dir + os.sep):
        raise HTTPException(400, "Path escapes attachments root")

    with open(target, "wb") as f:
        f.write(contents)

    rel = os.path.relpath(target, _attachments_root())
    new_attachment = {
        "path": rel,
        "mime_type": file.content_type,
        "size_bytes": len(contents),
        "original_name": safe_orig,
    }
    # JSON-column reassignment (don't .append() — SQLAlchemy doesn't track
    # in-place mutation on Column(JSON)).
    entry.attachments = existing + [new_attachment]
    entry.updated_at = utcnow()
    session.add(entry)
    await session.commit()
    return new_attachment


@router.get("/knowledge/{entry_id}/attachments/{filename}")
async def get_attachment(
    entry_id: uuid.UUID,
    filename: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase 5 MSY-03 D-14: stream attachment with auth + path-traversal guard.

    NO direct static-mount per D-14 — every read goes through the auth
    middleware. ``Content-Type`` is taken from the stored mime_type
    (preserves frontend image MIME for inline rendering).
    """
    if ".." in filename:
        raise HTTPException(400, "Invalid filename")
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    if not entry.attachments:
        raise HTTPException(404, "No attachments")

    file_dir = _entry_attachment_dir(entry)
    real_dir = os.path.realpath(file_dir)
    target = os.path.realpath(os.path.join(real_dir, filename))
    if not target.startswith(real_dir + os.sep):
        raise HTTPException(400, "Path escapes attachments root")
    if not os.path.isfile(target):
        raise HTTPException(404, "File not found")

    # Find matching attachment metadata for mime_type
    rec = next(
        (
            a for a in entry.attachments
            if os.path.basename(a.get("path", "")) == filename
        ),
        None,
    )
    mime = rec.get("mime_type") if rec else "application/octet-stream"

    return FileResponse(
        target,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete(
    "/knowledge/{entry_id}/attachments/{filename}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_attachment(
    entry_id: uuid.UUID,
    filename: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase 5 MSY-03 D-15: remove a single attachment file + DB array entry.

    Path-traversal guarded the same way as GET. Filesystem error during
    ``os.remove`` is logged but not raised — DB state stays the source of
    truth so a stuck file does not block the entry from updating its
    attachments list.
    """
    if ".." in filename:
        raise HTTPException(400, "Invalid filename")
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    if not entry.attachments:
        raise HTTPException(404, "No attachments")

    file_dir = _entry_attachment_dir(entry)
    real_dir = os.path.realpath(file_dir)
    target = os.path.realpath(os.path.join(real_dir, filename))
    if not target.startswith(real_dir + os.sep):
        raise HTTPException(400, "Path escapes attachments root")

    if os.path.isfile(target):
        try:
            os.remove(target)
        except Exception as e:  # pragma: no cover — defensive belt+braces
            logger.warning("attachment delete fs error: %s", e)

    # Drop matching DB entry — JSON-column reassignment, not in-place mutation
    entry.attachments = [
        a for a in entry.attachments
        if os.path.basename(a.get("path", "")) != filename
    ]
    entry.updated_at = utcnow()
    session.add(entry)
    await session.commit()
    return


# ── Phase 5 MSY-02: MERGE-Badge user-confirm endpoints ──────────────────────
# Three actions land at the cosine-merge UX: (1) merge source → target,
# (2) keep both (clear the candidate flag), (3) mark unrelated (clear flag
# + tag for future-similarity suppression). All three are static-named under
# the existing ``/knowledge/{id}/...`` prefix so no router-ordering risk.
# Auth: require_user JWT (matches the rest of the /knowledge surface).


@router.post("/knowledge/{entry_id}/merge_into/{target_id}", status_code=status.HTTP_200_OK)
async def merge_into(
    entry_id: uuid.UUID,
    target_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase 5 MSY-02 D-06: merge SOURCE into TARGET.

    SOURCE content is appended to TARGET (separator: ``\\n\\n---\\n\\n``).
    SOURCE tags + linked_ids are union-merged into TARGET. SOURCE id is
    appended to TARGET.linked_ids for back-reference. SOURCE row + its
    Qdrant vector are deleted. Target's updated_at advances.

    Idempotent on the SOURCE delete (already-deleted ids return 404). The
    Qdrant cleanup is fail-soft — see ``delete_memory_index`` semantics.
    """
    if entry_id == target_id:
        raise HTTPException(400, "Cannot merge entry into itself")
    source = await session.get(BoardMemory, entry_id)
    if not source:
        raise HTTPException(404, "Source entry not found")
    target = await session.get(BoardMemory, target_id)
    if not target:
        raise HTTPException(404, "Target entry not found")

    target.content = (target.content or "") + "\n\n---\n\n" + (source.content or "")
    target.tags = list({*(target.tags or []), *(source.tags or [])})
    target.linked_ids = list(
        {*(target.linked_ids or []), *(source.linked_ids or []), str(source.id)}
    )
    target.updated_at = utcnow()
    session.add(target)

    from app.services.memory_indexing import layer_for, delete_memory_index
    src_layer = layer_for(source)
    src_id = str(source.id)
    await session.delete(source)
    await session.commit()
    try:
        await delete_memory_index(src_id, layer=src_layer)
    except Exception:
        pass
    return {"merged_into": str(target_id), "deleted": src_id}


@router.post("/knowledge/{entry_id}/keep_both", status_code=status.HTTP_200_OK)
async def keep_both(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase 5 MSY-02 D-06: clear merge_candidate_id, keep both entries."""
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    entry.merge_candidate_id = None
    entry.updated_at = utcnow()
    session.add(entry)
    await session.commit()
    return {"kept_both": str(entry_id)}


@router.post("/knowledge/{entry_id}/unrelated", status_code=status.HTTP_200_OK)
async def mark_unrelated(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Phase 5 MSY-02 D-06: clear merge_candidate_id + tag for suppression.

    Adds tag ``dedup:reviewed:unrelated`` so future similarity flags can
    detect this user verdict and avoid re-prompting on the same pair (the
    flag itself is informational; the suppression heuristic lives in plan
    05-05's runtime call site if it picks up the tag).
    """
    entry = await session.get(BoardMemory, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    entry.merge_candidate_id = None
    entry.tags = list({*(entry.tags or []), "dedup:reviewed:unrelated"})
    entry.updated_at = utcnow()
    session.add(entry)
    await session.commit()
    return {"marked_unrelated": str(entry_id)}
