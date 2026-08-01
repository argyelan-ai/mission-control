"""Storage core for reference files (ADR-053) — shared by every ingest path.

The HTTP upload (routers/references.py) and the Slack file ingest
(services/slack_file_ingest.py) accept the same kind of file and must apply
the same rules: MIME allowlist, size cap, path-traversal guard, sha16-prefixed
filename, row + file-index upsert. This module is that ONE implementation;
the callers keep only what is genuinely theirs (HTTP status codes vs. Slack
replies).

Ownership: exactly one of task/project/agent. Task/project references reach
the agent via the dispatch directive; agent-bound references (a file dropped
top-level in the team chat) reach him as a thread message carrying the
absolute path — same 1:1 ~/.mc mount either way.
"""
from __future__ import annotations

import hashlib
import logging
import os

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.reference_file import ReferenceFile
from app.services.fs_roots import mc_home

logger = logging.getLogger("mc.reference_ingest")

# KEIN text/html und KEIN image/svg+xml: der browsable Files-Root served
# Inhalte inline mit Endungs-MIME — aktive Inhalte wären Stored XSS im
# App-Origin (Review-Fund M1).
ALLOWED_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/pdf", "text/plain", "text/markdown", "text/csv",
    "application/json", "application/zip",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",   # xlsx
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
}
MAX_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_FILES_PER_ENTITY = 20


class ReferenceIngestError(Exception):
    """One human-readable reason why a file was not taken. The message is
    written for the operator (HTTP detail / Slack reply), not for the log."""


def references_root() -> str:
    return str(mc_home() / "references")


def serialize_reference(ref: ReferenceFile) -> dict:
    return {
        **ref.model_dump(),
        # Absoluter Pfad, wie ihn Agenten (gleicher ~/.mc-Mount) lesen können.
        "abs_path": os.path.join(references_root(), ref.rel_path),
    }


async def count_references(
    session: AsyncSession,
    *,
    task_id=None,
    project_id=None,
    agent_id=None,
) -> int:
    if task_id is not None:
        cond = ReferenceFile.task_id == task_id
    elif project_id is not None:
        cond = ReferenceFile.project_id == project_id
    else:
        cond = ReferenceFile.agent_id == agent_id
    return len((await session.exec(select(ReferenceFile).where(cond))).all())


async def store_reference(
    session: AsyncSession,
    *,
    contents: bytes,
    filename: str,
    mime: str | None,
    board_id=None,
    task_id=None,
    project_id=None,
    agent_id=None,
    note: str | None = None,
    uploaded_by: str = "user",
    max_bytes: int = MAX_BYTES,
) -> ReferenceFile:
    """Validate + write one reference file, commit its row, index it.

    Raises ``ReferenceIngestError`` with an operator-readable reason; never
    leaves a file on disk without its row (the write happens last before the
    commit). Exactly one of task_id/project_id/agent_id must be set — the
    caller resolved ownership, this function only refuses nonsense.
    """
    owners = [x for x in (task_id, project_id, agent_id) if x is not None]
    if len(owners) != 1:
        raise ReferenceIngestError(
            "Genau eines von task/project/agent muss gesetzt sein."
        )

    if mime not in ALLOWED_MIMES:
        raise ReferenceIngestError(
            f"Dateityp {mime or 'unbekannt'} wird nicht angenommen. Erlaubt: "
            "PNG/JPEG/WebP/GIF, PDF, TXT/MD/CSV/JSON, ZIP, XLSX, DOCX."
        )
    if len(contents) > max_bytes:
        raise ReferenceIngestError(
            f"Datei ist {len(contents) // (1024 * 1024)} MB — Referenzen sind "
            f"auf {max_bytes // (1024 * 1024)} MB begrenzt."
        )

    count = await count_references(
        session, task_id=task_id, project_id=project_id, agent_id=agent_id
    )
    if count >= MAX_FILES_PER_ENTITY:
        raise ReferenceIngestError(
            f"Limit erreicht: max {MAX_FILES_PER_ENTITY} Referenzen pro Ziel."
        )

    # Traversal-Guard auf dem ROHEN Namen, vor basename (memory.py Pitfall 6).
    raw_name = filename or "file"
    if ".." in raw_name or "/" in raw_name or "\\" in raw_name:
        raise ReferenceIngestError(f"Ungültiger Dateiname: {raw_name!r}")
    safe_orig = os.path.basename(raw_name)

    if task_id is not None:
        kind, entity_id = "task", task_id
    elif project_id is not None:
        kind, entity_id = "project", project_id
    else:
        kind, entity_id = "agent", agent_id

    rel_dir = os.path.join(kind, str(entity_id))
    file_dir = os.path.join(references_root(), rel_dir)
    os.makedirs(file_dir, exist_ok=True)

    sha = hashlib.sha256(contents).hexdigest()[:16]
    fname = f"{sha}-{safe_orig}"
    target = os.path.join(file_dir, fname)
    real_dir = os.path.realpath(file_dir)
    real_target = os.path.realpath(target)
    if not real_target.startswith(real_dir + os.sep):
        raise ReferenceIngestError("Pfad verlässt den References-Root.")

    with open(target, "wb") as f:
        f.write(contents)

    ref = ReferenceFile(
        board_id=board_id,
        task_id=task_id,
        project_id=project_id,
        agent_id=agent_id,
        rel_path=os.path.join(rel_dir, fname),
        original_name=safe_orig,
        mime=mime,
        size=len(contents),
        note=(note or "").strip() or None,
        uploaded_by=uploaded_by,
    )
    session.add(ref)
    await session.commit()
    await session.refresh(ref)

    # Best-effort: sofort in den Files-Index (statt auf den Walker zu warten).
    try:
        from app.services.file_indexer import _upsert
        await _upsert(
            session, "references", ref.rel_path,
            name=fname, is_directory=False, size=ref.size, mime=ref.mime,
            mtime=os.path.getmtime(target), task_id=task_id,
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.debug("Referenz-Index-Upsert übersprungen", exc_info=True)

    return ref
