"""Single Source of Truth fuer Deliverable-Path-Validation.

Wird genutzt von:
  - backend/app/routers/agent_scoped.py  (agent_create_deliverable)
  - backend/app/routers/tasks.py         (admin POST /boards/.../deliverables)

Backend-interne Schreiber (pdf_generator, visual_verifier) umgehen diese
Validation bewusst (Sidecar-Output -> direkter DB-Insert ohne Router-Call).

Akzeptierte Prefixe (task-scoped):
  /deliverables/<task_id>/            Docker-Worker Agent-Container
  /shared-deliverables/<task_id>/     mc-playwright Sidecar (PDF, Screenshots)
  /shared-mcp/<task_id>/              Microsoft Playwright MCP Sidecar
  ~/.mc/deliverables/<task_id>/       Host-Worker (tilde-Form, z.B. Hermes)
  $HOME_HOST/.mc/deliverables/<task_id>/  Host-Worker (resolved, z.B. /Users/<login>/...)
  http(s)://...                       URL-Deliverables (deliverable_type=url)
"""
from __future__ import annotations

import os
import uuid

from fastapi import HTTPException


def accepted_path_prefixes(task_id: uuid.UUID) -> tuple[str, ...]:
    """Liefert alle gueltigen lokalen Pfad-Prefixe fuer Deliverables einer Task."""
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
    """Validiert path-Feld eines Deliverables. Wirft HTTPException 422 bei Verstoss.

    Akzeptiert:
      - Einen der task-scoped lokalen Prefixe (accepted_path_prefixes)
      - http/https URL (fuer deliverable_type=url)
      - None, wenn content inline gesetzt ist

    Security:
      - NUL-Byte Reject
      - Path-Traversal Reject via os.path.normpath Recheck
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
