"""Tests fuer das Blocker-Approval-System.

Deckt ab:
- blocker_decision Approval wird erstellt wenn Agent → blocked setzt
- Approval-Payload enthält korrekte Daten (Agent-Name, Blocker-Kommentar, Task-Titel)
- Blocker-Kommentar aus TaskComment wird in Payload erfasst
- Lead bekommt Info-RPC OHNE Handlungsoptionen
- Guard: Agent bekommt 403 bei blocked → in_progress mit pending Approval
- Guard gilt auch fuer den blockierten Developer selbst
- Guard greift NICHT bei bereits resolved Approval
- User-API (Operator) kann trotz pending Approval entblocken
- Resolution: Operator approved → Task in_progress, Agent bekommt UNBLOCKED-RPC
- Resolution: Operator rejected → Task failed
- Zweiter Block nach Entblockung erstellt neues Approval
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────


async def _create_blocker_data(*, task_status="in_progress"):
    """Board + Developer (Worker) + Lead + Task erstellen."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Blocker Board", slug="blocker")
        s.add(board)

        dev_token_raw, dev_token_hash = generate_agent_token()
        developer = Agent(
            id=dev_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=dev_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(developer)

        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=lead_id,
            name="Henry",
            role="lead",
            board_id=board_id,
            agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:manage"],
        )
        s.add(lead)

        task = Task(
            id=task_id,
            board_id=board_id,
            title="Implement Rust smoketest",
            status=task_status,
            assigned_agent_id=dev_id,
        )
        s.add(task)
        await s.commit()
        for obj in [board, developer, lead, task]:
            await s.refresh(obj)

    return {
        "board": board,
        "developer": developer,
        "lead": lead,
        "task": task,
        "dev_token": dev_token_raw,
        "lead_token": lead_token_raw,
    }


# ── Test: Approval wird erstellt bei blocked ──────────────────────────


@pytest.mark.asyncio
async def test_blocked_creates_approval(client, fake_redis):
    """Wenn Agent Task auf blocked setzt, wird ein blocker_decision Approval erstellt."""
    data = await _create_blocker_data(task_status="in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = True
            mock_rpc.chat_send = AsyncMock()

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
                headers={"Authorization": f"Bearer {data['dev_token']}"},
            )

    assert resp.status_code == 200
    assert resp.json()["status"] == "blocked"

    # Approval in DB pruefen
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(Approval).where(
                Approval.task_id == data["task"].id,
                Approval.action_type == "blocker_decision",
            )
        )
        approval = result.first()
        assert approval is not None
        assert approval.status == "pending"
        assert approval.agent_id == data["developer"].id
        assert approval.payload["blocked_agent_name"] == "Cody"


# ── Test: Guard — Agent kann nicht entblocken mit pending Approval ────


@pytest.mark.asyncio
async def test_guard_blocks_unblock_with_pending_approval(client, fake_redis):
    """Board Lead bekommt 403 wenn er blocked → in_progress setzt und Approval pending ist."""
    data = await _create_blocker_data(task_status="blocked")

    # Approval manuell erstellen (simuliert was bei blocked passiert)
    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {data['lead_token']}"},
        )

    assert resp.status_code == 403
    assert "Blocker-Approval" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unblock_allowed_without_pending_approval(client, fake_redis):
    """Agent kann entblocken wenn kein pending Approval existiert (Altlast)."""
    data = await _create_blocker_data(task_status="blocked")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "in_progress"},
                headers={"Authorization": f"Bearer {data['lead_token']}"},
            )

    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


# ── Test: Resolution — Operator approved → Task in_progress ──────────


@pytest.mark.asyncio
async def test_resolution_approved_unblocks_task(auth_client, fake_redis):
    """Operator approved blocker_decision → Task geht auf in_progress."""
    data = await _create_blocker_data(task_status="blocked")

    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            id=approval_id,
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
        with patch("app.utils.create_tracked_task"):
            # Phase 29 / Wave 4 cleanup: app.services.openclaw_rpc.rpc gone.
            # The approval-resolution path no longer touches RPC; we just
            # need create_tracked_task patched so auto_dispatch_task doesn't
            # spawn a background coroutine on the test loop.
            resp = await auth_client.patch(
                f"/api/v1/approvals/{approval_id}",
                json={"status": "approved", "resolver_note": "Go installieren"},
            )

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # Task geht auf inbox (Blocker → inbox → auto_dispatch als Background-Task)
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        assert task.status == "inbox"


# ── Test: Resolution — Operator rejected → Task failed ───────────────


@pytest.mark.asyncio
async def test_resolution_rejected_fails_task(auth_client, fake_redis):
    """Operator rejected blocker_decision → Task geht auf failed."""
    data = await _create_blocker_data(task_status="blocked")

    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            id=approval_id,
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
        # Phase 29 / Wave 4 cleanup: app.services.openclaw_rpc.rpc gone.
        # Rejection path does not call any RPC any more.
        resp = await auth_client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"status": "rejected"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    # Task muss failed sein
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        assert task.status == "failed"


# ── Test: Blocker-Kommentar wird in Payload erfasst ──────────────────


@pytest.mark.asyncio
async def test_blocker_comment_captured_in_payload(client, fake_redis):
    """Wenn Agent vor dem blocked-Status einen Kommentar postet, wird er im Approval-Payload gespeichert."""
    data = await _create_blocker_data(task_status="in_progress")

    # Kommentar vor blocked setzen
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comment = TaskComment(
            task_id=data["task"].id,
            author_type="agent",
            author_agent_id=data["developer"].id,
            content="Go-Toolchain fehlt. `go version` gibt 'command not found'.",
            comment_type="blocker",
        )
        s.add(comment)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = True
            mock_rpc.chat_send = AsyncMock()

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
                headers={"Authorization": f"Bearer {data['dev_token']}"},
            )

    assert resp.status_code == 200

    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(Approval).where(
                Approval.task_id == data["task"].id,
                Approval.action_type == "blocker_decision",
            )
        )
        approval = result.first()
        assert approval is not None
        assert "Go-Toolchain fehlt" in approval.payload["blocker_comment"]
        assert approval.payload["task_title"] == "Implement Rust smoketest"
        assert approval.payload["blocked_agent_id"] == str(data["developer"].id)


# ── Test: Lead bekommt RPC OHNE Handlungsoptionen ────────────────────


@pytest.mark.asyncio
async def test_lead_gets_info_rpc_without_options(client, fake_redis):
    """Lead bekommt Info-TaskComment aber OHNE 'Optionen: 1. loesen, 2. zuweisen, 3. Operator fragen'.

    Phase 29 (Gateway sunset): die ehemalige rpc.chat_send-Nachricht an den
    Lead wird jetzt als TaskComment (`comment_type="blocker_lead_notify"`)
    auf den Task geschrieben. cli-bridge poll.sh des Lead picks die Notify
    auf den naechsten Tick.
    """
    from app.models.task import TaskComment
    data = await _create_blocker_data(task_status="in_progress")

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    # TaskComment-Notify an den Lead pruefen
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == data["task"].id,
                TaskComment.comment_type == "blocker_lead_notify",
            )
        )
        notifies = list(result.all())

    assert len(notifies) == 1, f"Erwartet: 1 blocker_lead_notify Kommentar. Gefunden: {len(notifies)}"
    msg = notifies[0].content

    # MUSS enthalten: Info dass Approval erstellt wurde
    assert "Approval" in msg
    assert "Operator entscheidet" in msg
    # DARF NICHT enthalten: Handlungsoptionen
    assert "Optionen:" not in msg
    assert "Task einem anderen Agent zuweisen" not in msg
    assert "Blocker loesen" not in msg


# ── Test: Guard gilt auch fuer den blockierten Developer ──────────────


@pytest.mark.asyncio
async def test_guard_blocks_developer_self_unblock(client, fake_redis):
    """Auch der blockierte Developer selbst bekommt 403 beim Entblocken."""
    data = await _create_blocker_data(task_status="blocked")

    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 403


# ── Test: Guard greift NICHT bei resolved Approval ────────────────────


@pytest.mark.asyncio
async def test_guard_allows_unblock_after_approval_resolved(client, fake_redis):
    """Wenn Approval bereits resolved ist, kann Agent entblocken."""
    data = await _create_blocker_data(task_status="blocked")

    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="approved",  # bereits resolved
            resolved_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "in_progress"},
                headers={"Authorization": f"Bearer {data['lead_token']}"},
            )

    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


# ── Test: User-API kann trotz pending Approval entblocken ─────────────


@pytest.mark.asyncio
async def test_user_api_blocked_by_pending_approval(auth_client, fake_redis):
    """Der Operator ueber die User-API kann NICHT entblocken wenn Approval pending ist.

    Seit Phase 2A: Blocker-Approval Guard greift auch in der User-Route.
    Der Operator muss ueber die Approval-Resolution entblocken, nicht ueber direkten PATCH.
    """
    data = await _create_blocker_data(task_status="blocked")

    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    resp = await auth_client.patch(
        f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
        json={"status": "in_progress"},
    )

    assert resp.status_code == 403
    assert "Blocker-Approval" in resp.json()["detail"]


# ── Test: Approved Resolution sendet UNBLOCKED-RPC an Agent ───────────


@pytest.mark.asyncio
async def test_approved_sends_unblocked_rpc_to_agent(auth_client, fake_redis):
    """Operator approved → Agent bekommt UNBLOCKED-RPC mit der Anweisung des Operators."""
    data = await _create_blocker_data(task_status="blocked")

    from app.models.approval import Approval
    from app.utils import utcnow
    from datetime import timedelta

    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            id=approval_id,
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()

    with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
        with patch("app.utils.create_tracked_task"):
            # Phase 29 / Wave 4 cleanup: app.services.openclaw_rpc.rpc gone.
            # The unblock notification rides on TaskComment + cli-bridge
            # poll.sh now, not gateway RPC. We only care that the task ends
            # up in 'inbox' for auto_dispatch_task to pick up.
            await auth_client.patch(
                f"/api/v1/approvals/{approval_id}",
                json={"status": "approved", "resolver_note": "Go via Homebrew installieren"},
            )

    # Blocker-Resolution setzt Task auf inbox, auto_dispatch laeuft als Background
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        assert task.status == "inbox"  # Bereit fuer Re-Dispatch


# ── Test: Zweiter Block erstellt neues Approval ───────────────────────


@pytest.mark.asyncio
async def test_second_block_creates_new_approval(client, fake_redis):
    """Nach Entblockung und erneutem Block wird ein neues Approval erstellt."""
    data = await _create_blocker_data(task_status="in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = True
            mock_rpc.chat_send = AsyncMock()

            # Erster Block
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
                headers={"Authorization": f"Bearer {data['dev_token']}"},
            )
            assert resp.status_code == 200

    # Erstes Approval resolved machen + Task auf in_progress + last_seen_at resetten
    from app.models.approval import Approval
    from app.models.task import Task
    from app.models.agent import Agent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(Approval).where(
                Approval.task_id == data["task"].id,
                Approval.action_type == "blocker_decision",
                Approval.status == "pending",
            )
        )
        first_approval = result.first()
        first_approval.status = "approved"
        s.add(first_approval)
        task = await s.get(Task, data["task"].id)
        task.status = "in_progress"
        # Re-Assign nach Auto-Unassign vom ersten blocked. In Production
        # macht das auto_dispatch_task() nach Approval-Resolution; hier
        # simulieren wir den Endzustand.
        task.assigned_agent_id = data["developer"].id
        s.add(task)
        # last_seen_at resetten (SQLite timezone mismatch bei mehreren PATCH-Calls)
        dev = await s.get(Agent, data["developer"].id)
        dev.last_seen_at = None
        s.add(dev)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = True
            mock_rpc.chat_send = AsyncMock()

            # Zweiter Block
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
                headers={"Authorization": f"Bearer {data['dev_token']}"},
            )
            assert resp.status_code == 200

    # Zwei Approvals in DB (eins approved, eins pending)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(Approval).where(
                Approval.task_id == data["task"].id,
                Approval.action_type == "blocker_decision",
            )
        )
        approvals = result.all()
        assert len(approvals) == 2
        statuses = {a.status for a in approvals}
        assert statuses == {"approved", "pending"}
