"""Slack file ingest — the operator drops a file in the chat, MC takes it.

The receiving end of the operator→agent file path (Konzept §3D): a non-audio
``file_share`` in a served channel becomes a reference file (ADR-053) instead
of the old "kann ich hier noch nicht annehmen" reply. Ownership follows the
message's routing, which the caller (``slack_inbound``) has already decided:

  * a reply in a mapped task thread   → the file belongs to that TASK,
  * a thread with a project           → to that PROJECT,
  * the general chat / an ``@agent``  → to that AGENT (usually Boss) —
                                        the new ``agent_id`` owner (0172).

This module never routes and never replies; it downloads (streamed, capped —
``slack_files``), validates and stores (shared core — ``reference_ingest``)
and reports what happened in operator-readable lines. The channel gate ran
before any of this — bytes are only ever fetched for channels MC serves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.reference_file import ReferenceFile
from app.models.thread import Thread

logger = logging.getLogger("mc.slack_file_ingest")


def is_audio_file(file: dict) -> bool:
    """The voice path's definition of audio, shared so the two branches can
    never both claim (or both refuse) the same file."""
    if file.get("subtype") == "slack_audio":
        return True
    return str(file.get("mimetype") or "").startswith("audio/")


def non_audio_files(event: dict) -> list[dict]:
    """Every shared file the VOICE path will not take — the ingest's input."""
    return [
        f for f in (event.get("files") or [])
        if isinstance(f, dict) and not is_audio_file(f)
    ]


@dataclass
class IngestOutcome:
    """What one event's file batch became — the caller words the replies.

    ``stored`` rows carry their absolute path via ``abs_path(ref)``;
    ``rejected`` holds operator-readable one-liners ("name: warum nicht").
    ``owner_label`` names where the stored files went ("Task »…«", "Boss").
    """

    stored: list[ReferenceFile] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    owner_label: str = ""


async def _resolve_owner(
    session: AsyncSession, thread: Thread
) -> tuple[dict, str] | None:
    """Thread → storage ownership (exactly one of task/project/agent) + label.

    None means the thread has no owner a file could belong to — the caller
    says so instead of guessing (same honesty rule as the routing itself).
    """
    if thread.task_id is not None:
        from app.models.task import Task

        task = await session.get(Task, thread.task_id)
        if task is not None:
            label = f"Task »{(task.title or str(task.id)[:8])[:60]}«"
            return {"task_id": task.id, "board_id": task.board_id}, label
    if thread.project_id is not None:
        from app.models.board import Project

        project = await session.get(Project, thread.project_id)
        if project is not None:
            label = f"Projekt »{(project.name or str(project.id)[:8])[:60]}«"
            return {"project_id": project.id, "board_id": project.board_id}, label
    if thread.agent_id is not None:
        from app.models.agent import Agent

        agent = await session.get(Agent, thread.agent_id)
        if agent is not None:
            return (
                {"agent_id": agent.id, "board_id": agent.board_id},
                agent.name or "Agent",
            )
    return None


async def ingest_event_files(
    session: AsyncSession, thread: Thread, files: list[dict]
) -> IngestOutcome:
    """Take every non-audio file of one ``file_share`` event. Never raises.

    Per file: allowlist BEFORE download (no bytes are fetched for a type MC
    refuses anyway), then the streamed, capped download, then the shared
    storage core. One bad file never blocks its siblings.
    """
    from app.config import settings
    from app.services.reference_ingest import (
        ALLOWED_MIMES,
        ReferenceIngestError,
        store_reference,
    )
    from app.services.slack_files import declared_file_size, download_slack_file

    outcome = IngestOutcome()

    owner = await _resolve_owner(session, thread)
    if owner is None:
        outcome.rejected = [
            f"{f.get('name') or 'Datei'}: Thread hat weder Task noch Projekt "
            "noch Agent — Datei nicht übernommen."
            for f in files
        ]
        return outcome
    owner_kwargs, outcome.owner_label = owner

    cap = int(getattr(settings, "slack_file_ingest_max_bytes", 25 * 1024 * 1024))
    for file in files:
        name = file.get("name") or file.get("title") or "datei"
        mime = file.get("mimetype")
        if mime not in ALLOWED_MIMES:
            outcome.rejected.append(
                f"{name}: Dateityp {mime or 'unbekannt'} wird nicht angenommen "
                "(erlaubt: Bilder, PDF, Text/MD/CSV/JSON, ZIP, XLSX, DOCX)."
            )
            continue
        declared = declared_file_size(file)
        if declared is not None and declared > cap:
            outcome.rejected.append(
                f"{name}: {declared // (1024 * 1024)} MB überschreitet das "
                f"Limit von {cap // (1024 * 1024)} MB."
            )
            continue

        contents = await download_slack_file(file, max_bytes=cap)
        if contents is None:
            outcome.rejected.append(
                f"{name}: Download von Slack fehlgeschlagen — Details im "
                "Backend-Log."
            )
            continue

        try:
            ref = await store_reference(
                session,
                contents=contents,
                filename=name,
                mime=mime,
                note="via Slack",
                uploaded_by="slack",
                max_bytes=cap,
                **owner_kwargs,
            )
        except ReferenceIngestError as exc:
            outcome.rejected.append(f"{name}: {exc}")
            continue
        outcome.stored.append(ref)
        logger.info(
            "slack file ingest: %s (%d bytes) -> %s",
            ref.original_name, ref.size, ref.rel_path,
        )

    return outcome
