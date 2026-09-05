"""Rezepte aus Sicht einer Box — die Schnittstelle des Rezept-Umschalters
(Vertrag P0+P1, 02.09.2026).

- ``GET  /api/v1/hosts/{host_id}/recipes``               — alle freigegebenen
  Rezepte mit ``fit`` / ``startable`` / ``reason`` / ``running`` / Belegung,
  fertig gerechnet; das Frontend rechnet nichts nach.
- ``POST /api/v1/hosts/{host_id}/recipes/{slug}/start``  — Solo-Start über den
  bestehenden Lebenszyklus (Instanz anlegen falls nötig, dann
  ``runtime_manager.start_runtime``). Duo: 409 mit Satz.

Eigener Router statt Anbau an ``routers/hosts.py``: der Hosts-Router ist CRUD
plus Box-Wizard; das hier ist die Rezept-Seite und lebt mit
``services/recipe_switcher`` zusammen. Gleicher Prefix, FastAPI verträgt das.

Schreibzugriff ist admin-only — dieselbe Begründung wie bei den Host-
Schreibpfaden: der Start entscheidet, welche Box einen Befehl per SSH bekommt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.routers.hosts import _get_host
from app.services import recipe_switcher

router = APIRouter(prefix="/api/v1/hosts", tags=["hosts"])


async def _host_or_404(session: AsyncSession, host_id: str) -> Host:
    host = await _get_host(session, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host '{host_id}' nicht gefunden")
    return host


@router.get("/{host_id}/recipes")
async def list_recipes_for_host(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Reihenfolge: laufend zuerst, dann startbar, dann grau (mit Satz)."""
    host = await _host_or_404(session, host_id)
    return await recipe_switcher.list_host_recipes(session, host)


class StartRecipeBody(BaseModel):
    """P3: die gewünschte zweite Box. Optional und nur für Zweibox-Rezepte —
    ohne Angabe nimmt das Backend den ersten freien Kandidaten (Worker-Rolle
    zuerst), damit ein Klick ohne Dialog weiter funktioniert."""

    worker_host_id: str | None = None


@router.post("/{host_id}/recipes/{slug}/start")
async def start_recipe_for_host(
    host_id: str,
    slug: str,
    body: StartRecipeBody | None = None,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Instanz anlegen falls sie fehlt, dann starten — Solo wie Verbund (P3).

    Der Body ist optional: ein Start ohne Rumpf verhält sich wie bisher.
    """
    host = await _host_or_404(session, host_id)
    recipe = (await session.exec(select(LocalRecipe).where(LocalRecipe.slug == slug))).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Rezept '{slug}' nicht gefunden")
    try:
        return await recipe_switcher.start_recipe_on_host(
            session, host, recipe, worker_host_id=(body.worker_host_id if body else None)
        )
    except recipe_switcher.RecipeStartError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
