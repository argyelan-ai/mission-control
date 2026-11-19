"""Knowledge in one place — folders and index sync (ADR-104, slice 1).

One sweep (``sync_all``) does three things, all idempotent:

1. **Layout.** Creates ``~/.mc/knowledge/{notes,skills,projects}`` and a
   README that explains the drawers (only when missing).
2. **Project folders.** One folder per ``projects`` row:
   ``projects/<slug>/PROJECT.md`` rendered from the row (name, description,
   status, repo, working folder, briefing, plan, repo rules),
   ``projects/<slug>/knowledge/`` and ``projects/<slug>/.mc/project.json``
   (id → slug, the hash of the last rendered brief). The slug is fixed when
   the folder is made; a rename changes the title, never the folder. A brief
   the operator edited (its hash differs from the last render) is never
   overwritten again. A project deleted in the DB keeps its folder.
3. **Index.** Reads every Markdown file of the knowledge root, the vault and
   the skills folder into ``knowledge_entries``. The vault and the skills
   folder are only read. A file whose size and mtime are unchanged is not
   read again; a file that is gone loses its row. Under ``projects/<slug>/``
   a frontmatter ``project`` that names another project flags the file
   (``problem = project_mismatch``) — it is not shown in that project, and the
   file is not moved. Symlinks that leave their folder are skipped.

Runs as ``python -m app.services.knowledge_import`` (``--dry-run`` only
counts), from the background services every ``KNOWLEDGE_ROOT_SYNC_INTERVAL``
seconds, and via ``POST /api/v1/knowledge-root/_admin/sync``. Idles while
``KNOWLEDGE_ROOT_ENABLED`` is off.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Project
from app.models.knowledge_entry import KnowledgeEntry
from app.models.repo import Repo
from app.services import knowledge
from app.utils import utcnow

logger = logging.getLogger(__name__)

README = """# Knowledge

Everything MC knows, as plain Markdown files. Three drawers:

- `notes/` — facts: decisions, lessons, references, an inbox for unsorted notes.
- `skills/` — one folder per skill with a `SKILL.md`.
- `projects/<slug>/` — one folder per project: `PROJECT.md` (the project brief),
  `knowledge/` (notes for this project).

The files are the truth. MC keeps a search index over them that it can rebuild
at any time. Edit, add or delete files with any editor; MC picks it up on its
next sweep. MC creates the project folders and writes `PROJECT.md` until you
edit it — after that it is yours. `.mc/` folders belong to MC.
"""

LAST_SYNC_FILE = ".mc/last-sync.json"


@dataclass
class SyncStats:
    projects_created: int = 0
    briefs_rendered: int = 0
    briefs_kept: int = 0
    indexed_new: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    problems: int = 0
    errors: list[str] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "projectsCreated": self.projects_created,
            "briefsRendered": self.briefs_rendered,
            "briefsKept": self.briefs_kept,
            "indexedNew": self.indexed_new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "problems": self.problems,
            "errors": self.errors[:20],
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── 1. layout ────────────────────────────────────────────────────────────


def ensure_layout(root: Path) -> None:
    for drawer in knowledge.DRAWERS:
        (root / drawer).mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        _write_atomic(readme, README)


# ── 2. project folders ───────────────────────────────────────────────────


def _read_meta(folder: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads((folder / ".mc" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def existing_project_folders(root: Path) -> dict[str, str]:
    """project id → slug, from every ``projects/*/.mc/project.json``."""
    out: dict[str, str] = {}
    base = root / "projects"
    if not base.is_dir():
        return out
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        meta = _read_meta(folder)
        if meta and isinstance(meta.get("id"), str):
            out.setdefault(meta["id"], folder.name)
    return out


def render_brief(project: Project, slug: str, repo: Repo | None) -> str:
    fm = {
        "id": f"project-{project.id}",
        "type": "project",
        "project": slug,
        "title": project.name,
        "status": project.status,
        "source": "mc",
    }
    lines = [
        "---",
        yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip(),
        "---",
        "",
        f"# {project.name}",
        "",
        "<!-- Written by MC from the project record until you edit this file; after that MC leaves it alone. -->",
        "",
    ]
    if project.description:
        lines += [project.description.strip(), ""]
    facts = [f"- Status: {project.status}"]
    repo_name = repo.full_name if repo else project.github_repo_name
    if repo_name:
        facts.append(f"- Repository: {repo_name}")
    if project.workspace_path:
        facts.append(f"- Working folder: `{project.workspace_path}`")
    lines += facts + [""]
    for heading, body in (
        ("Briefing", project.briefing_doc),
        ("Plan", project.plan_summary),
        ("Repository rules", repo.rules_md if repo else None),
    ):
        if body and body.strip():
            lines += [f"## {heading}", "", body.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


async def sync_project_folders(session: AsyncSession, root: Path, stats: SyncStats) -> dict[str, uuid.UUID]:
    """Create / refresh one folder per project. Returns slug → project id
    for every project that has a folder now."""
    projects = (await session.exec(select(Project).order_by(Project.created_at, Project.id))).all()
    repo_ids = {p.repo_id for p in projects if p.repo_id}
    repos = {}
    if repo_ids:
        repos = {r.id: r for r in (await session.exec(select(Repo).where(Repo.id.in_(repo_ids)))).all()}

    by_id = existing_project_folders(root)
    taken = set(by_id.values())
    base = root / "projects"
    if base.is_dir():
        taken.update(entry.name for entry in base.iterdir())
    slugs: dict[str, uuid.UUID] = {}

    for p in projects:
        pid = str(p.id)
        slug = by_id.get(pid)
        created = False
        if slug is None:
            slug = knowledge.slugify(p.name)
            if slug in taken:
                slug = f"{slug[:52]}-{p.id.hex[:6]}"
            taken.add(slug)
            created = True
        folder = base / slug
        slugs[slug] = p.id
        try:
            (folder / "knowledge").mkdir(parents=True, exist_ok=True)
            meta = _read_meta(folder) or {}
            brief_path = folder / "PROJECT.md"
            new_text = render_brief(p, slug, repos.get(p.repo_id) if p.repo_id else None)
            new_hash = _sha(new_text.encode("utf-8"))
            if not brief_path.exists():
                _write_atomic(brief_path, new_text)
                meta["rendered_hash"] = new_hash
                stats.briefs_rendered += 1
            else:
                current = _sha(brief_path.read_bytes())
                if current == meta.get("rendered_hash"):
                    if current != new_hash:
                        _write_atomic(brief_path, new_text)
                        meta["rendered_hash"] = new_hash
                        stats.briefs_rendered += 1
                elif current != new_hash:
                    stats.briefs_kept += 1
            before = _read_meta(folder)
            meta.update({"id": pid, "slug": slug, "name": p.name, "status": p.status})
            meta.setdefault("created", utcnow().isoformat())
            if meta != before:  # no rewrite (and no new mtime) on every sweep
                _write_atomic(folder / ".mc" / "project.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")
            if created:
                stats.projects_created += 1
        except OSError as exc:
            stats.errors.append(f"projects/{slug}: {type(exc).__name__}")
    return slugs


# ── 3. index ─────────────────────────────────────────────────────────────


def _walk(source: str, root: Path):
    """Yield (rel_path, abs_path) of every Markdown file the index wants."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in dirnames:
            if d.startswith((".", "_")):
                continue
            if source == "vault" and not rel_dir and d in knowledge.VAULT_SKIP_TOP:
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            yield rel, Path(dirpath) / name


def _title(meta: dict[str, Any], body: str, rel_path: str) -> str:
    for key in ("title", "name"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:300]
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:300]
    stem = rel_path.rsplit("/", 1)[-1][:-3]
    if stem in ("SKILL", "PROJECT") and "/" in rel_path:
        stem = rel_path.split("/")[-2]
    return stem[:300]


def _tags(meta: dict[str, Any]) -> list[str]:
    v = meta.get("tags")
    if isinstance(v, str):
        v = [t.strip() for t in v.split(",")]
    if not isinstance(v, list):
        return []
    return [str(t)[:60] for t in v if str(t).strip()][:30]


def _mtime_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _same_mtime(stored: datetime | None, ts: float) -> bool:
    if stored is None:
        return False
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return abs(stored.timestamp() - ts) < 0.001


def _place(row: KnowledgeEntry, path_slug: str | None, known: dict[str, uuid.UUID]) -> None:
    """Project slug, project id and problem of a row from its path and its
    frontmatter hint."""
    hint = row.project_hint
    if row.drawer == "projects":
        row.project_slug = path_slug
        row.problem = "project_mismatch" if hint and hint != path_slug else None
    else:
        row.project_slug = hint if hint and hint in known else None
        row.problem = None
    row.project_id = known.get(row.project_slug) if row.project_slug else None


def _fill(row: KnowledgeEntry, raw: bytes, rel_path: str, default_kind: str) -> None:
    row.content_hash = _sha(raw)
    row.size_bytes = len(raw)
    if len(raw) > knowledge.MAX_INDEX_BYTES:
        row.title = rel_path.rsplit("/", 1)[-1][:300]
        row.tags, row.search_text, row.project_hint, row.kind = [], None, None, default_kind
        return
    text = raw.decode("utf-8-sig", errors="replace").replace("\x00", "")
    try:
        post = frontmatter.loads(text)
        meta, body = dict(post.metadata), post.content
    except Exception:  # noqa: BLE001 — a broken header is still a note
        meta, body = {}, text
    row.title = _title(meta, body, rel_path)
    row.tags = _tags(meta)
    kind = meta.get("type") if default_kind == "note" else default_kind
    row.kind = str(kind)[:40] if isinstance(kind, str) and kind.strip() else default_kind
    hint = meta.get("project")
    row.project_hint = str(hint).strip()[:120] if isinstance(hint, (str, int)) and str(hint).strip() else None
    row.search_text = f"{row.title}\n{' '.join(row.tags)}\n{body}"[: knowledge.SEARCH_TEXT_CAP]


async def index_source(
    session: AsyncSession, source: str, root: Path, known: dict[str, uuid.UUID], stats: SyncStats
) -> None:
    existing = {
        r.rel_path: r
        for r in (await session.exec(select(KnowledgeEntry).where(KnowledgeEntry.source == source))).all()
    }
    seen: set[str] = set()
    if root.is_dir():
        for rel, path in _walk(source, root):
            placed = knowledge.classify(source, rel)
            if placed is None or knowledge.resolve_inside(source, rel) is None:
                continue
            drawer, path_slug, default_kind = placed
            try:
                st = path.stat()
                row = existing.get(rel)
                if row is not None and row.size_bytes == st.st_size and _same_mtime(row.mtime, st.st_mtime):
                    seen.add(rel)
                    before = (row.project_slug, row.project_id, row.problem)
                    _place(row, path_slug, known)
                    if (row.project_slug, row.project_id, row.problem) != before:
                        session.add(row)
                    stats.unchanged += 1
                    stats.problems += 1 if row.problem else 0
                    continue
                raw = path.read_bytes()
            except OSError as exc:
                stats.errors.append(f"{source}:{rel}: {type(exc).__name__}")
                if rel in existing:
                    seen.add(rel)  # keep the old row while the file is unreadable
                continue
            new = row is None
            if new:
                row = KnowledgeEntry(source=source, rel_path=rel, drawer=drawer)
            elif row.content_hash == _sha(raw):
                row.mtime = _mtime_dt(st.st_mtime)
                _place(row, path_slug, known)
                session.add(row)
                seen.add(rel)
                stats.unchanged += 1
                stats.problems += 1 if row.problem else 0
                continue
            row.drawer = drawer
            _fill(row, raw, rel, default_kind)
            row.mtime = _mtime_dt(st.st_mtime)
            row.indexed_at = utcnow()
            _place(row, path_slug, known)
            session.add(row)
            seen.add(rel)
            stats.problems += 1 if row.problem else 0
            if new:
                stats.indexed_new += 1
            else:
                stats.updated += 1
    for rel, row in existing.items():
        if rel not in seen:
            await session.delete(row)
            stats.removed += 1


async def sync_all(session: AsyncSession) -> SyncStats:
    """Layout + project folders + index of all three sources, one commit."""
    stats = SyncStats()
    roots = knowledge.source_roots()
    root = roots["knowledge"]
    ensure_layout(root)
    known = await sync_project_folders(session, root, stats)
    for source in knowledge.SOURCES:
        await index_source(session, source, roots[source], known, stats)
    await session.commit()
    try:
        _write_atomic(
            root / LAST_SYNC_FILE,
            json.dumps({"at": utcnow().isoformat(), "stats": stats.to_api()}, indent=2) + "\n",
        )
    except OSError:
        logger.warning("knowledge sync: could not write %s", LAST_SYNC_FILE)
    return stats


def last_sync() -> dict[str, Any] | None:
    try:
        data = json.loads((knowledge.knowledge_root() / LAST_SYNC_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def dry_run(session: AsyncSession) -> dict[str, int]:
    """Count what the index would hold, writing nothing."""
    out: dict[str, int] = {}
    for source, root in knowledge.source_roots().items():
        n = 0
        if root.is_dir():
            for rel, _path in _walk(source, root):
                if knowledge.classify(source, rel) is not None and knowledge.resolve_inside(source, rel):
                    n += 1
        out[source] = n
    out["projects"] = len((await session.exec(select(Project.id))).all())
    return out


# ── background ───────────────────────────────────────────────────────────


class KnowledgeSync:
    """Runs ``sync_all`` on a schedule in the background services. An
    unchanged file costs one ``stat``. Idles while the flag is off."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self.last: SyncStats | None = None

    async def sweep_once(self) -> SyncStats | None:
        if not knowledge.enabled():
            return None
        from app import database

        async with database.async_session_maker() as db:
            stats = await sync_all(db)
        if stats.errors:
            logger.warning("knowledge sync: %d error(s): %s", len(stats.errors), stats.errors[:5])
        self.last = stats
        return stats

    async def start(self) -> None:
        from app.config import settings

        interval = getattr(settings, "knowledge_root_sync_interval", 0)
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="knowledge_sync")
        logger.info("knowledge sync started (interval=%ss)", interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: int) -> None:
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001 — never kill the loop
                logger.exception("knowledge sync failed")


knowledge_sync = KnowledgeSync()


async def _main(dry: bool) -> None:
    from app.database import async_session_maker

    if not dry and not knowledge.enabled():
        raise SystemExit("KNOWLEDGE_ROOT_ENABLED is off - nothing written (use --dry-run to count)")
    async with async_session_maker() as db:
        if dry:
            print(json.dumps(await dry_run(db), indent=2))
        else:
            print(json.dumps((await sync_all(db)).to_api(), indent=2))


if __name__ == "__main__":  # pragma: no cover - manual operator tool
    import argparse

    parser = argparse.ArgumentParser(description="Build the knowledge root and its index (ADR-104).")
    parser.add_argument("--dry-run", action="store_true", help="only count, write nothing")
    args = parser.parse_args()
    asyncio.run(_main(args.dry_run))
