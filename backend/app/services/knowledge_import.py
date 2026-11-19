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
   read again; a file that is gone loses its row, but a source folder that is
   missing keeps all its rows. Under ``projects/<slug>/`` a frontmatter
   ``project`` that names a *different* project (by slug, id, ``project-<id>``
   or name) flags the file (``problem = project_mismatch``) — it is not shown
   in that project, and the file is not moved. Symlinks that leave their
   folder are skipped. All file I/O runs in a worker thread; one sweep at a
   time (process lock + Postgres advisory lock, else ``SyncBusy``).

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
from sqlalchemy import text as sa_text
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


class SyncBusy(RuntimeError):
    """Another sweep holds the lock (this process or another one)."""


#: One sweep at a time in this process …
_local_lock = asyncio.Lock()
#: … and across processes (API + worker): a Postgres advisory lock held for
#: the sweep's transaction. Any constant works; this is "KNOW" + 104.
_PG_LOCK_KEY = 0x4B4E4F57_0104


@dataclass
class Known:
    """The project folders of this sweep: slug → project id, plus every
    name a note may use in its frontmatter ``project`` (folder slug, project
    id, the brief's ``project-<id>``, the slugified name) → slugs."""

    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    aliases: dict[str, set[str]] = field(default_factory=dict)

    def add(self, slug: str, project_id: uuid.UUID, name: str) -> None:
        self.ids[slug] = project_id
        for alias in (slug, str(project_id), f"project-{project_id}", knowledge.slugify(name)):
            self.aliases.setdefault(alias.lower(), set()).add(slug)

    def resolve(self, hint: str | None) -> set[str]:
        """Folder slugs a hint can mean; empty when it names no project
        (a board slug such as ``mc-dev``, a typo …)."""
        if not hint:
            return set()
        h = hint.strip().lower()
        return set(self.aliases.get(h, set())) | set(self.aliases.get(knowledge.slugify(hint), set()))


def _read_meta(folder: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads((folder / ".mc" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _brief_project_id(folder: Path) -> str | None:
    """The project id from ``PROJECT.md`` (``id: project-<uuid>``), used
    when ``.mc/project.json`` went missing (copy without hidden files …)."""
    try:
        fm = frontmatter.loads((folder / "PROJECT.md").read_text(encoding="utf-8")).metadata
    except Exception:  # noqa: BLE001 — no brief or a broken header: nothing to adopt
        return None
    pid = fm.get("id")
    if isinstance(pid, str) and pid.startswith("project-"):
        try:
            return str(uuid.UUID(pid[len("project-"):]))
        except ValueError:
            return None
    return None


def existing_project_folders(root: Path) -> dict[str, str]:
    """project id → slug, from every ``projects/*/.mc/project.json`` — or,
    when that file is gone, from the id in the folder's ``PROJECT.md``."""
    out: dict[str, str] = {}
    adopt: dict[str, str] = {}
    base = root / "projects"
    if not base.is_dir():
        return out
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        meta = _read_meta(folder)
        if meta and isinstance(meta.get("id"), str):
            out.setdefault(meta["id"], folder.name)
        elif meta is None:
            pid = _brief_project_id(folder)
            if pid:
                adopt.setdefault(pid, folder.name)
    for pid, slug in adopt.items():
        out.setdefault(pid, slug)  # a folder with project.json always wins
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


def _sync_folders_fs(root: Path, projects: list[Project], repos: dict[Any, Repo], stats: SyncStats) -> Known:
    """The file part of ``sync_project_folders`` (runs in a worker thread)."""
    ensure_layout(root)
    by_id = existing_project_folders(root)
    taken = set(by_id.values())
    base = root / "projects"
    if base.is_dir():
        taken.update(entry.name for entry in base.iterdir())
    known = Known()

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
        known.add(slug, p.id, p.name)
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
    return known


async def sync_project_folders(session: AsyncSession, root: Path, stats: SyncStats) -> Known:
    """Create / refresh one folder per project (layout included). Returns the
    project folders that exist now. DB reads here, file work in a thread."""
    projects = list((await session.exec(select(Project).order_by(Project.created_at, Project.id))).all())
    repo_ids = {p.repo_id for p in projects if p.repo_id}
    repos = {}
    if repo_ids:
        repos = {r.id: r for r in (await session.exec(select(Repo).where(Repo.id.in_(repo_ids)))).all()}
    return await asyncio.to_thread(_sync_folders_fs, root, projects, repos, stats)


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


def _place(row: KnowledgeEntry, path_slug: str | None, known: Known) -> None:
    """Project slug, project id and problem of a row from its path and its
    frontmatter hint. A hint may be the folder slug, the project id, the
    brief's ``project-<id>`` or the project name; a hint that names no project
    (the vault uses board slugs there) is ignored. Only a hint that names a
    *different* project flags a file under ``projects/<slug>/``."""
    meant = known.resolve(row.project_hint)
    if row.drawer == "projects":
        row.project_slug = path_slug
        row.problem = "project_mismatch" if meant and path_slug not in meant else None
    else:
        row.project_slug = next(iter(meant)) if len(meant) == 1 else None
        row.problem = None
    row.project_id = known.ids.get(row.project_slug) if row.project_slug else None


def _parse(raw: bytes, rel_path: str, default_kind: str) -> dict[str, Any]:
    """Index fields of one file (pure; runs in the scan thread)."""
    out: dict[str, Any] = {"content_hash": _sha(raw), "size_bytes": len(raw)}
    if len(raw) > knowledge.MAX_INDEX_BYTES:
        out.update(title=rel_path.rsplit("/", 1)[-1][:300], tags=[], search_text=None,
                   project_hint=None, kind=default_kind)
        return out
    text = raw.decode("utf-8-sig", errors="replace").replace("\x00", "")
    try:
        post = frontmatter.loads(text)
        meta, body = dict(post.metadata), post.content
    except Exception:  # noqa: BLE001 — a broken header is still a note
        meta, body = {}, text
    title = _title(meta, body, rel_path)
    tags = _tags(meta)
    kind = meta.get("type") if default_kind == "note" else default_kind
    hint = meta.get("project")
    out.update(
        title=title,
        tags=tags,
        kind=str(kind)[:40] if isinstance(kind, str) and kind.strip() else default_kind,
        project_hint=str(hint).strip()[:120] if isinstance(hint, (str, int)) and str(hint).strip() else None,
        search_text=f"{title}\n{' '.join(tags)}\n{body}"[: knowledge.SEARCH_TEXT_CAP],
    )
    return out


@dataclass
class _Seen:
    rel: str
    drawer: str
    path_slug: str | None
    default_kind: str
    mtime: float = 0.0
    #: None = size and mtime unchanged, not read. Otherwise the parsed fields
    #: (only ``content_hash`` when the content equals the stored hash).
    parsed: dict[str, Any] | None = None
    error: str | None = None


def _scan(source: str, root: Path, stored: dict[str, tuple[int | None, datetime | None, str | None]]) -> list[_Seen] | None:
    """Walk, stat, read and parse one source — all the file I/O of the index,
    off the event loop. ``None`` when the source folder is missing."""
    if not root.is_dir():
        return None
    out: list[_Seen] = []
    for rel, path in _walk(source, root):
        placed = knowledge.classify(source, rel)
        if placed is None or knowledge.resolve_inside(source, rel) is None:
            continue
        drawer, path_slug, default_kind = placed
        item = _Seen(rel, drawer, path_slug, default_kind)
        try:
            st = path.stat()
            item.mtime = st.st_mtime
            old = stored.get(rel)
            if old is not None and old[0] == st.st_size and _same_mtime(old[1], st.st_mtime):
                out.append(item)
                continue
            raw = path.read_bytes()
        except OSError as exc:
            item.error = type(exc).__name__
            out.append(item)
            continue
        sha = _sha(raw)
        item.parsed = {"content_hash": sha} if old is not None and old[2] == sha else _parse(raw, rel, default_kind)
        out.append(item)
    return out


async def index_source(session: AsyncSession, source: str, root: Path, known: Known, stats: SyncStats) -> None:
    existing = {
        r.rel_path: r
        for r in (await session.exec(select(KnowledgeEntry).where(KnowledgeEntry.source == source))).all()
    }
    stored = {rel: (r.size_bytes, r.mtime, r.content_hash) for rel, r in existing.items()}
    scanned = await asyncio.to_thread(_scan, source, root, stored)
    if scanned is None:
        if existing:  # a mount flap must not empty the index for a sweep
            stats.errors.append(f"{source}: folder missing, {len(existing)} entries kept")
        return
    seen: set[str] = set()
    for item in scanned:
        rel = item.rel
        row = existing.get(rel)
        if item.error is not None:
            stats.errors.append(f"{source}:{rel}: {item.error}")
            if row is not None:
                seen.add(rel)  # keep the old row while the file is unreadable
            continue
        seen.add(rel)
        if item.parsed is None or (row is not None and set(item.parsed) == {"content_hash"}):
            # unchanged by stat, or same content with a new mtime
            before = (row.project_slug, row.project_id, row.problem, row.mtime)
            if item.parsed is not None:
                row.mtime = _mtime_dt(item.mtime)
            _place(row, item.path_slug, known)
            if (row.project_slug, row.project_id, row.problem, row.mtime) != before:
                session.add(row)
            stats.unchanged += 1
            stats.problems += 1 if row.problem else 0
            continue
        new = row is None
        if new:
            row = KnowledgeEntry(source=source, rel_path=rel, drawer=item.drawer)
        row.drawer = item.drawer
        for key, value in item.parsed.items():
            setattr(row, key, value)
        row.mtime = _mtime_dt(item.mtime)
        row.indexed_at = utcnow()
        _place(row, item.path_slug, known)
        session.add(row)
        stats.problems += 1 if row.problem else 0
        if new:
            stats.indexed_new += 1
        else:
            stats.updated += 1
    for rel, row in existing.items():
        if rel not in seen:
            await session.delete(row)
            stats.removed += 1


async def _take_db_lock(session: AsyncSession) -> bool:
    """Postgres: try the advisory lock for this transaction (released on
    commit or rollback). Other databases (tests): always True."""
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    if dialect != "postgresql":
        return True
    got = (await session.execute(sa_text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _PG_LOCK_KEY})).scalar()
    return bool(got)


async def sync_all(session: AsyncSession) -> SyncStats:
    """Layout + project folders + index of all three sources, one commit.
    Raises ``SyncBusy`` when another sweep runs (this or another process)."""
    if _local_lock.locked():
        raise SyncBusy("knowledge sync already running")
    async with _local_lock:
        if not await _take_db_lock(session):
            await session.rollback()
            raise SyncBusy("knowledge sync already running")
        stats = SyncStats()
        roots = knowledge.source_roots()
        root = roots["knowledge"]
        known = await sync_project_folders(session, root, stats)
        for source in knowledge.SOURCES:
            await index_source(session, source, roots[source], known, stats)
        await session.commit()
    try:
        await asyncio.to_thread(
            _write_atomic,
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
            try:
                stats = await sync_all(db)
            except SyncBusy:
                logger.info("knowledge sync: skipped, another sweep is running")
                return None
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
