"""Reference files API (ADR-053) — Beispiel-/Asset-Uploads für Tasks & Projekte.

Ablage: ~/.mc/references/{task|project}/{id}/{sha16}-{name} (Files-Root
"references", browsable, NICHT im Files-Browser löschbar — Delete läuft nur
hier, Row + Datei zusammen). Agenten lesen die Dateien direkt über den
1:1-gemounteten ~/.mc-Pfad; die Dispatch-Directive listet sie auf.

Upload-Muster nach routers/memory.upload_attachment (Path-Traversal-Guard
auf dem ROHEN Multipart-Namen, MIME-Allowlist, Grössen-/Anzahl-Caps).
"""

import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.board import Project
from app.models.reference_file import ReferenceFile
from app.models.task import Task

# Der Storage-Kern (Allowlist, Caps, Traversal-Guard, Row + Index) ist mit dem
# Slack-Datei-Ingest geteilt — services/reference_ingest.py ist die eine
# Implementierung, hier bleibt nur HTTP. Die Re-Exports halten bestehende
# Importe (Tests, slack_inbound-Doku) am Leben.
from app.services.reference_ingest import (  # noqa: F401  (re-export)
    ALLOWED_MIMES,
    MAX_BYTES,
    MAX_FILES_PER_ENTITY,
    ReferenceIngestError,
    references_root as _references_root,
    serialize_reference as _serialize,
    store_reference,
)

logger = logging.getLogger("mc.references")

router = APIRouter(prefix="/api/v1/references", tags=["references"])


async def _resolve_target(
    session: AsyncSession, task_id: uuid.UUID | None, project_id: uuid.UUID | None,
) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Validiert task_id XOR project_id → (board_id, kind, entity_id)."""
    if bool(task_id) == bool(project_id):
        raise HTTPException(400, "Genau eines von task_id/project_id angeben")
    if task_id:
        task = await session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return task.board_id, "task", task_id
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project.board_id, "project", project_id


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_reference(
    file: UploadFile = File(...),
    task_id: uuid.UUID | None = Form(default=None),
    project_id: uuid.UUID | None = Form(default=None),
    note: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    board_id, kind, entity_id = await _resolve_target(session, task_id, project_id)

    # MIME zuerst und als 415 (der Storage-Kern würde denselben Fehler als
    # generische Reason liefern — der HTTP-Kontrakt hier ist älter und bleibt).
    if file.content_type not in ALLOWED_MIMES:
        raise HTTPException(415, f"MIME {file.content_type} not allowed")

    contents = await file.read()
    if len(contents) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 25 MB)")

    try:
        ref = await store_reference(
            session,
            contents=contents,
            filename=file.filename or "file",
            mime=file.content_type,
            board_id=board_id,
            task_id=task_id,
            project_id=project_id,
            note=note,
        )
    except ReferenceIngestError as exc:
        raise HTTPException(400, str(exc))

    return _serialize(ref)


@router.get("")
async def list_references(
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    include_project: bool = True,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Referenzen eines Tasks (optional inkl. der geerbten Projekt-Referenzen)
    oder eines Projekts."""
    if bool(task_id) == bool(project_id):
        raise HTTPException(400, "Genau eines von task_id/project_id angeben")
    if project_id:
        result = await session.exec(
            select(ReferenceFile).where(ReferenceFile.project_id == project_id)
            .order_by(ReferenceFile.created_at)
        )
        return [_serialize(r) for r in result.all()]

    own = (await session.exec(
        select(ReferenceFile).where(ReferenceFile.task_id == task_id)
        .order_by(ReferenceFile.created_at)
    )).all()
    inherited: list[ReferenceFile] = []
    if include_project:
        task = await session.get(Task, task_id)
        if task and task.project_id:
            inherited = list((await session.exec(
                select(ReferenceFile).where(ReferenceFile.project_id == task.project_id)
                .order_by(ReferenceFile.created_at)
            )).all())
    return [
        {**_serialize(r), "inherited": False} for r in own
    ] + [
        {**_serialize(r), "inherited": True} for r in inherited
    ]


@router.get("/{reference_id}/download")
async def download_reference(
    reference_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    ref = await session.get(ReferenceFile, reference_id)
    if not ref:
        raise HTTPException(404, "Reference not found")
    root = os.path.realpath(_references_root())
    target = os.path.realpath(os.path.join(root, ref.rel_path))
    if not target.startswith(root + os.sep):
        raise HTTPException(400, "Path escapes references root")
    if not os.path.isfile(target):
        raise HTTPException(404, "File missing on disk")
    return FileResponse(
        target,
        media_type=ref.mime or "application/octet-stream",
        filename=ref.original_name,
        content_disposition_type="attachment",
    )


@router.delete("/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reference(
    reference_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Löscht Row + Datei zusammen (einziger Lösch-Pfad für Referenzen)."""
    ref = await session.get(ReferenceFile, reference_id)
    if not ref:
        raise HTTPException(404, "Reference not found")
    from app.services.reference_cleanup import _delete_rows
    await _delete_rows(session, [ref])
    await session.commit()
