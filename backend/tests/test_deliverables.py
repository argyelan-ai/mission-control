"""Tests fuer Task Deliverables — Agent-registrierte Ergebnisse pro Task.

Testet:
1. Deliverable erstellen (Agent-scoped POST)
2. Ungueltige deliverable_type → 422
3. Leerer Titel → 422
4. Leere Liste bei Task ohne Deliverables
5. Mehrere Deliverables erstellen + auflisten (neuestes zuerst)
6. Agent postet auf falsches Board → 403
7. User-facing GET liefert Deliverables mit agent_name
"""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.deliverable import TaskDeliverable


# ── Validation (Pydantic) ────────────────────────────────────────────────

class TestDeliverableValidation:

    def test_invalid_type_rejected(self):
        """deliverable_type ausserhalb der erlaubten Werte → Validation Error."""
        from app.routers.agent_scoped import DeliverableCreate

        with pytest.raises(Exception):
            DeliverableCreate(
                deliverable_type="invalid",
                title="Test Deliverable",
            )

    def test_empty_title_rejected(self):
        """Leerer Titel → Validation Error."""
        from app.routers.agent_scoped import DeliverableCreate

        with pytest.raises(Exception):
            DeliverableCreate(
                deliverable_type="file",
                title="   ",
            )

    def test_valid_deliverable_create(self):
        """Gueltige Eingabe → OK."""
        from app.routers.agent_scoped import DeliverableCreate

        d = DeliverableCreate(
            deliverable_type="screenshot",
            title="Login Page Screenshot",
            path="/screenshots/login.png",
            description="Screenshot nach dem Login-Redesign",
        )
        assert d.title == "Login Page Screenshot"
        assert d.deliverable_type == "screenshot"


# ── Model + DB ───────────────────────────────────────────────────────────

class TestDeliverableModel:

    @pytest.mark.asyncio
    async def test_create_deliverable(self, session: AsyncSession, make_agent, make_task):
        """Deliverable kann erstellt werden und hat id + created_at."""
        board_id = uuid.uuid4()
        agent = await make_agent("DelivAgent", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Deliverable Task", assigned_agent_id=agent.id)

        deliverable = TaskDeliverable(
            task_id=task.id,
            agent_id=agent.id,
            deliverable_type="screenshot",
            title="Login Page Screenshot",
            path="/screenshots/login.png",
            description="Nach dem Redesign",
        )
        session.add(deliverable)
        await session.commit()
        await session.refresh(deliverable)

        assert deliverable.id is not None
        assert deliverable.created_at is not None
        assert deliverable.title == "Login Page Screenshot"
        assert deliverable.deliverable_type == "screenshot"
        assert deliverable.path == "/screenshots/login.png"

    @pytest.mark.asyncio
    async def test_list_deliverables_empty(self, session: AsyncSession, make_task):
        """Task ohne Deliverables → leere Liste."""
        from sqlmodel import select

        board_id = uuid.uuid4()
        task = await make_task(board_id, title="Empty Task")

        result = await session.exec(
            select(TaskDeliverable).where(TaskDeliverable.task_id == task.id)
        )
        deliverables = result.all()
        assert deliverables == []

    @pytest.mark.asyncio
    async def test_list_deliverables_ordered(self, session: AsyncSession, make_agent, make_task):
        """Mehrere Deliverables werden nach created_at desc sortiert."""
        from datetime import datetime, timedelta
        from sqlmodel import select

        board_id = uuid.uuid4()
        agent = await make_agent("ListAgent", board_id=board_id, role="developer")
        task = await make_task(board_id, title="Multi-Deliv Task", assigned_agent_id=agent.id)

        now = datetime.utcnow()
        d1 = TaskDeliverable(
            task_id=task.id, agent_id=agent.id,
            deliverable_type="file", title="First Deliverable",
            created_at=now - timedelta(minutes=10),
        )
        d2 = TaskDeliverable(
            task_id=task.id, agent_id=agent.id,
            deliverable_type="url", title="Second Deliverable",
            created_at=now,
        )
        session.add(d1)
        session.add(d2)
        await session.commit()

        result = await session.exec(
            select(TaskDeliverable)
            .where(TaskDeliverable.task_id == task.id)
            .order_by(TaskDeliverable.created_at.desc())  # type: ignore[union-attr]
        )
        deliverables = result.all()
        assert len(deliverables) == 2
        assert deliverables[0].title == "Second Deliverable"
        assert deliverables[1].title == "First Deliverable"


# ── API Endpoints (User-facing) ──────────────────────────────────────────

class TestDeliverableUserAPI:

    @pytest.mark.asyncio
    async def test_user_list_deliverables_empty(self, auth_client, make_board, make_task):
        """User-facing GET auf Task ohne Deliverables → leere Liste."""
        board = await make_board("Deliv Board", slug="deliv-board")
        task = await make_task(board.id, title="No Deliverables")

        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_user_list_deliverables_with_agent_name(
        self, auth_client, session: AsyncSession, make_board, make_agent, make_task,
    ):
        """User-facing GET liefert Deliverables mit aufgeloestem agent_name."""
        board = await make_board("Deliv Board 2", slug="deliv-board-2")
        agent = await make_agent("Cody", board_id=board.id, role="developer")
        task = await make_task(board.id, title="Deliv Task", assigned_agent_id=agent.id)

        # Deliverable direkt in DB anlegen
        deliverable = TaskDeliverable(
            task_id=task.id,
            agent_id=agent.id,
            deliverable_type="artifact",
            title="API Docs",
            description="Generierte API-Dokumentation",
        )
        session.add(deliverable)
        await session.commit()

        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "API Docs"
        assert data[0]["agent_name"] == "Cody"
        assert data[0]["deliverable_type"] == "artifact"
        assert data[0]["id"] is not None
        assert data[0]["created_at"] is not None

    @pytest.mark.asyncio
    async def test_user_list_deliverables_multiple(
        self, auth_client, session: AsyncSession, make_board, make_agent, make_task,
    ):
        """User-facing GET liefert mehrere Deliverables, neuestes zuerst."""
        from datetime import datetime, timedelta

        board = await make_board("Deliv Board 3", slug="deliv-board-3")
        agent = await make_agent("Rex", board_id=board.id, role="reviewer")
        task = await make_task(board.id, title="Multi Deliv", assigned_agent_id=agent.id)

        now = datetime.utcnow()
        d1 = TaskDeliverable(
            task_id=task.id, agent_id=agent.id,
            deliverable_type="screenshot", title="Older Screenshot",
            created_at=now - timedelta(minutes=5),
        )
        d2 = TaskDeliverable(
            task_id=task.id, agent_id=agent.id,
            deliverable_type="url", title="Newer URL",
            created_at=now,
        )
        session.add(d1)
        session.add(d2)
        await session.commit()

        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{task.id}/deliverables"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Neuestes zuerst
        assert data[0]["title"] == "Newer URL"
        assert data[1]["title"] == "Older Screenshot"

    @pytest.mark.asyncio
    async def test_user_list_deliverables_wrong_task(self, auth_client, make_board):
        """User-facing GET mit nicht-existierender Task-ID → 404."""
        board = await make_board("Deliv Board 4", slug="deliv-board-4")
        fake_task_id = uuid.uuid4()

        resp = await auth_client.get(
            f"/api/v1/boards/{board.id}/tasks/{fake_task_id}/deliverables"
        )
        assert resp.status_code == 404


# ── Agent-facing POST: Content-Required für document/data ─────────────────

class TestDeliverableContentRequired:
    """document/data Deliverables müssen inline content haben — path allein ist Container-intern."""

    @pytest.mark.asyncio
    async def test_document_without_content_rejected(self, client, make_agent, make_task):
        from app.auth import generate_agent_token
        from app.models.agent import Agent
        from sqlmodel import select
        from .conftest import test_engine

        board_id = uuid.uuid4()
        agent = await make_agent("DocAgent", board_id=board_id, role="researcher", scopes=["tasks:write"])
        task = await make_task(board_id, title="Doc Task", assigned_agent_id=agent.id)

        raw_token, token_hash = generate_agent_token()
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_agent = (await s.exec(select(Agent).where(Agent.id == agent.id))).one()
            db_agent.agent_token_hash = token_hash
            s.add(db_agent)
            await s.commit()

        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}/deliverables",
            json={"deliverable_type": "document", "title": "Missing Content", "path": "/home/agent/x.md"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 400
        assert "content" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_document_with_content_accepted(self, client, make_agent, make_task):
        from app.auth import generate_agent_token
        from app.models.agent import Agent
        from sqlmodel import select
        from .conftest import test_engine

        board_id = uuid.uuid4()
        agent = await make_agent("DocAgent2", board_id=board_id, role="researcher", scopes=["tasks:write"])
        task = await make_task(board_id, title="Doc Task 2", assigned_agent_id=agent.id)

        raw_token, token_hash = generate_agent_token()
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_agent = (await s.exec(select(Agent).where(Agent.id == agent.id))).one()
            db_agent.agent_token_hash = token_hash
            s.add(db_agent)
            await s.commit()

        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "document",
                "title": "With Content",
                "content": "# Research\n\nErgebnisse...",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 201
