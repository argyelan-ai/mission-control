"""Loops API (ADR-051, L1) — ergebnisgesteuerte Task-Schleifen verwalten.

Lifecycle: draft → running → (waiting_gate|paused) → done|failed.
Leitplanke: 1 aktiver Loop pro Board (running/waiting_gate).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.models.approval import Approval
from app.models.board import Board
from app.models.loop import BACKLOG_SOURCES, Loop, LoopRound, TERMINAL_LOOP_STATUSES
from app.services.activity import emit_event
from app.utils import utcnow

router = APIRouter(prefix="/api/v1", tags=["loops"])

ACTIVE_STATUSES = ("running", "waiting_gate")


class LoopCreate(BaseModel):
    board_id: uuid.UUID
    name: str
    goal: str
    project_id: uuid.UUID | None = None
    backlog_source: str = "markdown"
    backlog_md: str | None = None
    round_brief: str | None = None
    human_every_n_rounds: int = 0
    pause_on_failed_rounds: int = 2
    max_rounds: int = 10
    max_duration_minutes: int | None = None
    stop_on_backlog_empty: bool = True


class LoopUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    backlog_md: str | None = None
    round_brief: str | None = None
    human_every_n_rounds: int | None = None
    pause_on_failed_rounds: int | None = None
    max_rounds: int | None = None
    max_duration_minutes: int | None = None
    stop_on_backlog_empty: bool | None = None


async def _active_loop_on_board(
    session: AsyncSession, board_id: uuid.UUID, exclude: uuid.UUID | None = None,
) -> Loop | None:
    query = select(Loop).where(
        Loop.board_id == board_id,
        Loop.status.in_(ACTIVE_STATUSES),  # type: ignore[attr-defined]
    )
    if exclude:
        query = query.where(Loop.id != exclude)
    return (await session.exec(query)).first()


async def _supersede_pending_gates(session: AsyncSession, loop_id: uuid.UUID) -> None:
    """Operator-Aktion via UI ersetzt offene loop_gate-Approvals."""
    pending = (await session.exec(
        select(Approval).where(
            Approval.action_type == "loop_gate",
            Approval.status == "pending",
        )
    )).all()
    for a in pending:
        if (a.payload or {}).get("loop_id") == str(loop_id):
            a.status = "superseded"
            a.resolved_at = utcnow()
            session.add(a)


@router.get("/loops")
async def list_loops(
    board_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    query = select(Loop).order_by(Loop.created_at.desc())  # type: ignore[union-attr]
    if board_id:
        query = query.where(Loop.board_id == board_id)
    return (await session.exec(query)).all()


@router.post("/loops", status_code=status.HTTP_201_CREATED)
async def create_loop(
    payload: LoopCreate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    if payload.backlog_source not in BACKLOG_SOURCES:
        raise HTTPException(status_code=400, detail=f"backlog_source muss eines von {BACKLOG_SOURCES} sein")
    if payload.backlog_source == "markdown" and not (payload.backlog_md or "").strip():
        raise HTTPException(status_code=400, detail="backlog_md ist Pflicht bei backlog_source=markdown")
    if payload.max_rounds < 1:
        raise HTTPException(status_code=400, detail="max_rounds muss >= 1 sein")
    board = await session.get(Board, payload.board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    loop = Loop(**payload.model_dump())
    session.add(loop)
    await session.commit()
    await session.refresh(loop)
    return loop


@router.get("/loops/{loop_id}")
async def get_loop(
    loop_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    loop = await session.get(Loop, loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    rounds = (await session.exec(
        select(LoopRound)
        .where(LoopRound.loop_id == loop_id)
        .order_by(LoopRound.round_no)
    )).all()
    return {**loop.model_dump(), "rounds": [r.model_dump() for r in rounds]}


@router.patch("/loops/{loop_id}")
async def update_loop(
    loop_id: uuid.UUID,
    payload: LoopUpdate,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    loop = await session.get(Loop, loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    if loop.status in TERMINAL_LOOP_STATUSES:
        raise HTTPException(status_code=409, detail="Loop ist abgeschlossen")
    data = payload.model_dump(exclude_unset=True)
    if "max_rounds" in data and (data["max_rounds"] is None or data["max_rounds"] < 1):
        raise HTTPException(status_code=400, detail="max_rounds muss >= 1 sein")
    if "pause_on_failed_rounds" in data and (data["pause_on_failed_rounds"] or 0) < 1:
        raise HTTPException(status_code=400, detail="pause_on_failed_rounds muss >= 1 sein")
    if "human_every_n_rounds" in data and (data["human_every_n_rounds"] or 0) < 0:
        raise HTTPException(status_code=400, detail="human_every_n_rounds muss >= 0 sein")
    for k, v in data.items():
        setattr(loop, k, v)
    loop.updated_at = utcnow()
    session.add(loop)
    await session.commit()
    await session.refresh(loop)
    return loop


@router.post("/loops/{loop_id}/start")
async def start_loop(
    loop_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    loop = await session.get(Loop, loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    if loop.status not in ("draft", "paused", "waiting_gate"):
        raise HTTPException(status_code=409, detail=f"Loop kann aus Status '{loop.status}' nicht gestartet werden")

    other = await _active_loop_on_board(session, loop.board_id, exclude=loop.id)
    if other:
        raise HTTPException(
            status_code=409,
            detail=f"Auf diesem Board läuft bereits Loop '{other.name}' — nur 1 aktiver Loop pro Board",
        )

    loop.status = "running"
    if loop.started_at is None:
        loop.started_at = utcnow()
    loop.consecutive_failed_rounds = 0
    loop.updated_at = utcnow()
    await _supersede_pending_gates(session, loop.id)
    session.add(loop)
    await session.commit()
    await session.refresh(loop)
    await emit_event(
        session, "loop.started",
        f"Loop '{loop.name}' gestartet ({loop.rounds_completed}/{loop.max_rounds} Runden)",
        board_id=loop.board_id, detail={"loop_id": str(loop.id)},
    )
    return loop


@router.post("/loops/{loop_id}/pause")
async def pause_loop(
    loop_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    loop = await session.get(Loop, loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    if loop.status not in ("running", "waiting_gate"):
        raise HTTPException(status_code=409, detail=f"Loop ist nicht aktiv (Status '{loop.status}')")
    loop.status = "paused"
    loop.updated_at = utcnow()
    await _supersede_pending_gates(session, loop.id)
    session.add(loop)
    await session.commit()
    await session.refresh(loop)
    await emit_event(
        session, "loop.paused", f"Loop '{loop.name}' pausiert (Operator)",
        board_id=loop.board_id, detail={"loop_id": str(loop.id), "reason": "operator"},
    )
    return loop


@router.post("/loops/{loop_id}/stop")
async def stop_loop(
    loop_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    """Beendet den Loop endgültig. Eine laufende Runde (normaler Task) läuft
    als gewöhnlicher Task weiter bzw. wird über die Task-Werkzeuge gestoppt —
    der Loop startet keine neue Runde mehr."""
    loop = await session.get(Loop, loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    if loop.status in TERMINAL_LOOP_STATUSES:
        raise HTTPException(status_code=409, detail="Loop ist bereits abgeschlossen")
    # Laufende Runde in der Historie sauber abschliessen (Review Mi5) —
    # der Runden-Task selbst läuft als normaler Task weiter.
    if loop.current_task_id is not None:
        running_round = (await session.exec(
            select(LoopRound).where(
                LoopRound.loop_id == loop.id,
                LoopRound.round_no == loop.current_round_no,
            )
        )).first()
        if running_round and running_round.outcome is None:
            running_round.outcome = "aborted"
            running_round.report = "Loop wurde vom Operator beendet — Runde abgebrochen."
            running_round.finished_at = utcnow()
            session.add(running_round)
        loop.current_task_id = None
    loop.status = "done"
    loop.finished_at = utcnow()
    loop.updated_at = utcnow()
    await _supersede_pending_gates(session, loop.id)
    session.add(loop)
    await session.commit()
    await session.refresh(loop)
    await emit_event(
        session, "loop.finished",
        f"Loop '{loop.name}' vom Operator beendet ({loop.rounds_completed} Runden)",
        board_id=loop.board_id,
        detail={"loop_id": str(loop.id), "reason": "operator_stop"},
    )
    return loop


@router.delete("/loops/{loop_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_loop(
    loop_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    loop = await session.get(Loop, loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    if loop.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Aktiven Loop erst stoppen, dann löschen")
    rounds = (await session.exec(
        select(LoopRound).where(LoopRound.loop_id == loop_id)
    )).all()
    for r in rounds:
        await session.delete(r)
    await _supersede_pending_gates(session, loop.id)
    await session.delete(loop)
    await session.commit()
