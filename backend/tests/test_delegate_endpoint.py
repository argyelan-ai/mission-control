"""Tests fuer `mc delegate` — atomarer Subtask + Parent-Block ohne Operator-Approval.

Deckt ab:
- Delegate erstellt Subtask + setzt Parent auf blocked mit blocked_by_task_id
- KEINE blocker_decision Approval entsteht (Orchestration, nicht Operator-Decision)
- Fire-and-Forget (--no-callback): Parent bleibt in_progress
- Defense-in-depth: PATCH status=blocked mit blocked_by_task_id → keine Approval
- Watchdog-Fallback: Subtask done ohne blocked_by_task_id-Link → Parent via
  parent_task_id + callback_agent_id auto-resumed
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_delegate_scenario():
    """Board + Orchestrator (Boss) + Worker (Researcher) + aktiver Parent-Task."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    researcher_id = uuid.uuid4()
    parent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Delegate Board", slug=f"deleg-{uuid.uuid4().hex[:6]}")
        s.add(board)

        boss_token, boss_hash = generate_agent_token()
        boss = Agent(
            id=boss_id,
            name="Boss",
            role="orchestrator",
            board_id=board_id,
            agent_token_hash=boss_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            current_task_id=parent_id,
        )
        s.add(boss)

        researcher = Agent(
            id=researcher_id,
            name="Researcher",
            role="researcher",
            board_id=board_id,
            agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        )
        s.add(researcher)

        parent = Task(
            id=parent_id,
            board_id=board_id,
            title="Boss Orchestration Task",
            status="in_progress",
            assigned_agent_id=boss_id,
        )
        s.add(parent)
        await s.commit()
        for obj in [board, boss, researcher, parent]:
            await s.refresh(obj)

    return {
        "board_id": board_id,
        "boss_id": boss_id,
        "researcher_id": researcher_id,
        "parent_id": parent_id,
        "boss_token": boss_token,
    }


@pytest.mark.asyncio
async def test_delegate_creates_subtask_and_blocks_parent(client, fake_redis):
    """Boss delegiert an Researcher → Subtask entsteht, Parent → blocked, KEINE Approval."""
    data = await _setup_delegate_scenario()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            with patch(
                "app.services.operations.check_dispatch_allowed",
                new_callable=AsyncMock,
                return_value=(True, None),
            ):
                resp = await client.post(
                    f"/api/v1/agent/boards/{data['board_id']}/delegate",
                    json={
                        "title": "Research: Brasil Shops CH",
                        "description": "Finde Online-Shops mit Lieferung in die Schweiz, die brasilianische Lebensmittel anbieten. Mind. 3 Anbieter mit Preisbereich + Versandkosten.",
                        "assigned_agent_id": str(data["researcher_id"]),
                        "callback": True,
                    },
                    headers={"Authorization": f"Bearer {data['boss_token']}"},
                )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["assigned_to"] == "Researcher"
    assert body["your_status"] == "blocked"
    subtask_id = uuid.UUID(body["subtask_id"])

    from app.models.task import Task
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = await s.get(Task, data["parent_id"])
        assert parent.status == "blocked"
        assert parent.blocked_by_task_id == subtask_id
        assert parent.callback_agent_id == data["boss_id"]

        subtask = await s.get(Task, subtask_id)
        assert subtask is not None
        assert subtask.parent_task_id == data["parent_id"]
        assert subtask.assigned_agent_id == data["researcher_id"]
        assert subtask.callback_agent_id == data["boss_id"]
        assert subtask.status == "inbox"

        # KEINE blocker_decision Approval — das ist Orchestration, nicht Operator-Decision
        result = await s.exec(
            select(Approval).where(
                Approval.task_id == data["parent_id"],
                Approval.action_type == "blocker_decision",
            )
        )
        assert result.first() is None


@pytest.mark.asyncio
async def test_delegate_fire_and_forget_keeps_parent_in_progress(client, fake_redis):
    """--no-callback: Parent bleibt in_progress, Subtask ohne callback_agent_id."""
    data = await _setup_delegate_scenario()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            with patch(
                "app.services.operations.check_dispatch_allowed",
                new_callable=AsyncMock,
                return_value=(True, None),
            ):
                resp = await client.post(
                    f"/api/v1/agent/boards/{data['board_id']}/delegate",
                    json={
                        "title": "Fire-and-Forget Subtask",
                        "description": "Asynchrone Aufgabe — Boss wartet nicht auf Ergebnis. Erledige wenn Zeit ist.",
                        "assigned_agent_id": str(data["researcher_id"]),
                        "callback": False,
                    },
                    headers={"Authorization": f"Bearer {data['boss_token']}"},
                )

    assert resp.status_code == 201
    assert resp.json()["your_status"] == "in_progress"

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = await s.get(Task, data["parent_id"])
        assert parent.status == "in_progress"
        assert parent.blocked_by_task_id is None

        subtask = await s.get(Task, uuid.UUID(resp.json()["subtask_id"]))
        assert subtask.callback_agent_id is None


@pytest.mark.asyncio
async def test_blocked_with_blocked_by_task_id_skips_approval(client, fake_redis):
    """Defense-in-depth: Agent setzt status=blocked mit blocked_by_task_id im Payload → keine Approval."""
    data = await _setup_delegate_scenario()

    # Zusaetzlichen Subtask erstellen, auf den der Parent wartet
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = Task(
            id=uuid.uuid4(),
            board_id=data["board_id"],
            title="Subtask",
            status="in_progress",
            parent_task_id=data["parent_id"],
            assigned_agent_id=data["researcher_id"],
        )
        s.add(sub)
        await s.commit()
        await s.refresh(sub)
        subtask_id = sub.id

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['parent_id']}",
                json={
                    "status": "blocked",
                    "blocked_by_task_id": str(subtask_id),
                },
                headers={"Authorization": f"Bearer {data['boss_token']}"},
            )

    assert resp.status_code == 200

    # KEINE blocker_decision Approval
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(Approval).where(Approval.task_id == data["parent_id"])
        )
        assert result.first() is None


@pytest.mark.asyncio
async def test_approval_bypass_via_random_blocked_by_task_id_rejected(client, fake_redis):
    """C1-Regression: PATCH status=blocked mit random UUID in blocked_by_task_id → 422.

    Ohne diesen Guard koennte jeder Agent mit tasks:write den Operator-Approval-Flow
    umgehen indem er eine beliebige UUID in blocked_by_task_id setzt.
    """
    data = await _setup_delegate_scenario()
    random_uuid = str(uuid.uuid4())

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['parent_id']}",
                json={
                    "status": "blocked",
                    "blocked_by_task_id": random_uuid,
                },
                headers={"Authorization": f"Bearer {data['boss_token']}"},
            )

    assert resp.status_code == 422
    assert "blocked_by_task_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delegate_rejects_cross_board_target(client, fake_redis):
    """C2-Regression: Target-Agent auf anderem Board → 422."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    data = await _setup_delegate_scenario()

    # Fremder Agent auf anderem Board
    other_board_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=other_board_id, name="Other", slug=f"other-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=other_agent_id, name="Intruder", role="developer",
            board_id=other_board_id,
            agent_token_hash=generate_agent_token()[1],
            provision_status="provisioned",
        ))
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{data['board_id']}/delegate",
        json={
            "title": "Cross-board leak attempt",
            "description": "Sensitive info should stay on this board",
            "assigned_agent_id": str(other_agent_id),
        },
        headers={"Authorization": f"Bearer {data['boss_token']}"},
    )
    assert resp.status_code == 422
    assert "Board" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delegate_rejects_not_provisioned_target(client, fake_redis):
    """M4-Regression: Target-Agent mit provision_status != 'provisioned' → 422."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    data = await _setup_delegate_scenario()

    local_agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=local_agent_id, name="NotReady", role="developer",
            board_id=data["board_id"],
            agent_token_hash=generate_agent_token()[1],
            provision_status="local",  # not provisioned
        ))
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{data['board_id']}/delegate",
        json={
            "title": "x",
            "description": "should fail because target not provisioned yet",
            "assigned_agent_id": str(local_agent_id),
        },
        headers={"Authorization": f"Bearer {data['boss_token']}"},
    )
    assert resp.status_code == 422
    assert "provisioniert" in resp.json()["detail"].lower() or "provision" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delegate_503_when_dispatch_disallowed(client, fake_redis):
    """C3-Regression: wenn check_dispatch_allowed False → 503 + KEIN Subtask in DB."""
    from app.models.task import Task
    from sqlmodel import func

    data = await _setup_delegate_scenario()

    # Anzahl Tasks vor Aufruf
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(func.count()).select_from(Task))
        tasks_before = result.one()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch(
            "app.services.operations.check_dispatch_allowed",
            new_callable=AsyncMock,
            return_value=(False, "System HALTED"),
        ):
            resp = await client.post(
                f"/api/v1/agent/boards/{data['board_id']}/delegate",
                json={
                    "title": "Should be blocked",
                    "description": "System ist halted — kein neuer Subtask erlaubt",
                    "assigned_agent_id": str(data["researcher_id"]),
                },
                headers={"Authorization": f"Bearer {data['boss_token']}"},
            )

    assert resp.status_code == 503
    assert "HALTED" in resp.json()["detail"]

    # Kein Zombie-Subtask in DB
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(func.count()).select_from(Task))
        tasks_after = result.one()
    assert tasks_after == tasks_before, "Subtask darf NICHT persistiert sein wenn Dispatch disallowed"


@pytest.mark.asyncio
async def test_fallback_resume_skips_when_pending_approval():
    """H2-Regression: Fallback-Resume darf Parent mit pending blocker_decision NICHT aufwecken."""
    from app.routers.agent_scoped import _handle_callback_resume
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.approval import Approval
    from app.auth import generate_agent_token
    from app.utils import utcnow
    from datetime import timedelta

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="FB-H2", slug=f"fb2-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=boss_id, name="Boss", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        # Parent blocked, blocked_by_task_id=NULL, aber PENDING Operator-Approval
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent with pending approval",
            status="blocked", assigned_agent_id=boss_id,
        ))
        s.add(Task(
            id=subtask_id, board_id=board_id, title="Unrelated old subtask",
            status="done", parent_task_id=parent_id,
            callback_agent_id=boss_id,
        ))
        # Echter Operator-Blocker pending
        s.add(Approval(
            id=uuid.uuid4(), board_id=board_id, task_id=parent_id,
            agent_id=boss_id, action_type="blocker_decision",
            description="Real blocker waiting for operator",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        ))
        await s.commit()

        subtask = await s.get(Task, subtask_id)
        with patch(
            "app.routers.agent_scoped.dispatch_callback_to_parent",
            new_callable=AsyncMock,
        ):
            with patch("app.services.activity.emit_event", new_callable=AsyncMock):
                await _handle_callback_resume(s, subtask)

        parent = await s.get(Task, parent_id)
        assert parent.status == "blocked", \
            "Parent muss blocked BLEIBEN wenn pending blocker_decision existiert"


@pytest.mark.asyncio
async def test_callback_resume_fallback_via_parent_task_id():
    """Watchdog-Fallback: Subtask done, Parent blocked aber blocked_by_task_id=NULL
    (Agent hat `mc delegate` umgangen) → Resume via parent_task_id + callback_agent_id.
    """
    from app.routers.agent_scoped import _handle_callback_resume
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    boss_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Fallback", slug=f"fb-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=boss_id, name="Boss", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        # Parent ist blocked ABER blocked_by_task_id ist NULL (vergessener Link)
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent",
            status="blocked", assigned_agent_id=boss_id,
            blocked_by_task_id=None,
        ))
        # Subtask ist done, zeigt via parent_task_id + callback_agent_id auf Boss
        s.add(Task(
            id=subtask_id, board_id=board_id, title="Subtask",
            status="done", parent_task_id=parent_id,
            callback_agent_id=boss_id,
        ))
        await s.commit()

        subtask = await s.get(Task, subtask_id)
        with patch(
            "app.routers.agent_scoped.dispatch_callback_to_parent",
            new_callable=AsyncMock,
        ):
            with patch("app.services.activity.emit_event", new_callable=AsyncMock):
                await _handle_callback_resume(s, subtask)

        await s.refresh(await s.get(Task, parent_id))
        parent = await s.get(Task, parent_id)
        assert parent.status == "in_progress", "Parent sollte via Fallback geweckt werden"
        assert parent.blocked_by_task_id is None
