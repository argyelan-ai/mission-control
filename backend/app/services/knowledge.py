"""Knowledge in one place — layout, classification and reads (ADR-104, slice 1).

One root, three drawers::

    ~/.mc/knowledge/
    ├── README.md
    ├── notes/                 facts (decisions, lessons, references, inbox …)
    ├── skills/<name>/SKILL.md
    └── projects/<slug>/       PROJECT.md, knowledge/, .mc/project.json

The files are the truth. ``knowledge_entries`` is an index over them and over
the two existing folders that are read in place in this slice — the vault
(``settings.vault_path``) and the skills folder (``~/.mc/skills``). Slice 1
writes only into the knowledge root (project folders, README) and the index;
the vault and the skills folder are never written.

Writing the index and the folders lives in ``services/knowledge_import.py``.
Everything is behind ``settings.knowledge_root_enabled`` (default off).
"""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.knowledge_entry import KnowledgeEntry
from app.services.fs_roots import mc_home

DRAWERS = ("notes", "skills", "projects")
SOURCES = ("knowledge", "vault", "skills")

#: Text kept per file for full-text search (title + tags + body). The body
#: itself is always read from disk; this is only what search can find.
SEARCH_TEXT_CAP = 64_000

#: Files larger than this are listed but not read into the index (a pasted
#: log is not knowledge; the file stays readable through the API).
MAX_INDEX_BYTES = 2_000_000

#: At most this much of a file goes into one ``GET /entries/{id}`` answer;
#: the rest is cut and the answer says ``truncated``.
MAX_BODY_BYTES = 2_000_000

#: Vault folders that are machine state, trash or binary storage — not
#: knowledge. ``jobs/`` and ``runs/`` are task-era history (slice 4).
VAULT_SKIP_TOP = frozenset({"attachments", "jobs", "runs"})


def enabled() -> bool:
    return bool(getattr(settings, "knowledge_root_enabled", False))


def knowledge_root() -> Path:
    return mc_home() / "knowledge"


def source_roots() -> dict[str, Path]:
    return {
        "knowledge": knowledge_root(),
        "vault": Path(settings.vault_path),
        "skills": mc_home() / "skills",
    }


# ── slugs ────────────────────────────────────────────────────────────────


def slugify(name: str) -> str:
    """``"Online Shop!"`` → ``online-shop``; never empty, at most 60 chars."""
    norm = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")[:60].strip("-")
    return slug or "project"


# ── which file belongs where ─────────────────────────────────────────────


def _hidden_or_private(parts: tuple[str, ...]) -> bool:
    return any(p.startswith((".", "_")) for p in parts)


def classify(source: str, rel_path: str) -> tuple[str, str | None, str] | None:
    """Drawer, project slug (from the path) and default kind of a file —
    or ``None`` when the file is not knowledge (skipped by the index)."""
    if not rel_path.endswith(".md"):
        return None
    parts = tuple(rel_path.split("/"))
    if source == "knowledge":
        if len(parts) < 2 or _hidden_or_private(parts[:-1]) or parts[-1].startswith("."):
            return None
        top = parts[0]
        if top == "notes":
            return "notes", None, "note"
        if top == "skills":
            if len(parts) == 3 and parts[2] == "SKILL.md":
                return "skills", None, "skill"
            return None
        if top == "projects" and len(parts) >= 3:
            slug = parts[1]
            kind = "project" if len(parts) == 3 and parts[2] == "PROJECT.md" else "note"
            return "projects", slug, kind
        return None
    if source == "vault":
        if _hidden_or_private(parts) or parts[0] in VAULT_SKIP_TOP or len(parts) < 2:
            return None
        return "notes", None, "note"
    if source == "skills":
        if len(parts) == 2 and parts[1] == "SKILL.md" and not _hidden_or_private(parts):
            return "skills", None, "skill"
        return None
    return None


def resolve_inside(source: str, rel_path: str) -> Path | None:
    """The absolute file of an entry, or ``None`` when its real path leaves
    the source folder (a symlink pointing elsewhere, ``..``)."""
    root = source_roots().get(source)
    if root is None:
        return None
    try:
        real_root = os.path.realpath(root)
        real = os.path.realpath(root / rel_path)
    except OSError:
        return None
    if real != real_root and not real.startswith(real_root + os.sep):
        return None
    return Path(real)


# ── reads ────────────────────────────────────────────────────────────────


def entry_dict(e: KnowledgeEntry) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "source": e.source,
        "path": e.rel_path,
        "drawer": e.drawer,
        "project": e.project_slug,
        "projectId": str(e.project_id) if e.project_id else None,
        "kind": e.kind,
        "title": e.title,
        "tags": list(e.tags or []),
        "sizeBytes": e.size_bytes,
        "modifiedAt": e.mtime.isoformat() if e.mtime else None,
        "problem": e.problem,
    }


def _filtered(q, *, drawer: str | None, project: str | None, source: str | None):
    if drawer:
        q = q.where(KnowledgeEntry.drawer == drawer)
    if project:
        q = q.where(KnowledgeEntry.project_slug == project).where(col(KnowledgeEntry.problem).is_(None))
    if source:
        q = q.where(KnowledgeEntry.source == source)
    return q


async def list_entries(
    session: AsyncSession,
    *,
    drawer: str | None = None,
    project: str | None = None,
    source: str | None = None,
    problems: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    q = _filtered(select(KnowledgeEntry), drawer=drawer, project=project, source=source)
    cq = _filtered(select(func.count()).select_from(KnowledgeEntry), drawer=drawer, project=project, source=source)
    if problems:
        q = q.where(col(KnowledgeEntry.problem).is_not(None))
        cq = cq.where(col(KnowledgeEntry.problem).is_not(None))
    q = q.order_by(KnowledgeEntry.drawer, KnowledgeEntry.source, KnowledgeEntry.rel_path).offset(offset).limit(limit)
    rows = (await session.exec(q)).all()
    total = (await session.exec(cq)).one()
    return {"entries": [entry_dict(r) for r in rows], "total": int(total)}


async def get_entry(session: AsyncSession, entry_id: uuid.UUID) -> KnowledgeEntry | None:
    return await session.get(KnowledgeEntry, entry_id)


def read_body(e: KnowledgeEntry) -> tuple[str, bool, bool] | None:
    """Body of an entry read from disk now (at most ``MAX_BODY_BYTES``),
    "changed since indexed" and "cut". ``None`` when the file is gone or
    outside its folder. The hash still covers the whole file."""
    import hashlib

    path = resolve_inside(e.source, e.rel_path)
    if path is None or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        head = fh.read(MAX_BODY_BYTES)
        h.update(head)
        truncated = False
        while chunk := fh.read(1 << 20):
            truncated = True
            h.update(chunk)
    stale = h.hexdigest() != e.content_hash
    return head.decode("utf-8", errors="replace"), stale, truncated


async def search(
    session: AsyncSession,
    query: str,
    *,
    drawer: str | None = None,
    project: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Postgres: full-text with the ``simple`` configuration (no stemming —
    notes mix German and English), GIN index from migration 0228. SQLite
    (tests): case-insensitive substring."""
    query = (query or "").strip()
    if not query:
        return []
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    q = _filtered(select(KnowledgeEntry), drawer=drawer, project=project, source=None)
    if dialect == "postgresql":
        doc = func.to_tsvector("simple", func.coalesce(KnowledgeEntry.search_text, ""))
        tsq = func.plainto_tsquery("simple", query)
        q = q.where(doc.op("@@")(tsq)).order_by(func.ts_rank(doc, tsq).desc(), KnowledgeEntry.rel_path)
    else:
        q = q.where(col(KnowledgeEntry.search_text).ilike(f"%{query}%")).order_by(KnowledgeEntry.rel_path)
    out = []
    first = query.lower().split()[0]
    for e in (await session.exec(q.limit(limit))).all():
        text = e.search_text or ""
        idx = text.lower().find(first)
        start = max(0, idx - 60) if idx >= 0 else 0
        item = entry_dict(e)
        item["snippet"] = text[start : start + 200]
        out.append(item)
    return out


async def list_projects(session: AsyncSession) -> list[dict[str, Any]]:
    """Every project folder the index knows, with its name from PROJECT.md
    and how many entries it holds — every entry ``/entries?project=<slug>``
    returns, so folder files plus vault notes that name the project (flagged
    ones not counted)."""
    rows = (
        await session.exec(
            select(KnowledgeEntry)
            .where(col(KnowledgeEntry.project_slug).is_not(None))
            .where(col(KnowledgeEntry.problem).is_(None))
        )
    ).all()
    by_slug: dict[str, dict[str, Any]] = {}
    for e in rows:
        slug = e.project_slug or ""
        p = by_slug.setdefault(slug, {"slug": slug, "projectId": None, "name": slug, "entries": 0})
        p["entries"] += 1
        if e.kind == "project" and e.drawer == "projects":
            p["name"] = e.title or slug
            p["projectId"] = str(e.project_id) if e.project_id else None
    return sorted(by_slug.values(), key=lambda p: p["slug"])


async def counts(session: AsyncSession) -> dict[str, Any]:
    drawer_rows = (
        await session.exec(select(KnowledgeEntry.drawer, func.count()).group_by(KnowledgeEntry.drawer))
    ).all()
    source_rows = (
        await session.exec(select(KnowledgeEntry.source, func.count()).group_by(KnowledgeEntry.source))
    ).all()
    problems = (
        await session.exec(
            select(func.count()).select_from(KnowledgeEntry).where(col(KnowledgeEntry.problem).is_not(None))
        )
    ).one()
    by_drawer = {d: 0 for d in DRAWERS}
    by_drawer.update({d: int(n) for d, n in drawer_rows})
    by_source = {s: 0 for s in SOURCES}
    by_source.update({s: int(n) for s, n in source_rows})
    return {"counts": by_drawer, "bySource": by_source, "problems": int(problems)}
