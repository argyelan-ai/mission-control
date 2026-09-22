"""Task Run Record ("Laufakte") — curated, human-readable summary of one
task's full history. Read-only view over six existing sources; the Vault
export is the only write path, and it only ever creates/overwrites its own
Markdown file (Bauplan 6.1 / 6.5 — nothing else is touched).

Contract (Nachtrag team-lead, siehe tests/test_task_run_record.py):
  GET  /api/v1/tasks/{task_id}/run-record          -> JSON      (viewer+)
  GET  /api/v1/tasks/{task_id}/run-record.md        -> Markdown  (viewer+)
  POST /api/v1/tasks/{task_id}/run-record/to-vault   -> Vault-Export (operator+)

Roles follow app/routers/workflows.py:93/:99 — require_role(Role.VIEWER)
for reads, require_role(Role.OPERATOR) for the export.
"""

import uuid
from datetime import datetime, timezone

import frontmatter
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.config import settings
from app.database import get_session
from app.models.task import Task
from app.services.run_record import build_run_record, render_run_record_markdown

router = APIRouter(prefix="/api/v1", tags=["run-record"])


async def _load_task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/tasks/{task_id}/run-record", dependencies=[Depends(require_role(Role.VIEWER))])
async def get_run_record(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _load_task(session, task_id)
    return await build_run_record(session, task_id)


@router.get(
    "/tasks/{task_id}/run-record.md", dependencies=[Depends(require_role(Role.VIEWER))]
)
async def get_run_record_markdown(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _load_task(session, task_id)
    record = await build_run_record(session, task_id)
    markdown = render_run_record_markdown(record)
    return PlainTextResponse(content=markdown, media_type="text/markdown")


@router.post(
    "/tasks/{task_id}/run-record/to-vault",
    dependencies=[Depends(require_role(Role.OPERATOR))],
)
async def export_run_record_to_vault(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    task = await _load_task(session, task_id)
    record = await build_run_record(session, task_id)
    markdown = render_run_record_markdown(record)

    # Live-Befund (nach Deploy main 855eab44): a bare Markdown file with no
    # YAML frontmatter was quarantined by the real VaultWatcher on write —
    # "missing required field: id" (app/helpers/vault_frontmatter.py
    # REQUIRED_FIELDS). Every field the watcher checks must be present:
    # id/type/agent/date. `agent="system"` because `runs/` has no
    # `agents/<slug>/` folder to own it (path-ownership check only applies
    # under agents/ — vault_watcher.py:_validate_path_ownership).
    post = frontmatter.Post(
        markdown,
        id=f"run-{task.id}",
        type="run-record",
        agent="system",
        date=datetime.now(timezone.utc).isoformat(),
        title=f"Laufakte: {task.title}",
        task=str(task.id),
    )

    runs_dir = settings.vault_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{task.id}-laufakte.md"
    target = runs_dir / filename
    target.write_text(frontmatter.dumps(post), encoding="utf-8")  # overwrites, nothing else touched

    return {"path": f"runs/{filename}"}
