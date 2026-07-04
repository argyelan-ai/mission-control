"""Referenz-Dateien-Cleanup (ADR-053) — von den Delete-Endpoints gerufen.

Löscht Rows + Dateien zusammen; Fehler blockieren den Entity-Delete nie.
"""

import logging
import os
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.reference_file import ReferenceFile
from app.services.fs_roots import mc_home

logger = logging.getLogger("mc.references")


async def delete_references_for(
    session: AsyncSession,
    *,
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> int:
    if bool(task_id) == bool(project_id):
        return 0
    cond = (
        ReferenceFile.task_id == task_id
        if task_id else ReferenceFile.project_id == project_id
    )
    refs = (await session.exec(select(ReferenceFile).where(cond))).all()
    return await _delete_rows(session, refs)


async def delete_references_for_tasks(
    session: AsyncSession, task_ids: list[uuid.UUID]
) -> int:
    """Bulk-Variante für Projekt-Kaskaden (Tasks werden per SQL gelöscht)."""
    if not task_ids:
        return 0
    refs = (await session.exec(
        select(ReferenceFile).where(ReferenceFile.task_id.in_(task_ids))  # type: ignore[union-attr]
    )).all()
    return await _delete_rows(session, refs)


async def _delete_rows(session: AsyncSession, refs) -> int:
    from app.models.file_index import FileIndexEntry

    root = os.path.realpath(str(mc_home() / "references"))
    for ref in refs:
        target = os.path.realpath(os.path.join(root, ref.rel_path))
        if target.startswith(root + os.sep) and os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                logger.warning("Referenz-Datei nicht löschbar: %s", target)
        # Index-Row miträumen — deren task_id-FK würde sonst den Task-Delete
        # blockieren (Live-Smoke-Fund 04.07.).
        for idx in (await session.exec(
            select(FileIndexEntry).where(
                FileIndexEntry.root_key == "references",
                FileIndexEntry.rel_path == ref.rel_path,
            )
        )).all():
            await session.delete(idx)
        await session.delete(ref)
    return len(refs)
