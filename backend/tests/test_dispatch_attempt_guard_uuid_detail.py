"""Regression: the rejection paths must answer 409, not 500, when the PATCH
body carries a UUID field.

Bug (reproduced live 2026-09-13): the guard builds its audit detail with
`payload.model_dump(exclude_none=True)`. AgentTaskUpdate declares four
`uuid.UUID` fields (project_id, assigned_agent_id, blocked_by_task_id), so
that dict holds raw UUID objects. `emit_event` writes the detail into a JSON
column (and publishes the same dict to Redis), the serializer raises
`TypeError: Object of type UUID is not JSON serializable` — and because
emit_event runs BEFORE `raise HTTPException(409)`, the caller never sees the
409. Log evidence: "Stale update REJECTED: ... expected=408c90bc..."
immediately followed by 500 Internal Server Error.

Covered rejection paths (all of them build a detail from the payload):
- stale_value, Phase B enforce   → emit_event + 409   (agent_task_status.py:1607)
- Phase A warning (enforce off)  → emit_event, request continues (:1655)
- run_control stopped/manual_hold → emit_event + 409  (:1584)
- missing_header, Phase B        → 409, deliberately WITHOUT emit_event
  (2026-05-18 decision: self-healing, no Discord noise). Pinned anyway so a
  future re-introduction of the event lands on a JSON-safe detail.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_dispatched_task(run_control: str | None = None) -> dict:
    """Board + worker + task with dispatch_attempt_id set."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    attempt_id = str(uuid.uuid4())
    token_raw, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="UUID Detail Board", slug=f"ud-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id, name="Worker", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
            current_task_id=task_id,
            emoji="🔍",
        ))
        task = Task(
            id=task_id, board_id=board_id, title="Briefing", status="in_progress",
            assigned_agent_id=agent_id,
            dispatch_attempt_id=attempt_id,
        )
        if run_control:
            task.run_control = run_control
        s.add(task)
        await s.commit()

    return {
        "board_id": board_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "token": token_raw,
    }


async def _events(event_type: str, task_id: uuid.UUID) -> list:
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.exec(
            select(ActivityEvent)
            .where(ActivityEvent.event_type == event_type)
            .where(ActivityEvent.task_id == task_id)
        )).all()


def _assert_detail_json_safe(event) -> None:
    """The stored detail must survive json.dumps without a custom encoder —
    that is exactly what the JSON column and the Redis publish do."""
    json.dumps(event.detail)


# ── 1. stale_value + UUID field → 409, not 500 ────────────────────────

@pytest.mark.asyncio
async def test_stale_value_with_uuid_field_returns_409(client, fake_redis):
    data = await _setup_dispatched_task()

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"status": "done", "assigned_agent_id": str(uuid.uuid4())},
        headers={
            "Authorization": f"Bearer {data['token']}",
            "X-Dispatch-Attempt-Id": str(uuid.uuid4()),
        },
    )

    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    assert "Stale" in r.json()["detail"] or "veraltet" in r.json()["detail"]

    rows = await _events("task.stale_update_rejected", data["task_id"])
    assert len(rows) == 1
    _assert_detail_json_safe(rows[0])
    # The audit value survives the round trip — a plain string, not repr()
    assert isinstance(rows[0].detail["attempted"]["assigned_agent_id"], str)


# ── 2. missing_header + UUID field → 409 (no event by design) ──────────

@pytest.mark.asyncio
async def test_missing_header_with_uuid_field_returns_409(client, fake_redis):
    data = await _setup_dispatched_task()

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"status": "done", "blocked_by_task_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {data['token']}"},
    )

    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    assert "mc done" in r.json()["detail"]
    assert await _events("task.missing_dispatch_attempt_id", data["task_id"]) == []


# ── 3. Phase A warning + UUID field → event, request not killed ───────

@pytest.mark.asyncio
async def test_phase_a_warning_with_uuid_field_does_not_500(client, fake_redis):
    """enforce_dispatch_attempt_id=False: the guard only warns, then lets the
    request through. The warning event carries the same detail dict."""
    data = await _setup_dispatched_task()

    import app.config

    with patch.object(app.config.settings, "enforce_dispatch_attempt_id", False):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"priority": "high", "blocked_by_task_id": str(uuid.uuid4())},
            headers={
                "Authorization": f"Bearer {data['token']}",
                "X-Dispatch-Attempt-Id": str(uuid.uuid4()),
            },
        )

    assert r.status_code < 500, f"guard must not 500: {r.status_code} {r.text}"

    rows = await _events("task.stale_update_warning", data["task_id"])
    assert len(rows) == 1
    _assert_detail_json_safe(rows[0])
    assert isinstance(rows[0].detail["attempted"]["blocked_by_task_id"], str)


# ── 4. stopped task + UUID field → 409, not 500 ───────────────────────

@pytest.mark.asyncio
async def test_stopped_task_with_uuid_field_returns_409(client, fake_redis):
    """Same defect, own branch: the late-update rejection for stopped/held
    tasks builds its detail from the payload too."""
    data = await _setup_dispatched_task(run_control="stopped")

    r = await client.patch(
        f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
        json={"status": "done", "project_id": str(uuid.uuid4())},
        headers={
            "Authorization": f"Bearer {data['token']}",
            "X-Dispatch-Attempt-Id": data["attempt_id"],
        },
    )

    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    assert "run_control" in r.json()["detail"]

    rows = await _events("task.late_update_rejected", data["task_id"])
    assert len(rows) == 1
    _assert_detail_json_safe(rows[0])
    assert isinstance(rows[0].detail["attempted"]["project_id"], str)
