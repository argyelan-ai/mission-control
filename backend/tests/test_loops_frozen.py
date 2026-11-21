"""Etappe 2 "clean up" (ADR-102 §3): loops are frozen.

Page counter 28.09.–19.11.: /loops 4 views, 0 in the last 14 days; the only
loop's last round was 07.07. Nothing is deleted: existing loops stay readable
and a running one can still be paused or stopped (the safe way out), but
create, start, edit and delete are refused with a clear 410 "frozen".
The freeze sits in the shared service (`loop_runner.FROZEN`), so every
caller is covered: the HTTP router, the scheduler's `start_loop` job and a
loop_gate approval (UI resolve and Telegram quick-resolve).
Way back: `loop_runner.FROZEN = False`.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.models.scheduled_job import ScheduledJob
from app.services import loop_runner
from tests.conftest import test_engine
from tests.test_loops_l1 import _get_loop, _mk_board, _mk_loop


def _payload(board_id) -> dict:
    return {
        "board_id": str(board_id), "name": "New grind", "goal": "g",
        "backlog_source": "markdown", "backlog_md": "- one item",
    }


@pytest.mark.asyncio
async def test_create_is_refused_with_410_frozen(auth_client: AsyncClient):
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json=_payload(board.id))
    assert r.status_code == 410, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "frozen"
    assert "frozen" in detail["message"].lower()

    # nothing was written
    listed = await auth_client.get("/api/v1/loops", params={"board_id": str(board.id)})
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_existing_loops_stay_readable_pausable_and_stoppable(auth_client: AsyncClient):
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused")

    listed = await auth_client.get("/api/v1/loops", params={"board_id": str(board.id)})
    assert [row["id"] for row in listed.json()] == [str(loop.id)]
    detail = await auth_client.get(f"/api/v1/loops/{loop.id}")
    assert detail.status_code == 200
    stopped = await auth_client.post(f"/api/v1/loops/{loop.id}/stop")
    assert stopped.status_code == 200, stopped.text

    running = await _mk_loop(board, status="running", name="Still running")
    paused = await auth_client.post(f"/api/v1/loops/{running.id}/pause")
    assert paused.status_code == 200, paused.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix,body", [
    ("post", "/start", None),
    ("patch", "", {"name": "renamed"}),
    ("delete", "", None),
])
async def test_start_edit_delete_are_refused_and_data_stays(auth_client: AsyncClient, method, suffix, body):
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused", name="Kept")
    kwargs = {"json": body} if body is not None else {}
    r = await getattr(auth_client, method)(f"/api/v1/loops/{loop.id}{suffix}", **kwargs)
    assert r.status_code == 410, r.text
    assert r.json()["detail"]["code"] == "frozen"

    after = await auth_client.get(f"/api/v1/loops/{loop.id}")
    assert after.status_code == 200
    assert after.json()["name"] == "Kept"
    assert after.json()["status"] == "paused"


@pytest.mark.asyncio
async def test_switch_off_frozen_restores_create(auth_client: AsyncClient, monkeypatch):
    monkeypatch.setattr(loop_runner, "FROZEN", False)
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json=_payload(board.id))
    assert r.status_code == 201, r.text


# ── Every other way to (re)start a loop is closed too ─────────────────

@pytest.mark.asyncio
async def test_scheduled_start_loop_job_does_not_start_a_frozen_loop():
    from app.services.scheduler import SchedulerService

    board = await _mk_board()
    loop = await _mk_loop(board, status="draft")
    job = ScheduledJob(
        id=uuid.uuid4(), name="Nightly loop kickoff",
        schedule_type="daily", schedule_time="03:00",
        action_type="start_loop", task_payload={"loop_id": str(loop.id)},
    )
    svc = SchedulerService.__new__(SchedulerService)
    with patch("app.services.loop_runner.emit_event", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            success, error, _detail = await svc._do_start_loop(s, job)

    assert success is False
    assert "frozen" in (error or "").lower()
    assert (await _get_loop(loop.id)).status == "draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("start_status", ["paused", "waiting_gate"])
async def test_gate_approval_does_not_revive_a_frozen_loop(auth_client: AsyncClient, fake_redis, start_status):
    board = await _mk_board()
    loop = await _mk_loop(board, status=start_status, consecutive_failed_rounds=2)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=board.id, action_type="loop_gate",
            description="x", payload={"loop_id": str(loop.id), "reason": "circuit_breaker"},
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    r = await auth_client.patch(f"/api/v1/approvals/{approval.id}", json={"status": "approved"})
    assert r.status_code == 200, r.text
    fresh = await _get_loop(loop.id)
    assert fresh.status == "paused"
    assert fresh.consecutive_failed_rounds == 2


@pytest.mark.asyncio
async def test_new_start_loop_schedule_job_is_refused(auth_client: AsyncClient):
    with patch("app.routers.schedule.scheduler") as mock_svc:
        mock_svc.add_job = AsyncMock()
        r = await auth_client.post("/api/v1/schedule/jobs", json={
            "name": "Nightly loop kickoff", "schedule_type": "daily",
            "schedule_time": "03:00", "action_type": "start_loop",
            "task_payload": {"loop_id": str(uuid.uuid4())},
        })
    assert r.status_code == 410, r.text
    assert r.json()["detail"]["code"] == "frozen"
    mock_svc.add_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_job_cannot_be_switched_to_start_loop(auth_client: AsyncClient):
    with patch("app.routers.schedule.scheduler") as mock_svc:
        mock_svc.add_job = AsyncMock()
        mock_svc.update_job = AsyncMock()
        created = await auth_client.post("/api/v1/schedule/jobs", json={
            "name": "Briefing", "schedule_type": "daily", "schedule_time": "09:00",
            "action_type": "chat_send", "agent_name": "lead", "message": "hi",
        })
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]
        r = await auth_client.patch(f"/api/v1/schedule/jobs/{job_id}", json={"action_type": "start_loop"})
        assert r.status_code == 410, r.text
        assert r.json()["detail"]["code"] == "frozen"
        mock_svc.update_job.assert_not_awaited()
    after = await auth_client.get(f"/api/v1/schedule/jobs/{job_id}")
    assert after.json()["action_type"] == "chat_send"
