"""Vault-as-Brain Phase A — Deliverable → Markdown wrapper sync.

For every TaskDeliverable (the agent-produced artifacts in `task_deliverables`)
we maintain a Markdown wrapper note in the vault so that:

- The operator + agents find the deliverable via the same FTS5/Qdrant search that
  already powers vault notes.
- Agents read the asset natively via `Read /vault/attachments/...` —
  Claude-Code's Read tool handles PDFs (pages-Parameter) and images
  (Vision-Input) transparently.
- The graph view edges them into the wikilink topology via `[[task-{id}]]`
  back-references.

Three deliverable kinds map to three wrapper shapes:

- **screenshot / file** — binary; hardlinked into `~/.mc/vault/attachments/`
  with copy-fallback for cross-FS. Wrapper body embeds it via Obsidian-style
  `![[…]]` syntax.
- **document** — markdown content already in the DB's `content` column;
  wrapper carries the markdown inline (no separate binary file).
- **url** — external link; no binary; wrapper carries the URL in frontmatter
  and a clickable link in the body.

The wrapper is the artifact, not a cache. If the operator deletes it manually it
stays deleted — there's no reconcile loop. The forward-hooks on the three
POST endpoints (added in routers/agent_scoped.py + tasks.py) handle the
"new deliverable → new wrapper" case; the one-shot backfill CLI handles
the initial seed of the 327 existing deliverables.

Idempotent: re-running `sync_deliverable_to_vault` on the same deliverable
is a no-op if the wrapper already exists and the underlying asset path
hasn't changed.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import frontmatter as fm_lib
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.deliverable import TaskDeliverable
from app.services.deliverable_fs_resolver import resolve_deliverable_fs_path
from app.utils import slugify

logger = logging.getLogger("mc.vault_wrapper")


# Mapping deliverable_type → on-disk subfolder under attachments/.
# Anything not in the map (`url`, `document`) skips the file copy entirely.
_ATTACHMENT_KIND_BY_TYPE: dict[str, str] = {
    "screenshot": "images",
    "file": "files",
    "artifact": "files",
}


@dataclass
class SyncResult:
    deliverable_id: UUID
    wrapper_path: Path | None
    attachment_path: Path | None
    skipped: bool
    reason: str | None
    error: str | None


def wrapper_relpath(deliverable: TaskDeliverable, agent_slug: str) -> str:
    """Deterministic wrapper path inside the vault.

    Layout: ``agents/{agent_slug}/deliverables/{title-slug}-{deliverable_id}.md``

    The deliverable_id suffix prevents collisions when two deliverables share
    the same title (rare but seen — same task re-run produces a new
    deliverable with the same name).
    """
    title_slug = slugify(deliverable.title) or "untitled"
    return f"agents/{agent_slug}/deliverables/{title_slug}-{deliverable.id}.md"


def attachment_relpath(deliverable: TaskDeliverable, src_path: Path) -> str | None:
    """Deterministic attachment path inside the vault, or None if no asset."""
    kind = _ATTACHMENT_KIND_BY_TYPE.get(deliverable.deliverable_type)
    if not kind:
        return None
    # Preserve the original extension so PDFs stay PDFs in Claude-Code's eyes.
    suffix = src_path.suffix or ""
    return f"attachments/{kind}/{deliverable.id}{suffix}"


def hardlink_or_copy(src: Path, dst: Path) -> str:
    """Hardlink src→dst; fall back to copy if the link crosses filesystems.

    Returns "hardlink" or "copy" so the caller can log which path was taken.
    Idempotent: if dst already exists with the same size we treat that as
    "already in place" and return "skip".
    """
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return "skip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


async def _agent_slug_for(deliverable: TaskDeliverable, session: AsyncSession) -> str:
    """Wrapper owner slug. Falls back to 'system' when agent_id is NULL
    (admin-created via MCP, or Hermes host-runtime worker)."""
    if deliverable.agent_id is None:
        return "system"
    from app.models.agent import Agent
    agent = await session.get(Agent, deliverable.agent_id)
    if not agent:
        return "system"
    return slugify(agent.name) or "system"


def _build_wrapper_post(
    deliverable: TaskDeliverable,
    *,
    agent_slug: str,
    attachment_rel: str | None,
    attachment_size: int | None,
    attachment_mime: str | None,
) -> fm_lib.Post:
    """Assemble the frontmatter + body of the wrapper note."""
    metadata: dict[str, Any] = {
        "id": f"deliverable-{deliverable.id}",
        "title": deliverable.title,
        "agent": agent_slug,
        "type": "deliverable",
        "deliverable_kind": deliverable.deliverable_type,
        "deliverable_id": str(deliverable.id),
        "date": (deliverable.created_at or datetime.now(timezone.utc)).isoformat(),
        "source_task": str(deliverable.task_id),
        "task": str(deliverable.task_id),  # Phase E task-klammer
        "tags": (deliverable.tags or []) + ["deliverable"],
        "related": [],
    }
    if attachment_rel:
        metadata["attachment_path"] = attachment_rel
        if attachment_size is not None:
            metadata["attachment_size"] = attachment_size
        if attachment_mime:
            metadata["attachment_mime"] = attachment_mime
    if deliverable.deliverable_type == "url" and deliverable.path:
        metadata["source_url"] = deliverable.path

    # Body. Order: H1 title (semantic-search anchor) → embed/link → description →
    # inline content (for `document` kind) → empty Auto-extracted slot for B.1.
    lines: list[str] = [f"# {deliverable.title}", ""]
    lines.append(
        f"> Auto-importiert aus Task [[task-{deliverable.task_id}]]."
    )
    if attachment_rel and deliverable.deliverable_type in {"screenshot", "file", "artifact"}:
        # Obsidian-style embed. The relpath is wrapper→attachment.
        lines.append("")
        lines.append(f"![[{attachment_rel}]]")
    elif deliverable.deliverable_type == "url" and deliverable.path:
        lines.append("")
        lines.append(f"Quelle: <{deliverable.path}>")

    if deliverable.description:
        lines += ["", "## Description", "", deliverable.description.strip()]

    # Inline markdown content (e.g. researcher reports stored directly in DB).
    if deliverable.deliverable_type == "document" and deliverable.content:
        lines += ["", "## Content", "", deliverable.content.strip()]

    # Phase B.1 will patch this section. We leave the heading + placeholder
    # so the file-watcher → embedding re-index already has a stable anchor.
    if deliverable.deliverable_type == "file" and (attachment_mime or "").endswith("pdf"):
        lines += ["", "## Auto-extracted", "", "*(extraktion läuft...)*"]

    return fm_lib.Post("\n".join(lines), **metadata)


def _atomic_write_post(target: Path, post: fm_lib.Post) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(fm_lib.dumps(post))
        tmp.replace(target)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


async def sync_deliverable_to_vault(
    deliverable: TaskDeliverable,
    session: AsyncSession,
    *,
    force: bool = False,
) -> SyncResult:
    """Idempotent sync: deliverable → vault wrapper (+ hardlinked attachment).

    Steps:
      1. Compute wrapper path. If wrapper exists and `force=False` → skip.
      2. For binary kinds, resolve the source file inside the container,
         hardlink (or copy) it into `attachments/{kind}/{id}.{ext}`.
      3. Build the frontmatter + body, atomically write the wrapper.

    Returns a structured SyncResult so the caller can log per-deliverable
    outcomes without exception-catching gymnastics.
    """
    vault_root = Path(settings.vault_path)
    agent_slug = await _agent_slug_for(deliverable, session)
    wrapper_rel = wrapper_relpath(deliverable, agent_slug)
    wrapper_abs = vault_root / wrapper_rel

    if wrapper_abs.exists() and not force:
        return SyncResult(
            deliverable_id=deliverable.id,
            wrapper_path=wrapper_abs,
            attachment_path=None,
            skipped=True,
            reason="already-exists",
            error=None,
        )

    attachment_abs: Path | None = None
    attachment_rel: str | None = None
    attachment_size: int | None = None
    attachment_mime: str | None = None

    if deliverable.deliverable_type in _ATTACHMENT_KIND_BY_TYPE:
        # Resolve to container-side path that the backend can open().
        src_str = await resolve_deliverable_fs_path(deliverable, session, target="container")
        if not src_str:
            return SyncResult(
                deliverable_id=deliverable.id,
                wrapper_path=None,
                attachment_path=None,
                skipped=True,
                reason="source-unresolvable",
                error=None,
            )
        src_path = Path(src_str)
        if not src_path.exists():
            return SyncResult(
                deliverable_id=deliverable.id,
                wrapper_path=None,
                attachment_path=None,
                skipped=True,
                reason=f"source-missing:{src_str}",
                error=None,
            )

        attachment_rel = attachment_relpath(deliverable, src_path)
        if attachment_rel is None:
            # _ATTACHMENT_KIND_BY_TYPE said yes but attachment_relpath said
            # no — defensive guard, shouldn't happen.
            return SyncResult(
                deliverable_id=deliverable.id,
                wrapper_path=None,
                attachment_path=None,
                skipped=True,
                reason="attachment-relpath-empty",
                error=None,
            )
        attachment_abs = vault_root / attachment_rel
        try:
            mode = hardlink_or_copy(src_path, attachment_abs)
            logger.debug(
                "Wrapper attach: %s → %s (%s)", src_path, attachment_abs, mode
            )
        except OSError as exc:
            return SyncResult(
                deliverable_id=deliverable.id,
                wrapper_path=None,
                attachment_path=None,
                skipped=False,
                reason=None,
                error=f"hardlink-failed: {exc}",
            )
        attachment_size = attachment_abs.stat().st_size
        guessed, _ = mimetypes.guess_type(str(attachment_abs))
        attachment_mime = guessed

    # Relative path from the wrapper file to the attachment, so Obsidian's
    # `![[…]]` resolves correctly. Both live under the same vault root, so
    # this is just "go up to vault, then descend to attachments/…".
    embed_rel: str | None = None
    if attachment_rel:
        depth = wrapper_rel.count("/")
        embed_rel = "../" * depth + attachment_rel

    post = _build_wrapper_post(
        deliverable,
        agent_slug=agent_slug,
        attachment_rel=embed_rel,
        attachment_size=attachment_size,
        attachment_mime=attachment_mime,
    )
    try:
        _atomic_write_post(wrapper_abs, post)
    except OSError as exc:
        return SyncResult(
            deliverable_id=deliverable.id,
            wrapper_path=None,
            attachment_path=attachment_abs,
            skipped=False,
            reason=None,
            error=f"wrapper-write-failed: {exc}",
        )

    logger.info(
        "Vault wrapper synced: %s (kind=%s, attach=%s)",
        wrapper_rel,
        deliverable.deliverable_type,
        attachment_rel or "—",
    )

    # Phase B.1: if the asset is a PDF, run text extraction inline. Cheap
    # (pdfplumber, local), deterministic, and means the wrapper is fully
    # searchable on the next file-watcher cycle (which triggers re-embed).
    # Sync execution: both call sites (FastAPI background-task hook AND the
    # backfill CLI) tolerate a few seconds per deliverable; the 78 existing
    # PDFs all extracted in <1s each in the smoke test.
    if (
        attachment_abs is not None
        and (attachment_mime or "").endswith("pdf")
    ):
        try:
            from app.services.deliverable_extractor import extract_and_patch
            text = extract_and_patch(wrapper_abs, attachment_abs)
            if text:
                logger.info(
                    "PDF text extracted: %d chars → %s",
                    len(text),
                    wrapper_rel,
                )
        except Exception as exc:
            # Extraction is best-effort; the wrapper stays valid (with the
            # placeholder) and the operator can still find it by title + description.
            logger.warning("PDF extract failed for %s: %s", wrapper_rel, exc)

    return SyncResult(
        deliverable_id=deliverable.id,
        wrapper_path=wrapper_abs,
        attachment_path=attachment_abs,
        skipped=False,
        reason=None,
        error=None,
    )


async def sync_deliverable_id(
    deliverable_id: UUID,
    session: AsyncSession,
    *,
    force: bool = False,
) -> SyncResult:
    """Convenience wrapper that loads the deliverable inside the call.

    Used by the FastAPI BackgroundTasks hooks where the only thing the
    handler has at hand after `session.commit()` is the deliverable's UUID.
    """
    deliverable = await session.get(TaskDeliverable, deliverable_id)
    if not deliverable:
        return SyncResult(
            deliverable_id=deliverable_id,
            wrapper_path=None,
            attachment_path=None,
            skipped=True,
            reason="deliverable-not-found",
            error=None,
        )
    return await sync_deliverable_to_vault(deliverable, session, force=force)
