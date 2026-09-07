"""Agent-scoped thread attachments — POST /api/v1/agent/threads/{id}/attachment.

Der Rückweg für Dateien: ein Agent hängt ein Bild (Mockup, Screenshot) oder
eine Datei an eine Thread-Nachricht — auch in einer Gruppe, die keinen Task
und keinen Chat-Spiegel hat (ADR-075). Live-Befund 07.09.2026: beide Agenten
einer Brainstorm-Gruppe antworteten auf „bitte Screenshots" mit „technisch
nicht möglich", weil `--vault-path` nur den Slack/Telegram-Spiegel bedient
und `mc report --photo` einen Task voraussetzt.

Gleicher Trick wie der Operator-Upload im Sessions-Chat (routers/agent_chat.
post_chat_attachment, #330): die Datei wird als AGENTEN-Referenz abgelegt
(``reference_files.agent_id``) und der absolute Pfad zurückgegeben. `mc msg
--attach` hängt ihn als ``[Anhang: <pfad>]``-Zeile an die Nachricht — das
Format, das der Composer schreibt und das Frontend zur Kachel macht. Kein
zweiter Speicherort, kein zweites Protokoll.

Eigene Datei statt agent_scoped.py: klein, einzeln testbar, und der grosse
Router ist in offenen PRs in Arbeit (Konfliktvermeidung).

Auth:  Agent-Token, Scope CHAT_WRITE — dieselbe Regel wie fürs Posten.
404:   Thread fehlt ODER Agent ist nicht Teil davon (kein Ertasten fremder
       Gespräche, siehe thread_agent_may_write_to).
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.agent import Agent
from app.scopes import Scope, require_scope
from app.services.reference_ingest import (
    ReferenceIngestError,
    ReferenceTooLargeError,
    is_image_reference,
    serialize_reference,
    store_reference,
)
from app.services.thread_scope import thread_agent_may_write_to

logger = logging.getLogger("mc.agent_thread_attachments")

router = APIRouter(prefix="/api/v1/agent", tags=["agent-thread-attachments"])


@router.post("/threads/{thread_id}/attachment", status_code=status.HTTP_201_CREATED)
async def agent_post_thread_attachment(
    thread_id: uuid.UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_scope(Scope.CHAT_WRITE)),
):
    """Datei für eine Thread-Nachricht ablegen; gibt den absoluten Pfad zurück.

    Erst die Berechtigung, dann die Bytes: ein Fremder bekommt 404, bevor
    irgendetwas auf der Platte landet. Keine Typ-Beschränkung und kein
    Datei-Deckel — wie beim Operator-Upload: ob der Leser mit der Datei etwas
    anfangen kann, ist seine Sache; aktive Inhalte liefert der Files-Endpunkt
    ohnehin nur als Download aus.
    """
    thread = await thread_agent_may_write_to(session, agent, thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread nicht gefunden oder nicht deiner.",
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=422, detail="Die Datei ist leer.")

    try:
        ref = await store_reference(
            session,
            contents=contents,
            filename=file.filename or "",
            mime=file.content_type,
            agent_id=agent.id,
            uploaded_by="agent",
            allowed_mimes=None,
            max_files=None,
        )
    except ReferenceTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except ReferenceIngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "Thread-Anhang: %s legt %s (%d B) für Thread %s ab",
        agent.name, ref.original_name, ref.size, thread.id,
    )
    return {
        "path": serialize_reference(ref)["abs_path"],
        "name": ref.original_name,
        "bytes": ref.size,
        "isImage": is_image_reference(ref.original_name),
        "root": "references",
        "subpath": ref.rel_path,
    }
