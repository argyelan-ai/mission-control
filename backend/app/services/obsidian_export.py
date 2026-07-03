"""Obsidian View-Only Export — background singleton for OBS-01/02/03 (Phase 7).

Periodically (default 300s) walks ``board_memory`` + task attachments and
renders Markdown files into ``${HOME_HOST}/.mc/vault/`` so the operator can
read their memory store in Obsidian (or any Markdown-focused app) — a strictly
READ-only mirror, MC stays the single source of truth (no reverse sync).

Mirrors the singleton pattern from ``intelligence.py`` / ``embedding_retry.py``
(in both cases: lifespan registration in ``main.py`` analogous to
``embedding_retry.start()`` / ``embedding_retry.stop()``).

Acceptance contracts (Plan 07-01 — Wave 1 skeleton):
- OBS-01: vault directory layout (memory/{agents,projects,global} +
  attachments/{tasks,deliverables}) is created on the first ``.start()``.
- OBS-02: ``settings.obsidian_export_enabled`` kill switch + ``_run_loop``
  with grace period (20s) and Redis lock dedup across multi-worker.
- OBS-03: ``_vault_attachment_path`` helper for attachment mirroring (Plan
  07-03 fills the body).

Plan 07-01 delivers ONLY the infrastructure — ``trigger_cycle()`` is a
``pass`` stub here. The pipeline implementation lands in Plan 07-02
(cycle body) and Plan 07-03 (attachments).

Pitfalls (see ``.planning/phases/07-obsidian-view-only-export/07-RESEARCH.md``):
- ``_vault_root()`` MUST resolve via HOME_HOST → HOME → expanduser('~').
  ``feedback_home_host_pattern.md`` — standalone ``expanduser('~')`` is
  forbidden because ``$HOME`` in the container points at ``/home/mcuser``
  while the mount has to live at the host HOME (``HOME_HOST``, e.g.
  ``/Users/<login>``).
- Path-traversal guard on every computed path: realpath + startswith
  under ``_vault_root()``. Pattern verbatim from
  ``routers/memory.py:530-545``.
- Lifespan calls ``.start()`` / ``.stop()`` — no auto-start here (Pitfall 4
  from EmbeddingRetryLoop).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Optional

import yaml
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import engine
from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.memory import BoardMemory
from app.redis_client import RedisKeys, get_redis
from app.routers.memory import _attachments_root
from app.services.git_service import slugify_project

logger = logging.getLogger("mc.obsidian_export")


def _vault_root() -> str:
    """Phase 7 OBS-01: HOME_HOST resolver for the vault directory.

    NEVER standalone ``expanduser('~')`` — memory feedback rule
    ``feedback_home_host_pattern.md``. The chain is:
    ``HOME_HOST`` env var (set on Docker containers via host-side
    docker-compose mount) → ``HOME`` env var → ``expanduser('~')``
    last resort. Returns ``${HOME_HOST}/.mc/vault``.

    Mirrors ``_attachments_root()`` (routers/memory.py:42-52) — same
    resolver chain, different sub-path.
    """
    home_host = os.environ.get("HOME_HOST") or os.environ.get("HOME") or os.path.expanduser("~")
    return f"{home_host}/.mc/vault"


def _ensure_vault_layout(vault_root: str) -> None:
    """Phase 7 OBS-01: create the vault directory tree.

    Idempotent — ``exist_ok=True`` on every ``makedirs`` call. Called on the
    first ``ObsidianExportService.start()`` + imported by Plan 07-02 /
    07-03 tests for layout assertions.

    The subdir list is the single source of truth for Plan 07-02
    (``_vault_memory_path`` routing) and Plan 07-03 (``_vault_attachment_path``
    routing).
    """
    subdirs = (
        "memory/agents",
        "memory/projects",
        "memory/global",
        "attachments/tasks",
        "attachments/deliverables",
    )
    for sub in subdirs:
        os.makedirs(os.path.join(vault_root, sub), exist_ok=True)


def _safe_join(vault_root: str, *parts: str) -> str:
    """Path-traversal guard wrapper — pattern verbatim from
    ``routers/memory.py:530-545``.

    Service context: ``RuntimeError`` instead of ``HTTPException``. Callers
    in Plan 07-02 / 07-03 wrap it as needed in ``logger.error`` + skip.
    """
    target = os.path.join(vault_root, *parts)
    real_root = os.path.realpath(vault_root)
    real_target = os.path.realpath(target)
    if not (real_target == real_root or real_target.startswith(real_root + os.sep)):
        raise RuntimeError(f"Path escapes vault root: {target}")
    return target


def _vault_memory_path(
    entry,
    agent_slug: str | None = None,
    project_slug: str | None = None,
) -> str:
    """Phase 7 OBS-01: routing for ``BoardMemory`` entries.

    Routing table (RESEARCH.md "Vault Layout"):
    - ``entry.agent_id is not None`` → ``memory/agents/{agent_slug}/``
    - ``entry.board_id is not None`` + ``project_slug`` →
      ``memory/projects/{project_slug}/``
    - ``entry.board_id is not None`` + no ``project_slug`` →
      ``memory/projects/_unprojected/{board_id-short}/``
    - otherwise → ``memory/global/``

    Filename: ``{slugify(title|content[:60]|id-short)}_{id-short}.md``.
    Plan 07-02 fills the cycle body that calls this function + writes the
    Markdown file.
    """
    vault_root = _vault_root()
    entry_id_short = str(entry.id)[:8]

    # Routing
    if entry.agent_id is not None:
        slug = agent_slug or str(entry.agent_id)[:8]
        sub_parts = ("memory", "agents", slug)
    elif entry.board_id is not None:
        if project_slug:
            sub_parts = ("memory", "projects", project_slug)
        else:
            board_short = str(entry.board_id)[:8]
            sub_parts = ("memory", "projects", "_unprojected", board_short)
    else:
        sub_parts = ("memory", "global")

    # Filename
    title_or_content = entry.title or (entry.content or "")[:60].split("\n")[0] or entry_id_short
    name_slug = slugify_project(title_or_content) or entry_id_short
    filename = f"{name_slug}_{entry_id_short}.md"

    return _safe_join(vault_root, *sub_parts, filename)


def _render_frontmatter(
    entry: BoardMemory,
    agent_slug: str | None,
    project_slug: str | None,
) -> str:
    """Phase 7 OBS-02: render YAML frontmatter for a BoardMemory row.

    Schema (deterministic key order — Pitfall 1):

    ::

        ---
        title: <str>
        type: <memory_type>
        tags: [<str>, ...]            # Pitfall 7 coerce + drop None
        date: <ISO-8601 sec>
        agent: <slug | null>
        project: <slug | null>
        status: <memory_type>          # placeholder until status field exists
        ---

    Notes
    -----
    - ``yaml.safe_dump(sort_keys=False, allow_unicode=True,
      default_flow_style=False)`` — Pitfall 1 closes deterministic order.
    - ``tags`` coerced via ``[str(t) for t in (entry.tags or []) if t is not None]``
      — Pitfall 7 (None values pollute Obsidian Properties UI).
    - ``date`` uses ``entry.updated_at.isoformat(timespec="seconds")`` so
      identical rows produce byte-identical frontmatter (idempotency).
    - ``agent_slug=None`` / ``project_slug=None`` MUST emit YAML ``null``
      (yaml.safe_dump renders Python ``None`` as ``null`` by default).
    """
    title_fallback = (
        (entry.content[:60].split("\n")[0] if entry.content else None)
        or str(entry.id)[:8]
    )
    title = entry.title or title_fallback

    tags_coerced = [str(t) for t in (entry.tags or []) if t is not None]

    if entry.updated_at is not None:
        date_str = entry.updated_at.isoformat(timespec="seconds")
    else:
        date_str = datetime.utcnow().isoformat(timespec="seconds")

    # Insertion order = literal order = YAML output order (Python 3.7+).
    meta: dict[str, Any] = {
        "title": title,
        "type": entry.memory_type,
        "tags": tags_coerced,
        "date": date_str,
        "agent": agent_slug,
        "project": project_slug,
        "status": entry.memory_type,
    }
    yaml_str = yaml.safe_dump(
        meta,
        sort_keys=False,            # Pitfall 1 — preserve key order
        allow_unicode=True,
        default_flow_style=False,
    )
    return f"---\n{yaml_str}---\n"


def _render_body(entry: BoardMemory) -> str:
    """Phase 7 OBS-02: render Markdown body for a BoardMemory row.

    Layout::

        # <title>

        <content>

        ---
        **Source:** <source>
        **Linked:** [[uuid1]], [[uuid2]]
        **Auto-generated:** True/False
        **Pinned:** True/False

    Notes
    -----
    - Title uses the same fallback chain as ``_render_frontmatter`` so the
      ``# heading`` matches the YAML frontmatter ``title:``.
    - The footer is ALWAYS present (even when ``source`` / ``linked_ids`` are
      empty) so the body shape is deterministic — required for idempotency.
    """
    title_fallback = (
        (entry.content[:60].split("\n")[0] if entry.content else None)
        or str(entry.id)[:8]
    )
    title = entry.title or title_fallback

    body = f"# {title}\n\n{entry.content or ''}\n"

    footer_parts: list[str] = []
    if entry.source:
        footer_parts.append(f"**Source:** {entry.source}")
    if entry.linked_ids:
        links = ", ".join(f"[[{lid}]]" for lid in entry.linked_ids)
        footer_parts.append(f"**Linked:** {links}")
    footer_parts.append(f"**Auto-generated:** {entry.auto_generated}")
    footer_parts.append(f"**Pinned:** {entry.is_pinned}")
    body += "\n---\n" + "\n".join(footer_parts) + "\n"
    return body


def _atomic_write(target: str, content: str) -> None:
    """Phase 7 OBS-02: atomic file write via tempfile + os.replace (POSIX).

    Pattern verbatim from RESEARCH.md lines 626-645. ``tempfile.mkstemp``
    creates the .tmp file in the SAME directory as ``target`` so
    ``os.replace`` stays on the same filesystem (no cross-device fallback).
    """
    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)        # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _write_if_changed(target: str, content: str) -> bool:
    """Phase 7 OBS-02: idempotent write — SHA-256 short-circuit.

    Returns
    -------
    bool
        True if the file was written (new file or content changed).
        False if the existing file's SHA-256 already matches ``content`` —
        no FS touch, mtime preserved.

    Idempotency invariant
    ---------------------
    Calling ``_write_if_changed(p, c)`` twice with identical ``c`` writes
    once. ``os.path.getmtime(p)`` is unchanged on the second call.
    """
    new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as f:
            old_hash = hashlib.sha256(f.read().encode("utf-8")).hexdigest()
        if old_hash == new_hash:
            return False
    _atomic_write(target, content)
    return True


async def _resolve_agent_slug(
    entry: BoardMemory,
    session: AsyncSession,
) -> str | None:
    """Phase 7 OBS-02: resolve ``entry.agent_id`` → agent name slug.

    Returns ``None`` for global rows (``agent_id is None``) so the caller
    routes to ``memory/global/``. Defensive: returns ``None`` if the agent
    row is missing (foreign-key dangling) — should not happen but cheap.
    """
    if entry.agent_id is None:
        return None
    agent = await session.get(Agent, entry.agent_id)
    if agent is None or not agent.name:
        return None
    return slugify_project(agent.name) or None


async def _resolve_project_slug(
    entry: BoardMemory,
    session: AsyncSession,
) -> str | None:
    """Phase 7 OBS-02: resolve ``entry.board_id`` → project name slug.

    Routing: BoardMemory has ``board_id`` (no direct project_id). The board
    row's ``default_project_id`` points at the project to slug. If the
    board has no default_project_id, return ``None`` so the caller routes
    to ``memory/projects/_unprojected/{board_short}/``.
    """
    if entry.board_id is None:
        return None
    board = await session.get(Board, entry.board_id)
    if board is None or board.default_project_id is None:
        return None
    project = await session.get(Project, board.default_project_id)
    if project is None or not project.name:
        return None
    return slugify_project(project.name) or None


def _vault_attachment_path(entry, filename: str, category: str = "tasks") -> str:
    """Phase 7 OBS-03: routing for attachment mirroring.

    ``category`` MUST be ``"tasks"`` or ``"deliverables"`` (A2 default in
    RESEARCH.md = "everything to tasks/"). Plan 07-03 fills the mirror body
    that calls this function and copies / links the file.
    """
    if category not in ("tasks", "deliverables"):
        raise ValueError(f"category must be 'tasks' or 'deliverables', got {category!r}")
    vault_root = _vault_root()
    return _safe_join(vault_root, "attachments", category, str(entry.id), filename)


def _resolve_collision_safe_attachments(
    attachments: list[dict] | None,
) -> list[dict]:
    """Phase 7 OBS-03 (Pitfall 5): assign collision-safe ``display_name``.

    When two attachments under the same memory_id share the same
    ``original_name`` (e.g., two ``screen.png`` files uploaded by the user),
    we keep the sha16-prefix in the on-disk vault filename AND in the
    Wiki-Link reference. This way both notes resolve to their own image
    in Obsidian without one shadowing the other.

    Behavior
    --------
    - Returns ``[]`` if ``attachments`` is None or empty.
    - For unique ``original_name`` → ``display_name = original_name``.
    - For colliding ``original_name`` → ``display_name = {sha16}-{original_name}``
      where ``sha16`` is extracted from ``att.path`` (segment before the
      first ``-`` in the basename, matching MSY-03 storage convention).
    - Input list is NOT mutated; new dicts are returned with ``display_name``
      added.

    Notes
    -----
    The sha16 prefix is robust to off-by-one path shapes — we walk the
    basename, then split on ``-`` once to grab the prefix. If extraction
    fails (no ``-`` in basename) we fall back to ``original_name`` (no
    prefix), which mirrors the pre-collision behavior — the worst case is
    a single confused image, not a crash.
    """
    if not attachments:
        return []

    counts: dict[str, int] = {}
    for att in attachments:
        name = att.get("original_name") or ""
        counts[name] = counts.get(name, 0) + 1

    out: list[dict] = []
    for att in attachments:
        original = att.get("original_name") or ""
        path = att.get("path") or ""
        if counts.get(original, 0) > 1:
            basename = os.path.basename(path)
            if "-" in basename:
                sha_prefix, _, _rest = basename.partition("-")
                display = f"{sha_prefix}-{original}" if sha_prefix else original
            else:
                display = original
        else:
            display = original
        new = dict(att)
        new["display_name"] = display
        out.append(new)
    return out


def _mirror_attachment(src_abs: str, dest_abs: str) -> bool:
    """Phase 7 OBS-03: idempotent file mirror.

    Returns
    -------
    bool
        ``True`` after copy (new file or stale destination overwritten).
        ``False`` on size+mtime skip OR when ``src_abs`` does not exist
        (defensive: a row may reference an attachment that has since been
        deleted; the cycle must not crash).

    Idempotency contract
    --------------------
    Skip copy when destination exists AND ``dst.st_size == src.st_size``
    AND ``dst.st_mtime >= src.st_mtime``. ``shutil.copy2`` preserves mtime
    so subsequent skip detection works.

    Notes
    -----
    - ``os.makedirs(os.path.dirname(dest_abs), exist_ok=True)`` runs before
      copy so the parent directory tree auto-creates.
    - Missing source files emit a WARN log + return ``False`` (do NOT raise).
      This mirrors ``intelligence.py`` per-row defensive pattern.
    """
    if not os.path.isfile(src_abs):
        logger.warning("ObsidianExport mirror: source missing, skipping: %s", src_abs)
        return False

    if os.path.isfile(dest_abs):
        try:
            src_st = os.stat(src_abs)
            dst_st = os.stat(dest_abs)
            if (
                dst_st.st_size == src_st.st_size
                and dst_st.st_mtime >= src_st.st_mtime
            ):
                return False
        except FileNotFoundError:
            # Race: destination disappeared between isfile and stat. Fall
            # through and copy.
            pass

    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
    shutil.copy2(src_abs, dest_abs)
    return True


def _rewrite_wikilinks(
    body: str,
    attachments: list[dict] | None,
    memory_id: str,
) -> str:
    """Phase 7 OBS-03: precise wikilink rewrite (Pitfall 6).

    For every known attachment (whose ``rel_path`` and ``original_name``
    are both populated), perform — in order:

    1. **Image syntax** ``![ANY_ALT](needle)`` → ``![[display_name]]`` for
       ``needle`` ∈ {``rel_path``, ``original_name``}. Uses a precise
       regex bounded to the EXACT known needle (``re.escape``); the alt
       text is captured as ``[^\\]]*`` so any user-chosen label is replaced.
    2. **Plain link target** ``](rel_path)`` → ``](display_name)`` so a
       non-image markdown link like ``[view](rel_path)`` survives but
       points at the friendly name.
    3. **Bare literal** ``rel_path`` → ``display_name`` last-resort substring
       replacement (after image+link rewrites have consumed the structured
       cases).

    Pitfall 6 closure
    -----------------
    Every regex / string.replace is scoped to the EXACT path or filename of
    a known attachment. User-authored markdown like
    ``![cat](https://example.com/cat.jpg)`` is left untouched because the
    URL ``https://example.com/cat.jpg`` is not in any attachment's
    ``path`` or ``original_name``.

    The ``memory_id`` argument is currently informational (for future
    namespacing); kept in the signature so callers don't need to refactor
    if scoping rules tighten.
    """
    import re

    out = body
    for att in attachments or []:
        original = att.get("original_name") or ""
        rel_path = att.get("path") or ""
        display = att.get("display_name", original) or original
        if not original or not rel_path:
            continue
        # 1: image syntax — bounded regex per known needle (Pitfall 6 safe).
        for needle in (rel_path, original):
            pattern = r"!\[[^\]]*\]\(" + re.escape(needle) + r"\)"
            out = re.sub(pattern, f"![[{display}]]", out)
        # 2: plain (non-image) link target rewrite — preserves link label.
        # Run AFTER image rewrite so ![alt](needle) is already gone.
        for needle in (rel_path, original):
            if needle != display:
                out = out.replace(f"]({needle})", f"]({display})")
        # 3: bare literal path reference (after structured cases consumed).
        if rel_path != display:
            out = out.replace(rel_path, display)
    return out


class ObsidianExportService:
    """Singleton background loop. Mirrors ``EmbeddingRetryLoop`` /
    ``IntelligenceService``.

    Lifecycle::

        # main.py lifespan
        await obsidian_export.start()        # schedules _run_loop as Task
        ...
        await obsidian_export.stop()         # cancels + awaits Task

    Tests bypass the loop entirely::

        svc = ObsidianExportService(interval=99999)  # never auto-fires
        await svc.trigger_cycle()                    # direct call
    """

    def __init__(self, interval: Optional[int] = None):
        self._interval = interval or settings.obsidian_export_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # OBS-01: layout MUST exist by the first ``.start()`` at the latest.
        # Idempotent — if Plan 07-02 tests create the layout beforehand,
        # this is a no-op.
        _ensure_vault_layout(_vault_root())
        self._task = asyncio.create_task(self._run_loop())
        logger.info("ObsidianExport started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ObsidianExport stopped")

    async def _run_loop(self) -> None:
        # Grace period — lifespan is still hooking into Qdrant + DB
        # (mirrors intelligence.py:100 / embedding_retry.py:144 — same 20s
        # window).
        await asyncio.sleep(20)
        while self._running:
            try:
                if not settings.obsidian_export_enabled:
                    logger.debug("obsidian_export disabled via kill-switch — skipping cycle")
                elif await self._acquire_lock():
                    await self.trigger_cycle()
                else:
                    # Multi-worker dedup — mirrors intelligence.py:117 WARN
                    # pattern so lock contention is visible at the default
                    # log level.
                    logger.warning("obsidian_export: lock contention, skipping cycle")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("ObsidianExport tick error: %s", e)
            await asyncio.sleep(self._interval)

    async def _acquire_lock(self) -> bool:
        """Redis lock so only one worker exports per cycle.

        Mirrors ``intelligence.py:125-134`` — fail-safe to ``True`` if Redis
        is unreachable (better to write twice once than block entirely).
        """
        try:
            redis = await get_redis()
            acquired = await redis.set(
                RedisKeys.obsidian_export_lock(), "1", nx=True, ex=self._interval
            )
            return bool(acquired)
        except Exception:
            return True

    async def trigger_cycle(self) -> None:
        """One-shot export — testable directly.

        Plan 07-02 pipeline:

        1. Walk all ``BoardMemory`` rows (descending ``updated_at``).
        2. Resolve agent_slug + project_slug for routing.
        3. Render frontmatter + body.
        4. Atomic-write via ``_write_if_changed`` (SHA-256 short-circuit).
        5. Tally written / skipped per cycle.

        Per-row exceptions are logged (with ``row.id``) and skipped so a
        single corrupt entry doesn't abort the whole cycle. Phase 7
        attachments are NOT in scope here — Plan 07-03 wires the
        ``_rewrite_wikilinks`` pass and the file mirror.
        """
        vault_root = _vault_root()
        _ensure_vault_layout(vault_root)
        written = 0
        skipped = 0
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.exec(
                select(BoardMemory).order_by(BoardMemory.updated_at.desc())
            )
            rows = result.all()
            for row in rows:
                try:
                    agent_slug = await _resolve_agent_slug(row, session)
                    project_slug = await _resolve_project_slug(row, session)
                    target = _vault_memory_path(
                        row,
                        agent_slug=agent_slug,
                        project_slug=project_slug,
                    )

                    # Phase 7 OBS-03: mirror attachments BEFORE rendering
                    # body, so _rewrite_wikilinks can target the
                    # collision-safe display_name.
                    safe_attachments = _resolve_collision_safe_attachments(
                        row.attachments
                    )
                    real_root = os.path.realpath(_attachments_root())
                    for att in safe_attachments:
                        try:
                            src_rel = att.get("path") or ""
                            display = att.get("display_name") or att.get(
                                "original_name", ""
                            )
                            if not src_rel or not display:
                                continue
                            src_abs = os.path.join(_attachments_root(), src_rel)
                            # T-7-03-01: source path-traversal guard
                            real_src = os.path.realpath(src_abs)
                            if not real_src.startswith(real_root + os.sep):
                                logger.warning(
                                    "ObsidianExport attachment path escapes "
                                    "attachments root: %s",
                                    src_rel,
                                )
                                continue
                            # T-7-03-02: vault target uses _safe_join via
                            # _vault_attachment_path
                            dest_abs = _vault_attachment_path(
                                row, display, category="tasks"
                            )
                            _mirror_attachment(src_abs, dest_abs)
                        except Exception as e:
                            logger.error(
                                "ObsidianExport attachment mirror failed for "
                                "row %s att=%s: %s",
                                row.id,
                                att.get("path"),
                                e,
                            )

                    frontmatter = _render_frontmatter(row, agent_slug, project_slug)
                    body = _rewrite_wikilinks(
                        _render_body(row), safe_attachments, str(row.id)
                    )
                    full_content = frontmatter + "\n" + body
                    if _write_if_changed(target, full_content):
                        written += 1
                    else:
                        skipped += 1
                except Exception as e:
                    logger.error("ObsidianExport row %s render failed: %s", row.id, e)
        logger.info("ObsidianExport cycle: wrote %d, skipped %d", written, skipped)


# Module-level singleton — analogous to intelligence.py:720 / embedding_retry.py:293.
# Lifespan in main.py calls .start() / .stop() — no auto-start here (Pitfall 4).
obsidian_export = ObsidianExportService()
