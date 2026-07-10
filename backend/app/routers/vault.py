"""Vault REST API — read-only in M.1 (admin rebuild + list + search + note).

Write routes (POST /agent/vault/note) added in M.2 T7.
WebSocket streams (WS /vault/stream, WS /vault/voice-highlight) added in M.4 T2.

Patterns adapted from llmwiki (Apache 2.0) — see NOTICE.

Auth adaptations vs plan template:
- Plan used `require_admin_user` → actual: `require_role(Role.ADMIN)` from app.auth
- Plan used `get_current_agent` → actual: `require_agent` from app.auth (already
  called internally by `require_scope`, so no separate Depends needed for agent-id)
- WS auth: mirrors cli_plugins.py pattern — token via query param, JWT decode via jose
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import unquote

import frontmatter as fm_lib
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pathlib import Path as _PathLib

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_agent, require_role
from app.config import settings
from app.database import get_session
from app.helpers.vault_frontmatter import FrontmatterError, parse_frontmatter, validate_frontmatter
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.task import Task
from app.redis_client import get_redis
from app.scopes import Scope, require_scope
from app.services.vault_graph import build_graph
from app.services.vault_cache import (
    bump_graph_version,
    get_cached_graph,
    get_graph_version,
    params_hash,
    publish_vault_event,
    set_cached_graph,
)
from app.utils import slugify

logger = logging.getLogger("mc.vault_routes")

# ── Helper ────────────────────────────────────────────────────────────────────


def _safe_path(rel_path_encoded: str, vault: Path) -> Path:
    """URL-decode path and verify it stays within vault.

    Rejects:
    - paths containing '..'
    - paths starting with '/'
    - resolved paths that escape the vault root (traversal)

    Raises HTTPException(400) on any violation.
    """
    rel = unquote(rel_path_encoded)
    if ".." in rel or rel.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")
    full = (vault / rel).resolve()
    try:
        full.relative_to(vault.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="path traversal blocked",
        )
    return full


# ── User-auth router ─────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/vault", tags=["vault"])


@router.post("/_admin/rebuild", dependencies=[Depends(require_role(Role.ADMIN))])
async def admin_rebuild_index(request: Request):
    """Admin-only: rebuild FTS5 index by walking the vault directory."""
    index = request.app.state.vault_index
    stats = index.rebuild_from_vault()
    logger.info("Vault index rebuild triggered via API: %s", stats)
    return {"ok": True, "stats": stats}


@router.get("/notes", dependencies=[Depends(require_role(Role.ADMIN))])
async def list_notes(
    request: Request,
    agent: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query(description="Filter by status")] = None,
    limit: Annotated[int, Query(le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List indexed vault notes (newest first) with optional filters + paging.

    `count` reflects the full filtered set so the client can decide when to
    stop fetching the next page. `notes` is the requested slice.
    """
    index = request.app.state.vault_index
    notes = list(index.list_all())
    if agent:
        notes = [n for n in notes if n["agent"] == agent]
    if type:
        notes = [n for n in notes if n["type"] == type]
    # Phase 2: optional status filter
    if status:
        notes = [n for n in notes if n.get("status", "published") == status]
    return {"count": len(notes), "notes": notes[offset : offset + limit]}


@router.get("/search", dependencies=[Depends(require_role(Role.ADMIN))])
async def search_notes(
    request: Request,
    q: Annotated[str, Query(min_length=1)],
    agent: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query(description="Filter by status (draft/published/stale/archived)")] = None,
    limit: Annotated[int, Query(le=200)] = 50,
):
    """Full-text search over vault notes (FTS5 porter tokenizer)."""
    index = request.app.state.vault_index
    hits = list(index.search(q, agent=agent, type=type, limit=limit))
    # Phase 2: optional status filter for admin
    if status:
        hits = [h for h in hits if h.get("status", "published") == status]

    # Phase 2: track views for last_viewed_at DB updates
    activity = getattr(request.app.state, "vault_activity", None)
    if activity and hits:
        for hit in hits:
            note_id = hit.get("id")
            path = hit.get("path")
            if note_id:
                try:
                    await activity.enqueue_view_for_db(note_id, path=path)
                except Exception:
                    pass  # fail-soft

    return {"q": q, "hits": hits}


@router.get("/note/{path:path}", dependencies=[Depends(require_role(Role.ADMIN))])
async def get_note(request: Request, path: str):
    """Return frontmatter + content of a single vault note by relative path.

    Path is URL-encoded on the wire (e.g. agents%2Fsparky%2Flessons%2Fa.md).
    """
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    try:
        post = parse_frontmatter(full)
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return {"frontmatter": post.metadata, "content": post.content}


# ── Task Bracket (Phase E) ───────────────────────────────────────────────────


@router.get("/related/{task_id}", dependencies=[Depends(require_role(Role.ADMIN))])
async def list_task_related(request: Request, task_id: str):
    """List all vault notes/wrappers that share a `task` frontmatter field.

    Used by the Reading-Panel "Verwandt" section: opening a wrapper for
    deliverable X surfaces every other note/file/lesson from the same task
    so the operator can see the full thread (research markdown + PDF report +
    researcher lesson + decision notes) without searching.

    The task field is filled on:
      - Deliverable wrappers (auto from deliverable.task_id, Phase A)
      - Manually-created notes when the user/agent passes task_id in the
        POST payload (Phase E)
      - Existing board_memory-derived markdown after the backfill script
        runs (scripts/backfill_vault_task_field.py)
    """
    # Quick UUID sanity check — bad input gets 400, not "no results" which
    # would look like the task simply has no related notes.
    import uuid as _uuid
    try:
        _uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task_id must be a UUID",
        )

    index = request.app.state.vault_index
    notes = list(index.list_all(task=task_id))
    return {"task_id": task_id, "count": len(notes), "notes": notes}


# ── Edit (admin direct write) ───────────────────────────────────────────────


class VaultNoteUpdate(BaseModel):
    """Partial update of a vault note.

    Only user-facing fields are editable here. Immutable / system fields
    (id, agent, type, date, related, relations, source) are deliberately
    NOT exposed — those carry semantics the indexer relies on.

    `model_config = ConfigDict(extra="forbid")` is implicit via the
    BaseModel + field whitelist below. Any unknown key raises 422.
    """
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None


# Allowed note types for admin-created notes. Kept in sync with the agent
# write-path enum + frontend MemoryType select. "journal" stays valid here
# (the operator's daily entries) — the auto-reflection guard only applies to the
# agent endpoint where it filters telemetry noise.
_ADMIN_NOTE_TYPES = frozenset({
    "note", "knowledge", "lesson", "reference", "journal",
    # "deliverable" are the wrappers for files from the TaskDeliverables inventory
    # (Phase A vault-as-brain). User-created notes normally pick a
    # different type, but we allow "deliverable" so the operator can, if needed,
    # edit or manually create a wrapper.
    "deliverable",
})


class VaultNoteCreateAdmin(BaseModel):
    """Body for admin-driven manual note creation (UI 'Neuer Eintrag').

    Writes the canonical file directly under ``agents/{agent}/{type}s/`` —
    no inbox/compactor round-trip needed because admin writes are trusted.
    """
    title: str = Field(min_length=3, max_length=120)
    content: str = Field(min_length=1)
    type: str = Field(default="note")
    tags: list[str] = Field(default_factory=list, max_length=12)
    agent: str = Field(default="mark", min_length=1, max_length=40)
    # Phase E task bracket — optional originating task. When set, the note's
    # frontmatter carries `task: <uuid>` and GET /vault/related/{task_id}
    # surfaces it alongside the deliverable wrappers from the same task.
    task_id: str | None = Field(default=None)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in _ADMIN_NOTE_TYPES:
            raise ValueError(
                f"type must be one of: {', '.join(sorted(_ADMIN_NOTE_TYPES))}"
            )
        return v

    @field_validator("agent")
    @classmethod
    def _slug_agent(cls, v: str) -> str:
        s = slugify(v)
        if not s:
            raise ValueError("agent must contain at least one alphanumeric char")
        return s


@router.post("/note", dependencies=[Depends(require_role(Role.ADMIN))])
async def create_note(request: Request, payload: VaultNoteCreateAdmin):
    """Create a vault note directly (admin UI 'Neuer Eintrag').

    Different from the agent endpoint (POST /agent/vault/note) which goes
    through the inbox/compactor for cross-writer safety. Here the user is
    the operator via JWT, so we write the canonical file synchronously and re-index
    in the same request — list/search/graph see it on the next fetch.

    Path layout: ``agents/{agent}/{type}s/{title-slug}-{ts}.md``.
    The timestamp suffix prevents collisions when the same title is used
    twice (rare but possible — the operator drafting two journal entries).
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S")
    agent_slug = payload.agent
    title_slug = slugify(payload.title) or "note"

    # Pluralise the type for the directory: lesson → lessons, decision →
    # decisions. Matches the existing on-disk convention.
    type_dir = f"{payload.type}s"
    target_rel = f"agents/{agent_slug}/{type_dir}/{title_slug}-{ts}.md"
    full = settings.vault_path / target_rel

    if full.exists():
        # Collision is astronomically unlikely (per-second timestamp) but
        # we surface it as 409 instead of silently overwriting.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"note already exists at {target_rel}",
        )

    full.parent.mkdir(parents=True, exist_ok=True)

    # Normalise tags: trim, drop empties, dedupe preserving insertion order.
    seen: set[str] = set()
    clean_tags: list[str] = []
    for t in payload.tags:
        tt = str(t).strip().lstrip("#")
        if tt and tt not in seen:
            seen.add(tt)
            clean_tags.append(tt)

    metadata: dict[str, Any] = {
        "id": f"{agent_slug}-{now.strftime('%Y%m%dT%H%M%S')}",
        "title": payload.title.strip(),
        "agent": agent_slug,
        "type": payload.type,
        "tags": clean_tags,
        "date": now.isoformat(),
        "related": [],
    }
    if payload.task_id:
        metadata["task"] = payload.task_id

    try:
        validate_frontmatter(metadata)
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    post = fm_lib.Post(payload.content, **metadata)

    # Atomic write: tmp → rename. Matches the PATCH and agent-write patterns.
    tmp = full.with_suffix(full.suffix + ".tmp")
    try:
        tmp.write_text(fm_lib.dumps(post))
        tmp.replace(full)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"write failed: {exc}",
        )

    # Synchronously re-index so the response can hand the new path back to
    # the UI and the user sees the note immediately. The watcher will also
    # fire, but its upsert is idempotent.
    index = request.app.state.vault_index
    try:
        index.upsert(full, post)
    except Exception as exc:
        logger.warning("post-create index upsert failed for %s: %s", target_rel, exc)

    embeddings = getattr(request.app.state, "vault_embeddings", None)
    if embeddings is not None:
        try:
            await embeddings.upsert(full, post, vault_path=settings.vault_path)
        except Exception as exc:
            logger.warning("post-create embed failed for %s: %s", target_rel, exc)

    try:
        redis = await get_redis()
        await publish_vault_event(redis, {"type": "created", "path": target_rel})
    except Exception as exc:
        logger.warning("vault:stream publish failed for create %s: %s", target_rel, exc)

    logger.info(
        "Vault create: %s (agent=%s type=%s tags=%d)",
        target_rel,
        agent_slug,
        payload.type,
        len(clean_tags),
    )
    return {"ok": True, "path": target_rel, "frontmatter": metadata, "content": post.content}


@router.patch("/note/{path:path}", dependencies=[Depends(require_role(Role.ADMIN))])
async def update_note(request: Request, path: str, payload: VaultNoteUpdate):
    """Edit a vault note's title, body and/or tags. Atomic write + sync index.

    The agent-owned write path (POST /agent/vault/note) goes through the
    inbox/compactor pattern for cross-writer safety. This endpoint is
    admin-only and writes the canonical file directly — the watcher will
    pick it up too, but we re-index synchronously so the frontend sees
    the change immediately on the response.

    Edge cases:
      - Refuses system paths (_trash/, _inbox/, _conflicts/, _rejected/,
        _lint/, .git/, .obsidian/) — those have their own lifecycles.
      - Preserves every frontmatter field the user did NOT send.
        Sending ``tags: []`` clears tags; sending ``tags: null`` (omitting
        the field) leaves them untouched.
    """
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    if full.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is a directory")

    vault_root = settings.vault_path.resolve()
    rel = str(full.relative_to(vault_root))
    if rel.startswith(("_trash/", "_inbox/", "_conflicts/", "_rejected/", "_lint/", ".git/", ".obsidian/")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot edit vault-system paths",
        )

    # 1) Read + merge ────────────────────────────────────────────────────────
    try:
        post = parse_frontmatter(full)
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    if payload.title is not None:
        post.metadata["title"] = payload.title.strip()
    if payload.tags is not None:
        # Normalise: trim, drop empties, dedupe while preserving order.
        seen: set[str] = set()
        clean: list[str] = []
        for t in payload.tags:
            tt = str(t).strip()
            if tt and tt not in seen:
                seen.add(tt)
                clean.append(tt)
        post.metadata["tags"] = clean
    if payload.content is not None:
        post.content = payload.content

    # 2) Validate post-merge — required fields must still be present.
    try:
        validate_frontmatter(post.metadata)
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # 3) Atomic write ────────────────────────────────────────────────────────
    tmp = full.with_suffix(full.suffix + ".tmp")
    try:
        tmp.write_text(fm_lib.dumps(post))
        tmp.replace(full)  # atomic on POSIX
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"write failed: {exc}",
        )

    # 4) Re-index synchronously so the next list/search/graph fetch sees the
    # edit. The watcher will also fire, but its upsert is idempotent.
    index = request.app.state.vault_index
    try:
        index.upsert(full, post)
    except Exception as exc:
        logger.warning("post-edit index upsert failed for %s: %s", rel, exc)

    embeddings = getattr(request.app.state, "vault_embeddings", None)
    if embeddings is not None:
        try:
            await embeddings.upsert(full, post, vault_path=settings.vault_path)
        except Exception as exc:
            logger.warning("post-edit embed failed for %s: %s", rel, exc)

    # 5) Bump graph-cache version + broadcast.
    try:
        redis = await get_redis()
        await publish_vault_event(redis, {"type": "modified", "path": rel})
    except Exception as exc:
        logger.warning("vault:stream publish failed for edit %s: %s", rel, exc)

    logger.info("Vault edit: %s (title=%r tags=%d)", rel, post.metadata.get("title"), len(post.metadata.get("tags") or []))
    return {"ok": True, "path": rel, "frontmatter": post.metadata, "content": post.content}


# ── Delete (soft-delete to _trash/) ─────────────────────────────────────────


def _note_id_and_stem(full: Path, vault: Path) -> tuple[str, str]:
    """Read (id, stem) from a note path. Used to find wikilink back-refs.

    Both forms exist: agents write [[<uuid>]] for cross-tree links and
    [[<stem>]] for in-tree convenience. We resolve both so the back-ref
    warning is honest.
    """
    stem = full.stem
    try:
        post = parse_frontmatter(full)
        note_id = str(post.metadata.get("id") or "")
    except Exception:
        note_id = ""
    return note_id, stem


@router.get("/note/{path:path}/backrefs", dependencies=[Depends(require_role(Role.ADMIN))])
async def get_note_backrefs(request: Request, path: str):
    """List notes that wikilink to the given note. Pre-delete UX surface.

    Returns:
        { "path": str, "backrefs": [{path, title, agent}, ...] }
    """
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    note_id, stem = _note_id_and_stem(full, settings.vault_path)
    index = request.app.state.vault_index
    refs = index.find_backrefs(note_id, stem)
    # Drop self-references defensively (a note that links to its own id).
    rel = str(full.relative_to(settings.vault_path.resolve()))
    refs = [r for r in refs if r.get("path") != rel]
    return {"path": rel, "backrefs": refs}


@router.delete("/note/{path:path}", dependencies=[Depends(require_role(Role.ADMIN))])
async def delete_note(request: Request, path: str):
    """Soft-delete a vault note. Moves the file into ``_trash/`` instead of
    unlinking it, so the operator can manually recover from accidental deletes.

    Side effects (best-effort, fail-soft on background systems):
      1. Move file: vault/<path> → vault/_trash/<YYYYMMDDTHHMMSS>-<basename>
      2. Drop the FTS5 row (so search + list never resurrect it).
      3. Delete Qdrant vector (so semantic search forgets it).
      4. Publish a ``vault:stream`` "deleted" event → live UIs refresh.

    Wikilink back-refs are returned in the response so the caller can show
    "X notes still reference this" feedback after the fact — the delete
    proceeds regardless. Hard-blocking would let one orphaned link prevent
    cleanup forever.

    Returns:
        { "ok": bool, "path": original, "trashed_to": str, "backrefs": int }
    """
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    if full.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is a directory")

    vault_root = settings.vault_path.resolve()
    rel = str(full.relative_to(vault_root))

    # Refuse to delete vault-system files. _trash/_inbox/_conflicts/_lint
    # entries are managed by the compactor/maintenance flows; UI users
    # shouldn't be removing them piecemeal.
    if rel.startswith(("_trash/", "_inbox/", "_conflicts/", "_rejected/", "_lint/", ".git/", ".obsidian/")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot delete vault-system paths",
        )

    # Capture id + stem BEFORE we move the file (frontmatter only loads on
    # the live path). Used both for back-ref lookup and the audit event.
    note_id, stem = _note_id_and_stem(full, settings.vault_path)
    index = request.app.state.vault_index
    backrefs = index.find_backrefs(note_id, stem)

    # 1) Soft-delete: move into _trash/ with a UTC timestamp prefix. Keeps
    # the relative-path shape readable, lets recovery happen with `mv`.
    trash_dir = settings.vault_path / "_trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # Use full relative path with `/` → `__` so two notes with same basename
    # can't collide in trash.
    safe_rel = rel.replace("/", "__")
    trash_target = trash_dir / f"{ts}-{safe_rel}"
    try:
        shutil.move(str(full), str(trash_target))
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to move to trash: {exc}",
        )

    # 2) Drop FTS5 row.
    try:
        index.delete(rel)
    except Exception as exc:
        logger.error("vault_index delete failed for %s: %s", rel, exc)

    # 3) Drop Qdrant vector (fail-soft inside VaultEmbeddings.delete).
    embeddings = getattr(request.app.state, "vault_embeddings", None)
    if embeddings is not None:
        try:
            await embeddings.delete(rel)
        except Exception as exc:
            logger.warning("vault_embeddings delete failed for %s: %s", rel, exc)

    # 4) Bump graph-cache version + broadcast so /memory list, /memory graph,
    # and the voice bridge all repaint without a hard refresh.
    try:
        redis = await get_redis()
        await publish_vault_event(redis, {
            "type": "deleted",
            "path": rel,
            "trashed_to": str(trash_target.relative_to(vault_root)),
            "id": note_id,
            "backrefs": len(backrefs),
        })
    except Exception as exc:
        logger.warning("vault:stream publish failed for delete %s: %s", rel, exc)

    logger.info(
        "Vault delete: %s → _trash/ (id=%s, backrefs=%d)",
        rel, note_id or "?", len(backrefs),
    )

    return {
        "ok": True,
        "path": rel,
        "trashed_to": str(trash_target.relative_to(vault_root)),
        "backrefs": len(backrefs),
    }


# ── Trash management ────────────────────────────────────────────────────────

# Filename format the soft-delete endpoint writes:
#   <UTC-timestamp>-<original-rel-path-with-slashes-replaced-by-double-underscore>.md
# e.g.  20260515T220500-agents__sparky__lessons__foo.md
import re as _re
_TRASH_FILENAME_RE = _re.compile(r"^(\d{8}T\d{6})-(.+\.md)$")


def _safe_trash_filename(filename: str, trash_dir: Path) -> Path:
    """Resolve a trash filename to its absolute path, refusing traversal.

    The user can only address files directly inside _trash/. No subdir
    listings, no '..', no symlink chasing.
    """
    name = unquote(filename)
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid trash filename")
    full = (trash_dir / name).resolve()
    try:
        full.relative_to(trash_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="trash filename escapes vault",
        )
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in trash")
    return full


def _parse_trash_filename(filename: str) -> tuple[Optional[str], Optional[str]]:
    """Return (iso_timestamp_or_none, original_rel_path_or_none).

    Legacy / manually-placed trash files that don't match the auto-prefix
    are still listable — they just expose no timestamp or original-path
    hint, and the restore endpoint refuses to operate on them.
    """
    m = _TRASH_FILENAME_RE.match(filename)
    if not m:
        return (None, None)
    ts_raw, safe_rel = m.group(1), m.group(2)
    try:
        ts_dt = datetime.strptime(ts_raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        ts_iso = ts_dt.isoformat()
    except ValueError:
        ts_iso = None
    original_rel = safe_rel.replace("__", "/")
    return (ts_iso, original_rel)


@router.get("/_trash", dependencies=[Depends(require_role(Role.ADMIN))])
async def list_trash(request: Request):
    """List notes in the vault soft-delete trash.

    For each file:
      - trash_filename  — what the restore/purge endpoints take
      - original_path   — derived from filename (null if pattern doesn't match)
      - trashed_at      — derived from filename timestamp (null if same)
      - title/agent/type/tags — parsed live from frontmatter so the row is
        recognisable even for old trash without an MC-generated filename.

    Sorted newest-first.
    """
    trash_dir = settings.vault_path / "_trash"
    if not trash_dir.exists():
        return {"count": 0, "items": []}

    items: list[dict[str, Any]] = []
    for entry in trash_dir.iterdir():
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        trashed_at, original_path = _parse_trash_filename(entry.name)
        # Parse frontmatter for display fields. Stale/malformed frontmatter
        # shouldn't break the list — just leave fields empty.
        title = ""
        agent = ""
        ntype = ""
        tags: list[str] = []
        date = ""
        try:
            post = parse_frontmatter(entry)
            meta = post.metadata
            title = str(meta.get("title") or "")
            agent = str(meta.get("agent") or "")
            ntype = str(meta.get("type") or "")
            tags = list(meta.get("tags") or [])
            date_raw = meta.get("date") or meta.get("created_at") or ""
            date = str(date_raw) if date_raw else ""
        except Exception:
            pass

        items.append({
            "trash_filename": entry.name,
            "original_path": original_path,
            "trashed_at": trashed_at,
            "title": title,
            "agent": agent,
            "type": ntype,
            "tags": tags,
            "date": date,
            "size_bytes": entry.stat().st_size,
        })

    # Newest first — sort by trashed_at when available, fallback to mtime.
    items.sort(
        key=lambda it: it["trashed_at"] or "",
        reverse=True,
    )
    return {"count": len(items), "items": items}


@router.post("/_trash/{filename}/restore", dependencies=[Depends(require_role(Role.ADMIN))])
async def restore_trash(request: Request, filename: str):
    """Move a trashed note back to its original vault path.

    Refuses if:
      - The filename doesn't parse (legacy/manual trash with no path hint).
      - The original path is already occupied (the operator needs to resolve manually
        — auto-rename would silently lose data).
      - The reconstructed path escapes the vault (path-traversal defence).
    """
    trash_dir = settings.vault_path / "_trash"
    src = _safe_trash_filename(filename, trash_dir)

    _, original_rel = _parse_trash_filename(filename)
    if not original_rel:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filename has no MC trash prefix — cannot infer original path. Restore manually via mv.",
        )

    # Reconstruct target with the standard safe-path check (defends against
    # `__../../etc/passwd.md` style abuse).
    target = _safe_path(original_rel, settings.vault_path)
    if target.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"original path {original_rel} already exists — move or delete it first",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(target))
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"restore failed: {exc}",
        )

    # Re-index. The watcher would pick this up eventually, but doing it
    # synchronously means the next list/search call sees the restored note.
    index = request.app.state.vault_index
    try:
        post = parse_frontmatter(target)
        index.upsert(target, post)
    except Exception as exc:
        logger.warning("post-restore index upsert failed for %s: %s", original_rel, exc)

    # Re-embed (fail-soft).
    embeddings = getattr(request.app.state, "vault_embeddings", None)
    if embeddings is not None:
        try:
            await embeddings.upsert(target, post)  # type: ignore[arg-type]
        except Exception as exc:
            logger.warning("post-restore embed failed for %s: %s", original_rel, exc)

    # Bump graph-cache version + broadcast — list/graph repaint, side panels react.
    try:
        redis = await get_redis()
        await publish_vault_event(redis, {
            "type": "restored",
            "path": original_rel,
            "from_trash": filename,
        })
    except Exception as exc:
        logger.warning("vault:stream publish failed for restore %s: %s", filename, exc)

    logger.info("Vault restore: %s → %s", filename, original_rel)
    return {"ok": True, "path": original_rel}


@router.delete("/_trash/{filename}", dependencies=[Depends(require_role(Role.ADMIN))])
async def purge_trash_item(request: Request, filename: str):
    """Permanently delete a single trash entry. No recovery after this."""
    trash_dir = settings.vault_path / "_trash"
    src = _safe_trash_filename(filename, trash_dir)
    try:
        src.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"purge failed: {exc}",
        )

    try:
        redis = await get_redis()
        # No version bump needed — trash content doesn't affect the live
        # graph. We only publish so the Trash tab updates in real time.
        await redis.publish(
            "vault:stream",
            json.dumps({"type": "trash_purged", "filename": filename}),
        )
    except Exception:
        pass

    logger.info("Vault trash purged: %s", filename)
    return {"ok": True, "filename": filename}


# ── Track-view endpoint (heatmap driver, M.4 prep) ───────────────────────────


class VaultTrackView(BaseModel):
    path: str  # vault-relative path, e.g. "agents/sparky/lessons/foo.md"

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        p = _PathLib(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("path must be a relative path with no '..' components")
        # Belt-and-suspenders: reject leading / or backslash explicitly
        if v.startswith("/") or v.startswith("\\"):
            raise ValueError("path must not start with / or \\")
        return v


@router.get("/graph", dependencies=[Depends(require_role(Role.ADMIN))])
async def get_graph(
    request: Request,
    cluster: Annotated[bool, Query()] = True,
    heatmap: Annotated[str, Query(description="Activity window, e.g. 7d, 30d")] = "30d",
    similarity_edges: Annotated[bool, Query(description="Include ghost edges from Qdrant top-K similarity (W3-A)")] = True,
    no_cache: Annotated[bool, Query(description="Bypass Redis cache (force fresh build)")] = False,
):
    """Build graph JSON (nodes/edges/clusters) for the 3D vault visualization.

    Read-only. Gracefully degrades when embeddings are unavailable
    (returns `clusters: []` and `cluster_id: null` on nodes).

    ``similarity_edges=true`` (default) appends implicit ghost edges
    (kind="similarity") derived from Qdrant nearest-neighbour search.
    Pass ``similarity_edges=false`` to get wikilink-only edges.

    Cached in Redis under a version-counter key (see vault_cache.py).
    The counter bumps on every vault mutation so callers always see a
    consistent view. Pass ``no_cache=true`` to force a fresh rebuild
    (debugging only — adds 1-4 s of cold-path latency).
    """
    index = request.app.state.vault_index
    embeddings = getattr(request.app.state, "vault_embeddings", None)
    activity = request.app.state.vault_activity
    if index is None or activity is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault services not initialized",
        )

    redis = await get_redis()
    p_hash = params_hash(
        cluster=cluster, heatmap=heatmap, similarity_edges=similarity_edges
    )
    version = await get_graph_version(redis)

    if not no_cache:
        cached = await get_cached_graph(redis, version, p_hash)
        if cached is not None:
            # Tag the response so the frontend can see "instant" hits in
            # the stats line + DevTools without changing the schema shape.
            stats = cached.get("stats") if isinstance(cached, dict) else None
            if isinstance(stats, dict):
                stats["cache_hit"] = True
            logger.debug(
                "vault_graph: cache hit v%d/%s (%d nodes)",
                version,
                p_hash,
                len((cached or {}).get("nodes", [])) if isinstance(cached, dict) else 0,
            )
            return cached

    payload = await build_graph(
        index,
        embeddings,
        activity,
        cluster=cluster,
        heatmap=heatmap,
        similarity_edges=similarity_edges,
    )
    # Mark this as a cold build before caching so the next hit knows.
    if isinstance(payload, dict) and isinstance(payload.get("stats"), dict):
        payload["stats"]["cache_hit"] = False
    await set_cached_graph(redis, version, p_hash, payload)
    logger.info(
        "vault_graph: cold build cached at v%d/%s (build_ms=%s)",
        version,
        p_hash,
        (payload.get("stats") or {}).get("build_ms"),
    )
    return payload


# ── Topics (Phase 3 Intelligence) ──────────────────────────────────────────

@router.get("/topics", dependencies=[Depends(require_role(Role.ADMIN))])
async def get_topics(
    request: Request,
    k: Annotated[int | None, Query(description="Force cluster count (auto if None)")] = None,
):
    """Topic-centric view of vault notes using k-means clustering.

    Groups notes by semantic similarity and returns a flat topic list
    with top-5 notes per cluster, note count, and contributing agents.

    Uses the same k-means clustering as the graph endpoint but returns
    a simplified JSON shape for the Topics tab.
    """
    from app.services.vault_graph import (
        _fetch_embeddings,
        _kmeans_cluster,
        _single_cluster_fallback,
        _stem,
        resolve_label,
    )

    index = request.app.state.vault_index
    embeddings = getattr(request.app.state, "vault_embeddings", None)
    if index is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault index not initialized",
        )

    # 1) Load all notes
    rows = list(index.list_all())
    if not rows:
        return {"topics": [], "total_notes": 0}

    path_set: set[str] = set()
    notes_by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = row.get("path") or ""
        if not path:
            continue
        path_set.add(path)
        label = resolve_label(
            frontmatter={"title": row.get("title") or ""},
            content=row.get("content") or "",
            filename=path,
        )
        notes_by_path[path] = {
            "title": label,
            "agent": row.get("agent") or "",
            "type": row.get("type") or "",
            "tags": (row.get("tags") or "").split(" ") if row.get("tags") else [],
        }

    # 2) Fetch embeddings from Qdrant
    vectors_by_path: dict[str, list[float]] = {}
    if embeddings is not None:
        vectors_by_path = await _fetch_embeddings(embeddings, path_set)

    if not vectors_by_path:
        # No embeddings available — single cluster with all notes
        return {
            "topics": [{
                "cluster_id": 0,
                "label": "Alle Notes",
                "note_count": len(notes_by_path),
                "top_notes": [notes_by_path[p]["title"] for p in list(notes_by_path)[:5]],
                "agents": list({notes_by_path[p]["agent"] for p in notes_by_path}),
            }],
            "total_notes": len(notes_by_path),
        }

    # 3) Cluster
    ordered_paths = [p for p in path_set if p in vectors_by_path]
    ordered_vecs = [vectors_by_path[p] for p in ordered_paths]
    cluster_by_path, clusters = _kmeans_cluster(ordered_paths, ordered_vecs)

    # 4) Build topic response
    topics: list[dict[str, Any]] = []
    for cluster in clusters:
        members = cluster["member_paths"]
        member_notes = [notes_by_path[m] for m in members if m in notes_by_path]
        agents = list({n["agent"] for n in member_notes if n["agent"]})
        top_notes = [n["title"] for n in member_notes[:5]]

        # Generate a label from the most common tags across members
        tag_counts: dict[str, int] = {}
        for n in member_notes:
            for tag in n.get("tags", []):
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
        sorted_tags = sorted(tag_counts, key=lambda t: tag_counts[t], reverse=True)
        label = " & ".join(sorted_tags[:3]) if sorted_tags else f"Cluster {cluster['cluster_id']}"

        topics.append({
            "cluster_id": cluster["cluster_id"],
            "label": label,
            "note_count": len(members),
            "top_notes": top_notes,
            "agents": agents,
        })

    # Sort by note count descending
    topics.sort(key=lambda t: t["note_count"], reverse=True)

    return {"topics": topics, "total_notes": len(notes_by_path)}


@router.post("/track-view", dependencies=[Depends(require_role(Role.ADMIN))])
async def track_view(payload: VaultTrackView, request: Request):
    """Record a view event in Redis for the M.4 heatmap.

    Fail-soft if vault_activity is not yet initialized (e.g. during tests
    that don't wire the full lifespan).

    TODO(follow-up): extract real user_id from JWT instead of hardcoding "mark".
    """
    activity = request.app.state.vault_activity
    if activity is None:
        return {"ok": False, "error": "activity not initialized"}
    await activity.track_view(payload.path, user_id="mark")  # user_id="mark" hardcoded for now
    return {"ok": True}


# ── Promotion endpoints (Phase 2) ──────────────────────────────────────────


@router.patch("/note/{path:path}/promote", dependencies=[Depends(require_role(Role.ADMIN))])
async def promote_note(request: Request, path: str):
    """Manually promote a draft note to published (admin only).

    Ignores contradictions — force=True. Used when the operator has reviewed
    and approved a note despite flagged contradictions.
    """
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")

    try:
        post = parse_frontmatter(full)
        note_id = str(post.metadata.get("id", ""))
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    if post.metadata.get("status") != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"note is {post.metadata.get('status')}, not draft",
        )

    promoter = getattr(request.app.state, "vault_promoter", None)
    if promoter is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="promoter not initialized")

    promoted = await promoter.promote_note(note_id, path, force=True)
    if not promoted:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="promotion failed")

    return {"ok": True, "path": path, "status": "published"}


@router.patch("/note/{path:path}/reject", dependencies=[Depends(require_role(Role.ADMIN))])
async def reject_note(request: Request, path: str):
    """Reject a draft note — moves it to _rejected/ (admin only)."""
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")

    try:
        post = parse_frontmatter(full)
        note_id = str(post.metadata.get("id", ""))
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    promoter = getattr(request.app.state, "vault_promoter", None)
    if promoter is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="promoter not initialized")

    rejected = await promoter.reject_note(note_id, path)
    if not rejected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="rejection failed")

    return {"ok": True, "path": path, "status": "rejected"}


# ── Agent-scoped router ───────────────────────────────────────────────────────

agent_router = APIRouter(prefix="/api/v1/agent/vault", tags=["vault-agent"])


@agent_router.get("/search", dependencies=[Depends(require_scope(Scope.VAULT_READ))])
async def agent_search_notes(
    request: Request,
    q: Annotated[str, Query(min_length=1)],
    agent: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query(description="Filter by status (draft/published/stale/archived)")] = None,
    limit: Annotated[int, Query(le=50)] = 20,
    current_agent=Depends(require_agent),
):
    """Agent-scoped FTS search (requires vault:read scope)."""
    index = request.app.state.vault_index
    hits = list(index.search(q, agent=agent, type=type, limit=limit))

    # Phase 2: status filter — agents see published + own drafts by default
    requesting_slug = slugify(current_agent.name)
    if status:
        hits = [h for h in hits if h.get("status", "published") == status]
    else:
        # Default: published + own drafts
        hits = [
            h for h in hits
            if h.get("status", "published") == "published"
            or (h.get("status") == "draft" and h.get("agent") == requesting_slug)
        ]

    # Phase 2: track views for last_viewed_at DB updates
    activity = getattr(request.app.state, "vault_activity", None)
    if activity and hits:
        for hit in hits:
            note_id = hit.get("id")
            path = hit.get("path")
            if note_id:
                try:
                    await activity.enqueue_view_for_db(note_id, path=path)
                except Exception:
                    pass  # fail-soft — never block search for tracking

    return {"q": q, "hits": hits, "requesting_agent": slugify(current_agent.name)}


@agent_router.get("/note/{path:path}", dependencies=[Depends(require_scope(Scope.VAULT_READ))])
async def agent_get_note(
    request: Request,
    path: str,
    current_agent=Depends(require_agent),
):
    """Agent-scoped get vault note by path (requires vault:read scope)."""
    full = _safe_path(path, settings.vault_path)
    if not full.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    try:
        post = parse_frontmatter(full)
    except FrontmatterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return {"frontmatter": post.metadata, "content": post.content}


# ── Vault Attachment Serving (Phase 4 follow-ups) ───────────────────────────
#
# Wrappers carry `deliverable_id` in their frontmatter; the actual binary lives
# at vault/attachments/{kind}/<deliverable-id>.<ext>. These endpoints resolve
# id → on-disk path → FileResponse. Whitelist-only — the path is computed by
# the backend, no caller-supplied filename anywhere. Defense in depth: we still
# assert the resolved path lives under vault/attachments/.

_ATTACHMENT_KIND_DIRS = ("files", "images", "audio")


def _resolve_vault_attachment(deliverable_id: str, vault_path: Path) -> tuple[Path, str | None]:
    """Find the on-disk attachment for a deliverable_id.

    The wrapper-sync (deliverable_wrapper.py) names attachments
    ``attachments/<kind>/<deliverable_id>.<ext>`` — extension comes from the
    source file. We glob the three kind-dirs because callers don't pass a
    mime hint and we don't want a DB round-trip on the hot path.

    Returns (absolute_path, mime_or_none). Raises HTTPException(404) when no
    file matches. The mime hint is parsed from frontmatter of the matching
    wrapper if one exists; otherwise None and FileResponse falls back to
    application/octet-stream which still triggers an inline preview for
    PDFs/images in modern browsers.
    """
    attachments_root = (vault_path / "attachments").resolve()
    for kind in _ATTACHMENT_KIND_DIRS:
        kind_dir = attachments_root / kind
        if not kind_dir.is_dir():
            continue
        for candidate in kind_dir.glob(f"{deliverable_id}.*"):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(attachments_root)
            except ValueError:
                # Symlink escaping attachments/ — refuse.
                continue
            if resolved.is_file():
                # Best-effort mime lookup: scan wrappers for the matching id.
                # Cheap (only ~300 wrappers today). If miss, return None.
                mime = _lookup_mime_from_wrapper(deliverable_id, vault_path)
                return resolved, mime
    raise HTTPException(status_code=404, detail=f"attachment for deliverable {deliverable_id} not found")


def _lookup_mime_from_wrapper(deliverable_id: str, vault_path: Path) -> str | None:
    """Read the wrapper frontmatter to pick up the attachment_mime hint.

    Globs ``agents/*/deliverables/*-<deliverable_id>.md``. If found, parses
    the YAML frontmatter and returns ``attachment_mime``. Returns None on
    any miss/parse error — FileResponse handles None gracefully.
    """
    for wrapper in (vault_path / "agents").rglob(f"*-{deliverable_id}.md"):
        try:
            post = fm_lib.load(str(wrapper))
            mime = post.metadata.get("attachment_mime")
            if isinstance(mime, str) and mime:
                return mime
        except Exception:
            pass
        break  # only inspect the first match
    return None


@router.get("/attachment/{deliverable_id}", dependencies=[Depends(require_role(Role.ADMIN))])
async def serve_vault_attachment_admin(deliverable_id: str):
    """Admin: download the binary attachment for a vault wrapper.

    Used by the frontend Reading-Panel to render PDFs/images inline. The
    JWT in the auth header covers access control — this is admin-only
    because the operator is the sole admin user today.
    """
    abs_path, mime = _resolve_vault_attachment(deliverable_id, settings.vault_path)
    return FileResponse(
        str(abs_path),
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@agent_router.get(
    "/attachment/{deliverable_id}",
    dependencies=[Depends(require_scope(Scope.VAULT_READ))],
)
async def serve_vault_attachment_agent(deliverable_id: str):
    """Agent-scoped: same as admin but requires the vault:read scope.

    Lets worker agents fetch a wrapper's binary over HTTP when they can't
    Read it from the local vault mount (e.g. host-runtime agents without
    the docker bind-mount).
    """
    abs_path, mime = _resolve_vault_attachment(deliverable_id, settings.vault_path)
    return FileResponse(
        str(abs_path),
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@agent_router.get("/related/{task_id}", dependencies=[Depends(require_scope(Scope.VAULT_READ))])
async def agent_list_task_related(
    request: Request,
    task_id: str,
    current_agent=Depends(require_agent),
):
    """Agent-scoped task bracket (Phase E).

    Returns every vault note that carries the same `task: <uuid>` frontmatter
    field. The expected agentic usage: search_notes(...) → if the top hit
    is a deliverable wrapper with a task, follow up with related(task) to
    fetch the lessons + decisions + other deliverables from that task before
    drafting next steps.
    """
    import uuid as _uuid
    try:
        _uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task_id must be a UUID",
        )
    index = request.app.state.vault_index
    notes = list(index.list_all(task=task_id))
    return {"task_id": task_id, "count": len(notes), "notes": notes}


# Phase C → Refactor: Telegram delivery for vault wrappers now lives at
# POST /api/v1/agent/me/telegram with body `{text, vault_path}`. The handler
# (agent_scoped.py::agent_send_telegram_report) resolves vault_path → wrapper
# → attachment_path and ships the binary via telegram_reports.send_document.
# One Telegram endpoint, three input modes (deliverable_id / document_deliverable_id
# / vault_path) instead of a separate vault-specific route.


# ── Write route (M.2 T7) ─────────────────────────────────────────────────────


class VaultNoteCreate(BaseModel):
    title: str = Field(min_length=3, max_length=80, description="Human-readable title")
    content: str = Field(min_length=10, description="Markdown body")
    type: str = Field(default="note", description="note | knowledge | lesson | reference | journal")
    tags: list[str] = Field(default_factory=list, max_length=8)
    target: str | None = None
    idempotency_key: str | None = None
    # W3-C — advisory, not enforced. First note in a new area legitimately
    # has no neighbours. The wikilink-backfill job (vault_wikilink_backfill)
    # connects orphans retroactively via Qdrant similarity + Spark LLM.
    related_notes: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Wikilinks [[note-slug]] to existing notes. "
            "Empfohlen: vor dem Schreiben search_notes() aufrufen und die "
            "2-4 thematisch passendsten Ergebnisse hier verlinken (auch inline "
            "im content). Leer lassen erlaubt — der Backfill-Job verknüpft "
            "orphan-Notes automatisch nachträglich. "
            "Format: '[[note-slug]]'."
        ),
    )
    relations: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional. Relation types per note-slug: "
            "supersedes | contradicts | refines | example-of | depends-on | related-to"
        ),
    )
    # Phase E task bracket — optional originating task UUID. Voice + Worker
    # agents set this when writing a memory during an active task; the field
    # is what GET /related/{task_id} joins on. Old envelopes without it stay
    # valid (validate_frontmatter treats `task` as optional).
    task_id: str | None = Field(
        default=None,
        description=(
            "Optionale Task-UUID. Wenn ein Agent während eines aktiven Tasks "
            "eine Memory schreibt, setze diese ID — damit andere Agents später "
            "ALLE Notes + Deliverables aus diesem Task auf einmal finden."
        ),
    )

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str | None) -> str | None:
        if v is None:
            return v
        p = _PathLib(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("target must be a relative path with no '..' components")
        # Reject leading / or backslash chars (belt-and-suspenders over is_absolute)
        if v.startswith("/") or v.startswith("\\"):
            raise ValueError("target must not start with / or \\")
        return v


@agent_router.post("/note", dependencies=[Depends(require_scope(Scope.VAULT_WRITE))])
async def agent_write_note(
    payload: VaultNoteCreate,
    current_agent=Depends(require_scope(Scope.VAULT_WRITE)),
):
    """Write a vault note as an envelope to /_inbox/.

    The VaultCompactor (separate service) picks up envelopes asynchronously
    and merges them to their canonical target paths in the vault.

    Atomic write: tmp file → rename, so the compactor never sees a partial file.

    W4.1 guard: auto-generated task-done reflections (type=journal + tag "auto")
    must NOT be written to the vault — they are telemetry, not knowledge.
    Use a TaskComment with comment_type='reflection' instead.
    """
    # W4.1 — Block audit-trail noise at the API boundary
    if payload.type == "journal" and "auto" in (payload.tags or []):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Auto-reflections must not be written to the vault. "
                "Use a TaskComment with comment_type='reflection' instead."
            ),
        )

    inbox = settings.vault_path / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S%f")

    agent_slug = slugify(current_agent.name)
    title_slug = slugify(payload.title)
    target = payload.target or f"agents/{agent_slug}/{payload.type}s/{title_slug}.md"

    metadata = {
        "op": "upsert",
        "target": target,
        "agent_id": str(current_agent.id),
        "agent": agent_slug,
        "type": payload.type,
        "tags": payload.tags,
        "date": now.isoformat(),
        "id": f"{agent_slug}-{ts[:15]}",
        "sha256": sha256(payload.content.encode()).hexdigest(),
        "idempotency_key": payload.idempotency_key,
        "related": payload.related_notes,
        **({"relations": payload.relations} if payload.relations else {}),
        # Phase E: keep `task` in the envelope so the compactor preserves it
        # when merging into the canonical target file.
        **({"task": payload.task_id} if payload.task_id else {}),
    }

    envelope_name = f"{ts}_{agent_slug}_{title_slug}.md"
    envelope_path = inbox / envelope_name
    tmp = envelope_path.with_suffix(".tmp")

    post = fm_lib.Post(payload.content, **metadata)
    tmp.write_text(fm_lib.dumps(post))
    tmp.rename(envelope_path)

    logger.info(
        "Vault envelope written: %s → %s (agent=%s)",
        envelope_name,
        target,
        agent_slug,
    )
    return {"ok": True, "envelope": envelope_name, "expected_target": target}


# ── File-Answer (Phase 3 Intelligence) ─────────────────────────────────────


class VaultFileAnswer(BaseModel):
    """Body for filing a query-answer pair as a vault knowledge note."""
    query: str = Field(min_length=5, max_length=200, description="The original question")
    answer: str = Field(min_length=10, description="The answer/research result")
    source_note_ids: list[str] = Field(
        default_factory=list, max_length=10,
        description="UUIDs of vault notes that contributed to this answer",
    )
    type: str = Field(
        default="knowledge",
        pattern=r"^(knowledge|lesson|note|reference)$",
        description="note | knowledge | lesson | reference",
    )
    tags: list[str] = Field(default_factory=list, max_length=8)


@agent_router.post("/file-answer", dependencies=[Depends(require_scope(Scope.VAULT_WRITE))])
async def agent_file_answer(
    payload: VaultFileAnswer,
    current_agent=Depends(require_scope(Scope.VAULT_WRITE)),
):
    """File a query-answer pair as a vault knowledge note.

    Creates an inbox envelope with auto-generated title from the query,
    links to source notes, and goes through the normal promotion flow.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S%f")
    agent_slug = slugify(current_agent.name)

    # Auto-generate title from query (first 60 chars)
    title = payload.query[:60] + "..." if len(payload.query) > 60 else payload.query
    title_slug = slugify(title)

    target = f"agents/{agent_slug}/{payload.type}s/{title_slug}.md"

    # Build content with query context
    content_lines = [
        f"**Frage:** {payload.query}",
        "",
        payload.answer,
    ]
    if payload.source_note_ids:
        content_lines.append("")
        content_lines.append("**Quellen:** " + ", ".join(
            f"[[{sid}]]" for sid in payload.source_note_ids
        ))
    content = "\n".join(content_lines)

    # Build related links from source_note_ids
    related = [f"[[{sid}]]" for sid in payload.source_note_ids]

    metadata = {
        "op": "upsert",
        "target": target,
        "agent_id": str(current_agent.id),
        "agent": agent_slug,
        "type": payload.type,
        "tags": payload.tags,
        "date": now.isoformat(),
        "id": f"{agent_slug}-{ts[:15]}",
        "sha256": sha256(content.encode()).hexdigest(),
        "source": "query-result",
        "related": related,
    }

    inbox = settings.vault_path / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    envelope_name = f"{ts}_{agent_slug}_file-answer_{title_slug}.md"
    envelope_path = inbox / envelope_name
    tmp = envelope_path.with_suffix(".tmp")

    post = fm_lib.Post(content, **metadata)
    tmp.write_text(fm_lib.dumps(post))
    tmp.rename(envelope_path)

    logger.info(
        "Vault file-answer envelope: %s -> %s (agent=%s, sources=%d)",
        envelope_name, target, agent_slug, len(payload.source_note_ids),
    )
    return {"ok": True, "envelope": envelope_name, "expected_target": target}


# ── Briefing (M.4 T3) ────────────────────────────────────────────────────────

# Time-of-day labels for the Europe/Zurich timezone. CH is currently +01:00 (CET)
# or +02:00 (CEST). We use +01:00 as fallback — voice greeting precision is OK
# at hour granularity, DST drift is at worst one bucket off near boundaries.
# Acceptable trade-off: zero dependencies, no zoneinfo lookup.
_TZ_OFFSET_HOURS = 1  # Europe/Zurich (CET, ignoring DST; ~1h drift near boundary)


def _time_of_day_de(now: datetime) -> str:
    """Return a German time-of-day label for the given UTC datetime.

    Buckets (Europe/Zurich local time, approximated):
      05-10 → morgens
      11-12 → mittags
      13-16 → nachmittags
      17-21 → abends
      22-04 → nachts
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_hour = (now.astimezone(timezone.utc).hour + _TZ_OFFSET_HOURS) % 24
    if 5 <= local_hour <= 10:
        return "morgens"
    if 11 <= local_hour <= 12:
        return "mittags"
    if 13 <= local_hour <= 16:
        return "nachmittags"
    if 17 <= local_hour <= 21:
        return "abends"
    return "nachts"


_DATE_YEAR_MIN = 2020
_DATE_YEAR_MAX = 2100


def _plausible_ymd(year: int, month: int, day: int) -> bool:
    """Sanity-check a (year, month, day) triple before trusting it as a real date.

    Guards against misparsed fragments (e.g. a UUID segment that happens to be
    all-digits) producing nonsense dates like month=52 or year=9704.
    """
    return _DATE_YEAR_MIN <= year <= _DATE_YEAR_MAX and 1 <= month <= 12 and 1 <= day <= 31


def _extract_date_from_id(note_id: str | None) -> str | None:
    """Extract the YYYY-MM-DD date from a vault note id like 'sparky-20260514T123000'.

    Requires the strict ``<prefix>-<8 digits>T<6 digits>`` shape (the format the
    vault writer actually produces) plus a plausibility check on year/month/day.
    This intentionally does NOT match a bare UUID's trailing hex segment — a
    12-char hex group can accidentally be all-digits (no "T" separator, wrong
    length) and would otherwise be misread as a date (real incident: a UUID
    fragment was parsed into the date '9704-52-17').

    Returns None when the id is missing or doesn't match the strict shape.
    """
    if not note_id or "-" not in note_id:
        return None
    ts = note_id.rsplit("-", 1)[-1]
    # Strict shape: YYYYMMDDTHHMMSS (15 chars, literal T at index 8).
    if len(ts) < 15 or ts[8].upper() != "T" or not ts[:8].isdigit():
        return None
    year, month, day = int(ts[0:4]), int(ts[4:6]), int(ts[6:8])
    if not _plausible_ymd(year, month, day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_reliable_date(date_raw: Any) -> str | None:
    """Extract a plausible YYYY-MM-DD from a frontmatter ``date``/``created_at``
    value (as stored in the vault index — the author-provided, reliable field).

    Accepts a plain date ('2026-05-14') or an ISO timestamp
    ('2026-05-14T10:30:00Z'); rejects anything that doesn't match the strict
    leading YYYY-MM-DD pattern or fails the plausibility check.
    """
    if not date_raw:
        return None
    s = str(date_raw).strip()
    m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not _plausible_ymd(year, month, day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _note_date(note: dict) -> str | None:
    """Resolve a note's date from the most reliable source available.

    Prefers the vault index's own ``date`` column (frontmatter ``date`` or
    ``created_at`` — author-provided, reliable). Falls back to strict
    id-timestamp parsing only when no frontmatter date is present.
    """
    return _parse_reliable_date(note.get("date")) or _extract_date_from_id(note.get("id"))


def _age_days(date_str: str | None, now: datetime) -> int | None:
    """Days between *date_str* (YYYY-MM-DD) and *now*. None if unparseable."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, (now - d).days)


@agent_router.get("/briefing", dependencies=[Depends(require_scope(Scope.VAULT_READ))])
async def agent_vault_briefing(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_agent=Depends(require_agent),
):
    """Pre-session briefing JSON for voice (or any other agent) to orient itself.

    Returns: open_tasks (deduped, with age_days + duplicate_count),
             open_approvals_count, recent_lessons/recent_writes (real-date sorted,
             with age_days), agents_online/offline, current_time_of_day_de,
             staleness_summary.

    Fail-soft: every data source is wrapped — partial failures return the partial
    briefing with an `error` field instead of HTTP 500. Voice should never
    fail-fast on a briefing fetch.
    """
    now = datetime.now(timezone.utc)
    errors: list[str] = []

    result: dict[str, Any] = {
        "current_iso": now.isoformat(),
        "current_time_of_day_de": _time_of_day_de(now),
        "open_tasks": [],
        "open_approvals_count": 0,
        "recent_lessons": [],
        "recent_writes": [],
        "agents_online": 0,
        "agents_offline": 0,
        "staleness_summary": {},
    }

    # ── Open tasks (top 10, ordered by created_at DESC, deduped) ─────────────
    try:
        stmt = (
            select(Task)
            .where(Task.status.in_(("inbox", "in_progress", "blocked", "review")))
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        tasks = (await session.exec(stmt)).all()
        # exec() with select(Model) returns Row objects in SQLAlchemy 2.x;
        # using session.execute would too. Normalize.
        normalized: list[Task] = []
        for t in tasks:
            if hasattr(t, "_mapping"):  # Row
                normalized.append(t[0])
            else:
                normalized.append(t)
        # Resolve agent names for assignment display
        agent_ids = {t.assigned_agent_id for t in normalized if t.assigned_agent_id}
        agent_name_map: dict = {}
        if agent_ids:
            agent_stmt = select(Agent).where(Agent.id.in_(list(agent_ids)))
            agent_rows = (await session.exec(agent_stmt)).all()
            for a in agent_rows:
                if hasattr(a, "_mapping"):
                    a = a[0]
                agent_name_map[a.id] = a.name

        # Dedup by (title, status, assigned_to). `normalized` is already
        # created_at DESC, so the first occurrence of a key is the newest —
        # keep it, count the rest as duplicates instead of dropping them silently.
        deduped: dict[tuple, dict[str, Any]] = {}
        dedup_order: list[tuple] = []
        for t in normalized:
            assigned_to = agent_name_map.get(t.assigned_agent_id)
            key = (t.title, t.status, assigned_to)
            created = t.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if key in deduped:
                deduped[key]["duplicate_count"] += 1
                continue
            deduped[key] = {
                "id": str(t.id),
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "assigned_to": assigned_to,
                "age_days": max(0, (now - created).days) if created else None,
                "duplicate_count": 1,
            }
            dedup_order.append(key)

        # in_progress/blocked before inbox before everything else (e.g. review);
        # younger (lower age_days) first within each tier.
        _STATUS_TIER = {"in_progress": 0, "blocked": 0, "inbox": 1}

        def _task_sort_key(key: tuple) -> tuple:
            item = deduped[key]
            tier = _STATUS_TIER.get(item["status"], 2)
            age = item["age_days"] if item["age_days"] is not None else 10**9
            return (tier, age)

        dedup_order.sort(key=_task_sort_key)
        result["open_tasks"] = [deduped[k] for k in dedup_order]
    except Exception as exc:  # noqa: BLE001 — fail-soft per design
        logger.warning("briefing: open_tasks failed: %s", exc)
        errors.append(f"open_tasks: {exc}")

    # ── Open approvals count ─────────────────────────────────────────────────
    try:
        stmt = select(func.count()).select_from(Approval).where(Approval.status == "pending")
        cnt = (await session.execute(stmt)).scalar() or 0
        result["open_approvals_count"] = int(cnt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("briefing: approvals_count failed: %s", exc)
        errors.append(f"open_approvals_count: {exc}")

    # ── Recent lessons (last 24h, type=lesson, top 5 from vault index) ───────
    try:
        index = getattr(request.app.state, "vault_index", None)
        if index is None:
            errors.append("recent_lessons: vault_index not initialized")
        else:
            cutoff_date = (now - timedelta(hours=24)).strftime("%Y-%m-%d")
            lessons: list[dict[str, Any]] = []
            for note in index.list_all():
                if note.get("type") != "lesson":
                    continue
                date = _note_date(note)
                lessons.append(
                    {
                        "path": note.get("path"),
                        "agent": note.get("agent"),
                        "title": (note.get("content") or "").strip().splitlines()[0][:120]
                        if note.get("content")
                        else None,
                        "date": date,
                        "age_days": _age_days(date, now),
                    }
                )
            # Filter to last 24h when we have a date; keep dateless ones at the
            # back so we still return something on a freshly seeded vault.
            recent = [l for l in lessons if (l["date"] or "") >= cutoff_date]
            # Real-date descending; items without a date sink to the bottom.
            recent.sort(key=lambda x: x["date"] or "", reverse=True)
            if not recent:
                # Fallback: top 5 lessons regardless of date, still date-sorted
                # (dateless ones last).
                lessons.sort(key=lambda x: x["date"] or "", reverse=True)
                recent = lessons
            result["recent_lessons"] = recent[:5]
    except Exception as exc:  # noqa: BLE001
        logger.warning("briefing: recent_lessons failed: %s", exc)
        errors.append(f"recent_lessons: {exc}")

    # ── Recent writes (real-date sorted, top 5) ───────────────────────────────
    try:
        activity = getattr(request.app.state, "vault_activity", None)
        index = getattr(request.app.state, "vault_index", None)
        if activity is None:
            errors.append("recent_writes: vault_activity not initialized")
        else:
            # top_n_writes ranks by write COUNT (most-modified), not recency —
            # pull a wider candidate pool so genuinely recent single writes
            # aren't crowded out by old-but-frequently-touched notes, then
            # re-sort by real date ourselves before truncating to 5.
            raw_writes = await activity.top_n_writes(limit=25, window="24h")
            # Enrich each write with type+agent via index lookup (best-effort)
            path_to_meta: dict[str, dict] = {}
            if index is not None:
                for n in index.list_all():
                    path_to_meta[n.get("path")] = n
            enriched: list[dict[str, Any]] = []
            for w in raw_writes:
                meta = path_to_meta.get(w.get("path"), {})
                date = _note_date(meta)
                enriched.append(
                    {
                        "path": w.get("path"),
                        "agent": meta.get("agent"),
                        "type": meta.get("type"),
                        "date": date,
                        "age_days": _age_days(date, now),
                    }
                )
            # Real-date descending; dateless writes sink to the bottom instead
            # of leaking a stale "most frequently written" ordering.
            enriched.sort(key=lambda x: x["date"] or "", reverse=True)
            result["recent_writes"] = enriched[:5]
    except Exception as exc:  # noqa: BLE001
        logger.warning("briefing: recent_writes failed: %s", exc)
        errors.append(f"recent_writes: {exc}")

    # ── Agents online/offline (last_seen_at within 5min = online) ────────────
    try:
        threshold = now - timedelta(minutes=5)
        stmt = select(Agent.last_seen_at)
        rows = (await session.execute(stmt)).all()
        online = 0
        offline = 0
        for (ts,) in rows:
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts and ts >= threshold:
                online += 1
            else:
                offline += 1
        result["agents_online"] = online
        result["agents_offline"] = offline
    except Exception as exc:  # noqa: BLE001
        logger.warning("briefing: agents_status failed: %s", exc)
        errors.append(f"agents_status: {exc}")

    # ── Staleness summary — machine-readable so the voice layer can be honest
    # about how old the "recent" items actually are, instead of implying
    # freshness that isn't there. ────────────────────────────────────────────
    write_ages = [w["age_days"] for w in result["recent_writes"] if w.get("age_days") is not None]
    lesson_ages = [l["age_days"] for l in result["recent_lessons"] if l.get("age_days") is not None]
    newest_write_age = min(write_ages) if write_ages else None
    newest_lesson_age = min(lesson_ages) if lesson_ages else None
    if newest_write_age is None:
        note = "no reliably dated writes found"
    elif newest_write_age <= 1:
        note = "up to date"
    elif newest_write_age <= 7:
        note = f"newest write is {newest_write_age} days old"
    else:
        note = "no writes in last 7 days"
    result["staleness_summary"] = {
        "newest_write_age_days": newest_write_age,
        "newest_lesson_age_days": newest_lesson_age,
        "note": note,
    }

    if errors:
        result["error"] = "; ".join(errors)

    return result


# ── WebSocket streams (M.4 T2) ────────────────────────────────────────────────

_HEARTBEAT_INTERVAL = 30  # seconds


def _ws_validate_jwt(token: str | None) -> bool:
    """Return True if token is a valid, non-expired JWT with a 'sub' claim.

    Mirrors the auth pattern from cli_plugins.py:plugins_shell_websocket.
    Uses jose (already a project dependency) for decoding.
    """
    if not token:
        return False
    try:
        from jose import jwt as _jwt
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        return bool(payload.get("sub"))
    except Exception:
        return False


async def _pubsub_forward(
    websocket: WebSocket,
    channel: str,
    redis,
) -> None:
    """Subscribe to *channel* and forward messages to *websocket*.

    Protocol:
    - Each Redis message is forwarded as a JSON text frame.
    - A heartbeat ping `{"type": "ping", "ts": "<iso>"}` is sent every
      _HEARTBEAT_INTERVAL seconds so the frontend can detect dead connections.
    - On client disconnect (WebSocketDisconnect or CancelledError) the pubsub
      is cleanly unsubscribed and closed.

    Message format on the wire:
    - Publishers (vault_watcher, vault_compactor) call
      `redis.publish(channel, json.dumps({...}))`.
    - With decode_responses=True the listener receives a str.
    - We forward that str as-is (already valid JSON).
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    ping_task: asyncio.Task | None = None

    async def _send_heartbeats() -> None:
        """Background task: send a ping frame every _HEARTBEAT_INTERVAL seconds."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                ts = datetime.now(timezone.utc).isoformat()
                await websocket.send_text(json.dumps({"type": "ping", "ts": ts}))
            except Exception:
                break

    try:
        ping_task = asyncio.create_task(_send_heartbeats())

        async for message in pubsub.listen():
            if message["type"] != "message":
                # Skip subscribe-confirmation and other control messages
                continue
            data = message["data"]
            # data is str (decode_responses=True) — forward as-is
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            try:
                await websocket.send_text(data)
            except WebSocketDisconnect:
                break
            except Exception:
                break

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.warning("vault pubsub forward error on %s: %s", channel, exc)
    finally:
        if ping_task is not None and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:
            pass


@router.websocket("/stream")
async def vault_stream_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
):
    """WebSocket: live vault change events from Redis channel `vault:stream`.

    Populated by VaultWatcher (file modified) and VaultCompactor (envelope
    compacted / conflict detected). Powers live-updates in the M.4 3D graph.

    Auth: JWT via `?token=<jwt>` query parameter (same pattern as
    /plugins/shell/ws in cli_plugins.py).

    Close codes:
    - 4001: missing or invalid JWT
    """
    if not _ws_validate_jwt(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    redis = await get_redis()
    logger.info("WS /vault/stream connected")
    try:
        await _pubsub_forward(websocket, "vault:stream", redis)
    finally:
        logger.info("WS /vault/stream disconnected")
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/voice-highlight")
async def vault_voice_highlight_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
):
    """WebSocket: voice-driven graph highlight commands from `voice:graph-highlight`.

    Published by the voice worker (M.4 T5) when the user mentions a vault node
    during a voice session. The frontend 3D graph subscribes here and highlights
    the matching node.

    Auth: same JWT query-param pattern as /vault/stream.

    Close codes:
    - 4001: missing or invalid JWT
    """
    if not _ws_validate_jwt(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    redis = await get_redis()
    logger.info("WS /vault/voice-highlight connected")
    try:
        await _pubsub_forward(websocket, "voice:graph-highlight", redis)
    finally:
        logger.info("WS /vault/voice-highlight disconnected")
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/voice-display")
async def vault_voice_display_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
):
    """WebSocket: voice-driven display cards from `voice:display`.

    Published by the voice worker when xAI invokes a ``show_*`` function-tool
    (memory / url / file / task). The frontend VoiceDrawer appends the card
    to its Stack with a stagger animation.

    Auth: same JWT query-param pattern as /vault/stream.

    Close codes:
    - 4001: missing or invalid JWT
    """
    if not _ws_validate_jwt(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    redis = await get_redis()
    logger.info("WS /vault/voice-display connected")
    try:
        await _pubsub_forward(websocket, "voice:display", redis)
    finally:
        logger.info("WS /vault/voice-display disconnected")
        try:
            await websocket.close()
        except Exception:
            pass
