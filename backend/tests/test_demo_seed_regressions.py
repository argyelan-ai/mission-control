"""Regressionen aus dem Demo-Seed-Testlauf (2026-07-02, Migration 0132).

1. approvals.agent_id muss nullable sein — der Watchdog erstellt
   review_stuck-Approvals fuer Tasks OHNE zugewiesenen Agent. Mit NOT NULL
   crashte jeder Watchdog-Tick (Commit-Fehler -> Redis-Dedup nie gesetzt
   -> Endlos-Retry).

2. delete_board() ist Soft-Delete, boards.slug ist UNIQUE — ohne
   Slug-Umbenennung blockiert ein geloeschtes Board seinen Slug fuer
   immer (Neuanlage -> 500 UniqueViolation).
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_approval_without_agent_persists(make_board, make_task):
    """review_stuck-Approval fuer unassigned Task darf nicht crashen."""
    from app.models.approval import Approval

    board = await make_board(slug="approval-null-agent")
    task = await make_task(board_id=board.id, status="review")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            board_id=board.id,
            task_id=task.id,
            agent_id=None,  # Task hat keinen Agent — genau der Crash-Fall
            action_type="review_stuck",
            description="Review haengt seit 185 Min.",
        ))
        await s.commit()

        saved = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).first()
        assert saved is not None
        assert saved.agent_id is None


@pytest.mark.asyncio
async def test_deleted_board_frees_its_slug(auth_client: AsyncClient):
    """Archivieren benennt den Slug um — Neuanlage mit gleichem Slug geht."""
    payload = {"name": "Demo", "slug": "demo-slug-reuse"}

    first = await auth_client.post("/api/v1/boards", json=payload)
    assert first.status_code in (200, 201), first.text
    board_id = first.json()["id"]

    deleted = await auth_client.delete(f"/api/v1/boards/{board_id}")
    assert deleted.status_code == 204, deleted.text

    second = await auth_client.post("/api/v1/boards", json=payload)
    assert second.status_code in (200, 201), second.text
    assert second.json()["id"] != board_id

    # Das archivierte Board traegt den umbenannten Slug
    from app.models.board import Board
    async with AsyncSession(test_engine) as s:
        old = await s.get(Board, uuid.UUID(board_id))
        assert old.is_archived is True
        assert old.slug.startswith("demo-slug-reuse--archived-")
