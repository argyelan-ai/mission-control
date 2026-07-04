"""Reference files API (ADR-053) — Beispiel-/Asset-Uploads für Tasks & Projekte.

Ablage: ~/.mc/references/{task|project}/{id}/{sha16}-{name} (Files-Root
"references", browsable, NICHT im Files-Browser löschbar — Delete läuft nur
hier, Row + Datei zusammen). Agenten lesen die Dateien direkt über den
1:1-gemounteten ~/.mc-Pfad; die Dispatch-Directive listet sie auf.

Upload-Muster nach routers/memory.upload_attachment (Path-Traversal-Guard
auf dem ROHEN Multipart-Namen, MIME-Allowlist, Grössen-/Anzahl-Caps).
"""

import hashlib
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
from app.services.fs_roots import mc_home

logger = logging.getLogger("mc.references")

router = APIRouter(prefix="/api/v1/references", tags=["references"])

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


def _references_root() -> str:
    return str(mc_home() / "references")


def _serialize(ref: ReferenceFile) -> dict:
    return {
        **ref.model_dump(),
        # Absoluter Pfad, wie ihn Agenten (gleicher ~/.mc-Mount) lesen können.
        "abs_path": os.path.join(_references_root(), ref.rel_path),
    }


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

    if file.content_type not in ALLOWED_MIMES:
        raise HTTPException(415, f"MIME {file.content_type} not allowed")

    count = len((await session.exec(
        select(ReferenceFile).where(
            (ReferenceFile.task_id == task_id) if task_id
            else (ReferenceFile.project_id == project_id)
        )
    )).all())
    if count >= MAX_FILES_PER_ENTITY:
        raise HTTPException(400, f"Max {MAX_FILES_PER_ENTITY} Referenzen pro {kind}")

    contents = await file.read()
    if len(contents) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 25 MB)")

    # Traversal-Guard auf dem ROHEN Namen, vor basename (memory.py Pitfall 6).
    raw_name = file.filename or "file"
    if ".." in raw_name or "/" in raw_name or "\\" in raw_name:
        raise HTTPException(400, "Invalid filename")
    safe_orig = os.path.basename(raw_name)

    rel_dir = os.path.join(kind, str(entity_id))
    file_dir = os.path.join(_references_root(), rel_dir)
    os.makedirs(file_dir, exist_ok=True)

    sha = hashlib.sha256(contents).hexdigest()[:16]
    fname = f"{sha}-{safe_orig}"
    target = os.path.join(file_dir, fname)
    real_dir = os.path.realpath(file_dir)
    real_target = os.path.realpath(target)
    if not real_target.startswith(real_dir + os.sep):
        raise HTTPException(400, "Path escapes references root")

    with open(target, "wb") as f:
        f.write(contents)

    ref = ReferenceFile(
        board_id=board_id,
        task_id=task_id,
        project_id=project_id,
        rel_path=os.path.join(rel_dir, fname),
        original_name=safe_orig,
        mime=file.content_type,
        size=len(contents),
        note=(note or "").strip() or None,
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
