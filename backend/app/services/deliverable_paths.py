"""Single source of truth for deliverable path validation.

Used by:
  - backend/app/routers/agent_scoped.py  (agent_create_deliverable)
  - backend/app/routers/tasks.py         (admin POST /boards/.../deliverables)

Backend-internal writers (pdf_generator, visual_verifier) deliberately bypass
this validation (sidecar output -> direct DB insert without router call).

Accepted prefixes (task-scoped):
  /deliverables/<task_id>/            Docker worker agent container
  /shared-deliverables/<task_id>/     mc-playwright sidecar (PDF, screenshots)
  /shared-mcp/<task_id>/              Microsoft Playwright MCP sidecar
  ~/.mc/deliverables/<task_id>/       Host worker (tilde form, e.g. Hermes)
  $HOME_HOST/.mc/deliverables/<task_id>/  Host worker (resolved, e.g. /Users/<login>/...)
  http(s)://...                       URL deliverables (deliverable_type=url)
"""
from __future__ import annotations

import os
import uuid

from fastapi import HTTPException


def accepted_path_prefixes(task_id: uuid.UUID) -> tuple[str, ...]:
    """Returns all valid local path prefixes for deliverables of a task."""
    from app.config import settings

    home_host = settings.home_host
    return (
        f"/deliverables/{task_id}/",
        f"/shared-deliverables/{task_id}/",
        f"/shared-mcp/{task_id}/",
        f"~/.mc/deliverables/{task_id}/",
        f"{home_host}/.mc/deliverables/{task_id}/",
    )


def validate_deliverable_path(
    path: str | None,
    content: str | None,
    task_id: uuid.UUID,
) -> None:
    """Validates the path field of a deliverable. Raises HTTPException 422 on violation.

    Accepts:
      - One of the task-scoped local prefixes (accepted_path_prefixes)
      - http/https URL (for deliverable_type=url)
      - None, if content is set inline

    Security:
      - NUL byte reject
      - Path traversal reject via os.path.normpath recheck
    """
    if path is None:
        if not (content and content.strip()):
            raise HTTPException(
                status_code=422,
                detail="Deliverable braucht entweder 'path' (unter einem Task-Prefix) oder 'content' inline.",
            )
        return

    if path.startswith(("http://", "https://")):
        return

    prefixes = accepted_path_prefixes(task_id)
    if not any(path.startswith(p) for p in prefixes):
        raise HTTPException(
            status_code=422,
            detail=(
                "Deliverable-Pfad muss unter einem Task-Prefix liegen:\n"
                + "\n".join(f"  • {p}" for p in prefixes)
                + "\nOder 'content' inline ohne path."
                + " URLs (http/https) sind fuer deliverable_type=url erlaubt."
                + "\nPfade wie /home/agent/, /workspace/ oder ~/FreeCode/ sind"
                + " nicht auf den Host gemountet und damit fuer den Operator unsichtbar."
            ),
        )

    if "\x00" in path:
        raise HTTPException(status_code=422, detail="Deliverable-Pfad: NUL-Byte verboten")

    normalized = os.path.normpath(path)
    if not any(normalized.startswith(p) for p in prefixes):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Deliverable-Pfad escaped die Task-Zone nach Normalisierung "
                f"(normalized={normalized}). `..` Sequenzen nicht erlaubt."
            ),
        )
