"""Tests fuer Auto-Unassign bei Status-Uebergang nach failed/blocked.

Bug-Hintergrund (2026-04-23): Wenn ein Task auf failed/blocked gesetzt wird
ohne assigned_agent_id zu loeschen, geraet der Agent in eine stille
Cancel-Schleife: agent_poll prueft als ERSTES ob es einen failed Task fuer
den Agent gibt → liefert state="cancelled" → poll.sh sendet ESC → naechster
Poll: gleiche Antwort. Endlos. Neue Tasks werden NIE delivered weil der
failed Task immer Vorrang hat.

Fix: zentralen Helper apply_terminal_unassign() der bei jedem Uebergang nach
failed/blocked das assigned_agent_id auf NULL setzt. Ausnahme: blocked mit
blocked_by_task_id (Callback-Wait) — der Parent-Agent muss assigned bleiben
damit das Resume zurueckrouten kann.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.auth import generate_agent_token
from app.services.task_lifecycle import apply_terminal_unassign

from .conftest import test_engine


# ── Unit-Tests fuer apply_terminal_unassign ──────────────────────────────


@pytest.mark.asyncio
async def test_apply_terminal_unassign_failed_clears_assignment():
    """Uebergang → failed: assigned_agent_id wird auf None gesetzt."""
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b1")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id, current_task_id=task_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

        changed = await apply_terminal_unassign(s, task, "failed")
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    assert changed is True
    assert task.assigned_agent_id is None
    assert agent.current_task_id is None


@pytest.mark.asyncio
async def test_apply_terminal_unassign_blocked_without_callback_preserves_assignment():
    """Uebergang → blocked OHNE blocked_by_task_id: assigned_agent_id BLEIBT erhalten.

    Geaendert am 2026-04-24 (PR #111): Frueher wurde assigned_agent_id gecleaned,
    was dazu fuehrte dass nach mc blocked + Operator-Approval der Task zum Board-Lead
    eskalierte statt zum Original-Worker zurueck. Jetzt:
    - Task.assigned_agent_id bleibt (Worker bekommt Task beim Resume zurueck)
    - Agent.current_task_id wird freigegeben (Lock) damit der Worker andere Tasks nehmen kann
    - Agent.run_state → "blocked" zur Info
    """
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b2")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id, current_task_id=task_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
            blocked_by_task_id=None,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

        changed = await apply_terminal_unassign(s, task, "blocked")
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    # Post-PR-#111: assignment bleibt, nur Lock wird gelöst
    assert changed is False, "blocked darf nicht mehr unassignen"
    assert task.assigned_agent_id == agent_id, "Worker bleibt assigned fuer Resume"
    assert agent.current_task_id is None, "Lock wird trotzdem freigegeben"
    # run_state Wechsel passiert nur wenn der alte state "running" oder None war
    # (sonst bleibt idle/offline unveraendert). Im Test hatten wir keinen expliziten
    # run_state, default ist "idle" in agents.py — kein Wechsel erwartet.


@pytest.mark.asyncio
async def test_apply_terminal_unassign_blocked_with_callback_keeps_assignment():
    """Uebergang → blocked MIT blocked_by_task_id: assigned_agent_id BLEIBT.

    Struktureller Callback-Wait (help_request, delegate). Der Parent-Agent
    muss assigned bleiben, sonst kann der Resume nach Subtask-done nicht
    zum richtigen Agent zurueckrouten.
    """
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    sub_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b3")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id, current_task_id=task_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
            blocked_by_task_id=sub_id,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

        changed = await apply_terminal_unassign(s, task, "blocked")
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    assert changed is False, "Callback-Wait darf nicht unassignen"
    assert task.assigned_agent_id == agent_id
    # current_task_id darf hier nicht angefasst werden (Worker arbeitet noch
    # weiter wenn der Subtask zurueckkommt)
    assert agent.current_task_id == task_id


@pytest.mark.asyncio
async def test_apply_terminal_unassign_no_op_for_other_status():
    """Uebergang → done/in_progress/review: keine Aktion."""
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b4")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)

        for new_status in ("done", "in_progress", "review", "inbox", "user_test"):
            changed = await apply_terminal_unassign(s, task, new_status)
            assert changed is False, f"{new_status} darf nichts aendern"
            assert task.assigned_agent_id == agent_id


@pytest.mark.asyncio
async def test_apply_terminal_unassign_already_unassigned_safe():
    """Defensive: wenn assigned_agent_id bereits None ist, kein Crash."""
    board_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b5")
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=None,
        )
        s.add_all([board, task])
        await s.commit()
        await s.refresh(task)

        # Watchdog hat Task schon unassignt → Helper darf nichts brechen
        changed = await apply_terminal_unassign(s, task, "failed")
        assert changed is False
        assert task.assigned_agent_id is None


# ── Integrationstests via PATCH-Endpoints ────────────────────────────────


async def _setup_basic(*, task_status: str = "in_progress", blocked_by: uuid.UUID | None = None):
    """Board + Worker + Task fuer Integration-Tests."""
    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Bug Board", slug=f"bug-{uuid.uuid4().hex[:6]}")
        s.add(board)
        worker = Agent(
            id=worker_id,
            name="Worker",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            current_task_id=task_id,
        )
        s.add(worker)
        task = Task(
            id=task_id,
            board_id=board_id,
            title="Worker Task",
            status=task_status,
            assigned_agent_id=worker_id,
            blocked_by_task_id=blocked_by,
        )
        s.add(task)
        await s.commit()
        for o in (board, worker, task):
            await s.refresh(o)

    return {"board": board, "worker": worker, "task": task, "token": raw_token}


def _start_user_patch_mocks():
    """Mocks fuer alle externen Side-Effects beim User-PATCH starten.
    Returns Liste von Patcher-Objekten — Caller muss am Ende stop() aufrufen.
    """
    mocks = [
        patch("app.routers.tasks.create_tracked_task"),
        patch("app.services.task_lifecycle.create_tracked_task", create=True),
        patch("app.services.auto_memory.create_tracked_task", create=True),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
    ]
    for m in mocks:
        m.start()
    return mocks


def _stop_mocks(mocks):
    for m in mocks:
        m.stop()


@pytest.mark.asyncio
async def test_user_patch_to_failed_auto_unassigns(auth_client):
    """User PATCH status: failed → assigned_agent_id wird geloescht.

    Reproduziert den Live-Bug: ohne Auto-Unassign wuerde der Agent in
    Cancel-Schleife haengen weil agent_poll den failed Task immer
    priorisiert.
    """
    data = await _setup_basic(task_status="in_progress")

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "failed"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        worker = await s.get(Agent, data["worker"].id)

    assert task.status == "failed"
    assert task.assigned_agent_id is None, (
        "BUG: failed Task ohne unassign → Cancel-Schleife im agent_poll"
    )
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_user_patch_to_blocked_preserves_assignment(auth_client):
    """User PATCH status: blocked (ohne blocked_by_task_id) → assignment bleibt (PR #111).

    Frueher (vor 2026-04-24) wurde assigned_agent_id geloescht. Das fuehrte zu
    Worker-Orphaning bei mc blocked: nach der Operator-Resolution landete der Task
    beim Board-Lead statt zurueck beim Worker. Jetzt bleibt assigned_agent_id
    erhalten; nur Lock (current_task_id) wird freigegeben.
    """
    data = await _setup_basic(task_status="in_progress")

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "blocked"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        worker = await s.get(Agent, data["worker"].id)

    assert task.status == "blocked"
    # Post-PR-#111: assignment bleibt, Worker bekommt Task beim Resume zurueck
    assert task.assigned_agent_id == data["worker"].id
    # Lock wird trotzdem freigegeben damit Worker andere Tasks nehmen kann
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_user_patch_to_blocked_with_callback_keeps_assignment(auth_client):
    """User PATCH status: blocked MIT blocked_by_task_id → assigned bleibt.

    Edge-Case: Der Operator setzt manuell einen Task auf blocked der schon einen
    Subtask-Callback hat (z.B. nach delegate). Das ist struktureller
    Wartezustand, KEIN Operator-Approval — nicht unassignen.
    """
    sub_id = uuid.uuid4()
    data = await _setup_basic(task_status="in_progress", blocked_by=sub_id)

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "blocked"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)

    assert task.status == "blocked"
    assert task.assigned_agent_id == data["worker"].id, (
        "Callback-Wait darf assignment nicht loeschen"
    )


@pytest.mark.asyncio
async def test_worker_patch_to_failed_auto_unassigns(client):
    """Worker PATCH status: failed (eigener Task) → assigned wird geloescht."""
    data = await _setup_basic(task_status="in_progress")

    mocks = _start_user_patch_mocks()
    try:
        with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
            with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
                mock_rpc.connected = False
                mock_rpc.chat_send = AsyncMock()

                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "failed"},
                    headers={"Authorization": f"Bearer {data['token']}"},
                )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        worker = await s.get(Agent, data["worker"].id)

    assert task.status == "failed"
    assert task.assigned_agent_id is None
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_worker_patch_to_blocked_with_callback_keeps_assignment(client):
    """Worker PATCH blocked + blocked_by_task_id (delegate-Pattern) → assigned bleibt."""
    sub_id = uuid.uuid4()
    data = await _setup_basic(task_status="in_progress")

    # Subtask anlegen damit blocked_by_task_id-Validierung passt
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = Task(
            id=sub_id,
            board_id=data["board"].id,
            parent_task_id=data["task"].id,
            title="Sub",
            status="inbox",
            callback_agent_id=data["worker"].id,
        )
        s.add(sub)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False
            mock_rpc.chat_send = AsyncMock()

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={
                    "status": "blocked",
                    "blocked_by_task_id": str(sub_id),
                },
                headers={"Authorization": f"Bearer {data['token']}"},
            )

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)

    assert task.status == "blocked"
    assert task.assigned_agent_id == data["worker"].id, (
        "Callback-Wait (delegate-Pattern) darf assignment nicht loeschen"
    )
    assert task.blocked_by_task_id == sub_id


@pytest.mark.asyncio
async def test_help_request_self_block_keeps_assignment(client):
    """help_request Endpoint setzt blocked_by_task_id → Original-Agent bleibt assigned."""
    board_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    helper_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="HR Board", slug=f"hr-{uuid.uuid4().hex[:6]}")
        s.add(board)
        requester = Agent(
            id=requester_id, name="Coder", role="developer", board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "tasks:help"],
            current_task_id=task_id,
            provision_status="provisioned",
        )
        helper = Agent(
            id=helper_id, name="Helper", role="developer", board_id=board_id,
            provision_status="provisioned",
        )
        s.add(requester)
        s.add(helper)
        task = Task(
            id=task_id, board_id=board_id, title="Coder Task",
            status="in_progress", assigned_agent_id=requester_id,
        )
        s.add(task)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/help-request",
            json={
                "title": "Need help with X",
                "context": "Stuck on Y",
                "needed_role": "developer",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)

    assert task.status == "blocked"
    assert task.assigned_agent_id == requester_id, (
        "help_request: Original-Agent muss assigned bleiben (blocked_by_task_id Callback)"
    )
    assert task.blocked_by_task_id is not None


@pytest.mark.asyncio
async def test_user_patch_failed_does_not_break_already_unassigned(auth_client):
    """Defensive: Watchdog hat Task schon unassignt → User-PATCH crasht nicht."""
    board_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="DB", slug=f"db-{uuid.uuid4().hex[:6]}")
        s.add(board)
        task = Task(
            id=task_id, board_id=board_id, title="Stale",
            status="in_progress", assigned_agent_id=None,  # bereits unassignt
        )
        s.add(task)
        await s.commit()

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{board_id}/tasks/{task_id}",
            json={"status": "failed"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)

    assert task.status == "failed"
    assert task.assigned_agent_id is None
