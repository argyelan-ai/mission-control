"""Tests for the blocker approval system.

Covers:
- blocker_decision approval is created when agent sets → blocked
- approval payload contains correct data (agent name, blocker comment, task title)
- blocker comment from TaskComment is captured in the payload
- lead gets an info RPC WITHOUT action options
- guard: agent gets 403 on blocked → in_progress with a pending approval
- guard also applies to the blocked developer themselves
- guard does NOT apply to an already resolved approval
- user API (operator) can unblock despite a pending approval
- resolution: operator approved → task in_progress, agent gets UNBLOCKED RPC
- resolution: operator rejected → task failed
- a second block after unblocking creates a new approval
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────


async def _create_blocker_data(*, task_status="in_progress"):
    """Create board + developer (worker) + lead + task."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # blocker_triage_minutes=0: diese Datei pinnt den DIREKT-Operator-Flow
        # (Legacy-Verhalten). Der Lead-first-Triage-Pfad (Default 15min) ist in
        # test_blocker_triage.py + test_incident_replay_2026_07_04.py gepinnt.
        board = Board(
            id=board_id, name="Blocker Board", slug="blocker",
            blocker_triage_minutes=0,
        )
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


# ── Test: approval is created on blocked ──────────────────────────


@pytest.mark.asyncio
async def test_blocked_creates_approval(client, fake_redis):
    """When agent sets task to blocked, a blocker_decision approval is created."""
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

    # Check approval in DB
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


# ── Test: guard — agent cannot unblock with a pending approval ────


@pytest.mark.asyncio
async def test_guard_blocks_unblock_with_pending_approval(client, fake_redis):
    """Worker: 403 bei blocked→in_progress mit pending Approval.
    Lead: DARF entblocken und supersedet dabei das Approval (Fix A —
    das alte Lead-403 war der Autonomie-Killer im Incident 2026-07-04)."""
    data = await _create_blocker_data(task_status="blocked")

    # Manually create approval (simulates what happens on blocked)
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
        # Worker bleibt gegated (403) — fuer BEIDE Wege aus blocked heraus.
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )
        assert resp.status_code == 403
        assert "Blocker-Approval" in resp.json()["detail"]

        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "inbox"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )
        assert resp.status_code == 403, "inbox-Loophole muss geschlossen sein"

        # Lead darf loesen — Approval wird dabei superseded (Fix A).
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {data['lead_token']}"},
        )
        assert resp.status_code == 200, resp.text

    from app.models.approval import Approval as _Ap
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(_Ap).where(_Ap.task_id == data["task"].id)
        )
        approvals = list(result.all())
    assert len(approvals) == 1
    assert approvals[0].status == "superseded"


@pytest.mark.asyncio
async def test_unblock_allowed_without_pending_approval(client, fake_redis):
    """Agent can unblock when there is no pending approval (legacy)."""
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


# ── Test: resolution — operator approved → task in_progress ──────────


@pytest.mark.asyncio
async def test_resolution_approved_unblocks_task(auth_client, fake_redis):
    """Operator approved blocker_decision → task goes to in_progress."""
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

    # Task goes to inbox (blocker → inbox → auto_dispatch as a background task)
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        assert task.status == "inbox"


# ── Test: resolution — operator rejected → task failed ───────────────


@pytest.mark.asyncio
async def test_resolution_rejected_fails_task(auth_client, fake_redis):
    """Operator rejected blocker_decision → task goes to failed."""
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

    # Task must be failed
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        assert task.status == "failed"


# ── Test: blocker comment is captured in the payload ──────────────────


@pytest.mark.asyncio
async def test_blocker_comment_captured_in_payload(client, fake_redis):
    """When agent posts a comment before the blocked status, it is stored in the approval payload."""
    data = await _create_blocker_data(task_status="in_progress")

    # Set comment before blocked
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


# ── Test: lead gets RPC WITHOUT action options ────────────────────


@pytest.mark.asyncio
async def test_lead_gets_info_rpc_without_options(client, fake_redis):
    """Lead gets an info TaskComment but WITHOUT 'options: 1. resolve, 2. assign, 3. ask operator'.

    Phase 29 (gateway sunset): the former rpc.chat_send message to the
    lead is now written as a TaskComment (`comment_type="blocker_lead_notify"`)
    on the task. The lead's cli-bridge poll.sh picks up the notification
    on the next tick.
    """
    from app.models.task import TaskComment
    data = await _create_blocker_data(task_status="in_progress")

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    # Check TaskComment notification to the lead
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

    # MUST contain: info that an approval was created
    assert "Approval" in msg
    assert "Operator-Entscheid" in msg
    # MUST NOT contain: action options
    assert "Optionen:" not in msg
    assert "Task einem anderen Agent zuweisen" not in msg
    assert "Blocker loesen" not in msg


# ── Test: guard also applies to the blocked developer ──────────────


@pytest.mark.asyncio
async def test_guard_blocks_developer_self_unblock(client, fake_redis):
    """Even the blocked developer themselves gets 403 when unblocking."""
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


# ── Test: guard does NOT apply to a resolved approval ────────────────────


@pytest.mark.asyncio
async def test_guard_allows_unblock_after_approval_resolved(client, fake_redis):
    """When approval is already resolved, agent can unblock."""
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
            status="approved",  # already resolved
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


# ── Test: user API can unblock despite a pending approval ─────────────


@pytest.mark.asyncio
async def test_user_api_blocked_by_pending_approval(auth_client, fake_redis):
    """The operator via the user API can NOT unblock while an approval is pending.

    Since phase 2A: the blocker approval guard also applies to the user route.
    The operator must unblock via approval resolution, not via a direct PATCH.
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


# ── Test: approved resolution sends UNBLOCKED RPC to agent ───────────


@pytest.mark.asyncio
async def test_approved_sends_unblocked_rpc_to_agent(auth_client, fake_redis):
    """Operator approved → agent gets UNBLOCKED RPC with the operator's instruction."""
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

    # Blocker resolution sets task to inbox, auto_dispatch runs in the background
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        assert task.status == "inbox"  # Ready for re-dispatch


# ── Test: second block creates a new approval ───────────────────────


@pytest.mark.asyncio
async def test_second_block_creates_new_approval(client, fake_redis):
    """After unblocking and blocking again, a new approval is created."""
    data = await _create_blocker_data(task_status="in_progress")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = True
            mock_rpc.chat_send = AsyncMock()

            # First block
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
                headers={"Authorization": f"Bearer {data['dev_token']}"},
            )
            assert resp.status_code == 200

    # Resolve the first approval + set task to in_progress + reset last_seen_at
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
        # Re-assign after auto-unassign from the first blocked. In production
        # this is done by auto_dispatch_task() after approval resolution;
        # here we simulate the end state.
        task.assigned_agent_id = data["developer"].id
        s.add(task)
        # Reset last_seen_at (SQLite timezone mismatch across multiple PATCH calls)
        dev = await s.get(Agent, data["developer"].id)
        dev.last_seen_at = None
        s.add(dev)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = True
            mock_rpc.chat_send = AsyncMock()

            # Second block
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "blocked", "blocker_type": "missing_info", "blocker_question": "Was soll ich tun?"},
                headers={"Authorization": f"Bearer {data['dev_token']}"},
            )
            assert resp.status_code == 200

    # Two approvals in DB (one approved, one pending)
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
