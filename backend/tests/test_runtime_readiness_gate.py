"""Tests for the runtime-readiness dispatch gate (power-managed runtimes).

Two layers:
 1. runtime_ready_for_agent() unit logic — only power_managed-bound agents are
    ever gated; everything else passes (fail-open).
 2. agent_poll() integration — a fresh inbox task is HELD (state=idle,
    runtime_not_ready) while a power-managed backend is asleep, and the 24/7
    fleet (non-power-managed agents) is completely unaffected.
"""
import uuid
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services import runtime_readiness
from app.services.runtime_readiness import runtime_ready_for_agent
from tests.conftest import test_engine


async def _mk_runtime(session, *, power_managed: bool, slug: str) -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=f"RT {slug}",
        runtime_type="flask_wol" if power_managed else "vllm_docker",
        endpoint="http://192.0.2.20:8000/v1",
        power_managed=power_managed,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


# ── Unit: runtime_ready_for_agent ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_allows_when_no_runtime_id(async_session):
    agent = SimpleNamespace(runtime_id=None)
    ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_gate_allows_non_power_managed(async_session):
    rt = await _mk_runtime(async_session, power_managed=False, slug="dgx-vllm")
    agent = SimpleNamespace(runtime_id=rt.id)
    # is_runtime_ready must NOT even be consulted for a non-power-managed runtime
    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(side_effect=AssertionError("should not probe"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_gate_blocks_power_managed_not_ready(async_session):
    rt = await _mk_runtime(async_session, power_managed=True, slug="wol-a")
    agent = SimpleNamespace(runtime_id=rt.id)
    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(return_value=False)):
        ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is False
    assert "schläft" in reason.lower()


@pytest.mark.asyncio
async def test_gate_allows_power_managed_ready(async_session):
    rt = await _mk_runtime(async_session, power_managed=True, slug="wol-b")
    agent = SimpleNamespace(runtime_id=rt.id)
    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(return_value=True)):
        ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_gate_killswitch_off(async_session):
    rt = await _mk_runtime(async_session, power_managed=True, slug="wol-c")
    agent = SimpleNamespace(runtime_id=rt.id)
    with patch.object(runtime_readiness.settings, "enable_runtime_readiness_gate", False), \
         patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(side_effect=AssertionError("flag off → no probe"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_gate_fails_open_on_error(async_session):
    rt = await _mk_runtime(async_session, power_managed=True, slug="wol-d")
    agent = SimpleNamespace(runtime_id=rt.id)
    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(side_effect=RuntimeError("boom"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is True  # fail-open: a gate bug must never stall the fleet


# ── Integration: agent_poll gating ───────────────────────────────────────────

async def _setup_worker_with_runtime(*, power_managed: bool, slug: str):
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = await _mk_runtime(s, power_managed=power_managed, slug=slug)
        s.add(Board(id=board_id, name="RTGate", slug=f"rtgate-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=worker_id, name="RTGateWorker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
            runtime_id=rt.id,
        ))
        await s.commit()
    return board_id, worker_id, token_raw


@pytest.mark.asyncio
async def test_poll_holds_task_when_power_managed_asleep(client, fake_redis):
    """the wake box asleep → inbox task is held (idle/runtime_not_ready), stays inbox."""
    from app.models.task import Task

    board_id, worker_id, token = await _setup_worker_with_runtime(
        power_managed=True, slug="wol-poll-1"
    )
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=task_id, board_id=board_id, title="Needs the wake box",
                   status="inbox", assigned_agent_id=worker_id))
        await s.commit()

    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(return_value=False)):
        resp = await client.get("/api/v1/agent/me/poll",
                                headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    assert body.get("runtime_not_ready") is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t.status == "inbox"
        assert t.ack_at is None
        assert t.dispatched_at is None  # never delivered while asleep


@pytest.mark.asyncio
async def test_poll_delivers_task_when_power_managed_ready(client, fake_redis):
    """the wake box ready → the held task is delivered on the next poll."""
    from app.models.task import Task

    board_id, worker_id, token = await _setup_worker_with_runtime(
        power_managed=True, slug="wol-poll-2"
    )
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=task_id, board_id=board_id, title="Run on the wake box",
                   status="inbox", assigned_agent_id=worker_id))
        await s.commit()

    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(return_value=True)):
        resp = await client.get("/api/v1/agent/me/poll",
                                headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task"
    assert body["task"]["id"] == str(task_id)


@pytest.mark.asyncio
async def test_poll_unaffected_for_non_power_managed_fleet(client, fake_redis):
    """REGRESSION GUARD: a normal (non-power-managed) agent claims its task as
    before — the gate must never touch the 24/7 fleet. is_runtime_ready is not
    even consulted."""
    from app.models.task import Task

    board_id, worker_id, token = await _setup_worker_with_runtime(
        power_managed=False, slug="dgx-poll-1"
    )
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=task_id, board_id=board_id, title="Normal task",
                   status="inbox", assigned_agent_id=worker_id))
        await s.commit()

    with patch.object(runtime_readiness, "is_runtime_ready", new=AsyncMock(side_effect=AssertionError("fleet must not be probed"))):
        resp = await client.get("/api/v1/agent/me/poll",
                                headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task"
    assert body["task"]["id"] == str(task_id)


# ── Security: runtime CRUD writes are admin-only (review finding B — RCE surface)─

async def _user_token(role: str) -> str:
    """Create a user of the given role and return a JWT."""
    from app.models.user import User
    from app.auth import create_access_token
    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"{role}-{uid.hex[:8]}@mc.local", name=role.title(),
                   role=role, is_active=True))
        await s.commit()
    return create_access_token(str(uid), role)


_RT_BODY = {
    "slug": "evil-rt", "display_name": "Evil", "runtime_type": "flask_wol",
    "endpoint": "http://192.0.2.20:8000/v1",
    "launch_command": "Start-Process calc.exe",  # would be RCE if a viewer could set it
    "control_url": "http://192.0.2.20:5555", "power_managed": True,
}


@pytest.mark.asyncio
async def test_runtime_db_create_forbidden_for_viewer(client, fake_redis):
    """A viewer must NOT be able to set launch_command/control_url (→ PowerShell
    RCE on the wake box). Runtime writes are admin-only now."""
    token = await _user_token("viewer")
    resp = await client.post("/api/v1/runtimes/db",
                             headers={"Authorization": f"Bearer {token}"}, json=_RT_BODY)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_runtime_db_create_allowed_for_admin(client, fake_redis):
    token = await _user_token("admin")
    resp = await client.post("/api/v1/runtimes/db",
                             headers={"Authorization": f"Bearer {token}"}, json=_RT_BODY)
    assert resp.status_code in (200, 201)


@pytest.mark.asyncio
async def test_runtime_db_create_rejects_bad_control_url(client, fake_redis):
    token = await _user_token("admin")
    bad = {**_RT_BODY, "slug": "bad-url-rt", "control_url": "ftp://192.0.2.20:5555"}
    resp = await client.post("/api/v1/runtimes/db",
                             headers={"Authorization": f"Bearer {token}"}, json=bad)
    assert resp.status_code == 422


# ── Local runtimes (ADR-096, 19.10.2026) ─────────────────────────────────────
# Local AI is the main part of MC: a task for an agent whose own box is off
# must wait in the inbox — not be delivered into a session that can never
# answer (live: Spark :8000 off, task 4de17c6a went "in progress" 4 s after
# the claim and sat there 20 minutes). Only the poll claim is gated
# (include_local=True) — push sites (delegation, manual re-dispatch) keep
# working so a lead can still queue work while the model is down.

async def _mk_local_runtime(session, *, slug: str, host: bool = True) -> Runtime:
    from app.models.host import Host

    host_id = None
    if host:
        h = Host(slug=f"box-{uuid.uuid4().hex[:6]}", display_name="Box", kind="ssh")
        session.add(h)
        await session.flush()
        host_id = h.id
    rt = Runtime(
        slug=slug,
        display_name=f"RT {slug}",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.30:8000/v1",
        host_id=host_id,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


@pytest.mark.asyncio
async def test_local_runtime_down_holds_the_poll_claim(async_session):
    rt = await _mk_local_runtime(async_session, slug="local-down")
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=False)):
        ok, reason = await runtime_ready_for_agent(agent, async_session, include_local=True)
    assert ok is False
    assert "antwortet nicht" in reason


@pytest.mark.asyncio
async def test_local_runtime_up_passes(async_session):
    rt = await _mk_local_runtime(async_session, slug="local-up")
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=True)):
        ok, reason = await runtime_ready_for_agent(agent, async_session, include_local=True)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_push_sites_are_not_gated_for_local_runtimes(async_session):
    """Without include_local (check_dispatch_allowed) nothing is probed."""
    rt = await _mk_local_runtime(async_session, slug="local-push")
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_local_runtime_serving",
                      new=AsyncMock(side_effect=AssertionError("push must not probe"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_cloud_runtime_is_never_probed(async_session):
    """No host binding = not local (heads.pairs.is_local) — cloud stays untouched."""
    rt = await _mk_local_runtime(async_session, slug="cloud-like", host=False)
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_local_runtime_serving",
                      new=AsyncMock(side_effect=AssertionError("cloud must not probe"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session, include_local=True)
    assert ok is True and reason is None


@pytest.mark.asyncio
async def test_local_gate_killswitch(async_session):
    rt = await _mk_local_runtime(async_session, slug="local-kill")
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness.settings, "enable_local_runtime_readiness_gate", False), \
         patch.object(runtime_readiness, "is_local_runtime_serving",
                      new=AsyncMock(side_effect=AssertionError("switch off → no probe"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session, include_local=True)
    assert ok is True


@pytest.mark.asyncio
async def test_local_gate_fails_open(async_session):
    rt = await _mk_local_runtime(async_session, slug="local-boom")
    agent = SimpleNamespace(runtime_id=rt.id, id=uuid.uuid4(), name="W")
    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(side_effect=RuntimeError("boom"))):
        ok, reason = await runtime_ready_for_agent(agent, async_session, include_local=True)
    assert ok is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(200, True), (401, True), (404, True), (503, False)])
async def test_serving_probe_reads_any_answer_below_500_as_up(status, expected):
    """The question is 'is the model server there', not 'is the path right':
    auth or a wrong health path must never stall a running box (fail-open);
    5xx (engine warming/crashed) holds."""
    import httpx

    rt = Runtime(slug=f"p-{status}", display_name="P", runtime_type="openai_compatible",
                 endpoint="http://192.0.2.31:8000/v1")
    transport = httpx.MockTransport(lambda req: httpx.Response(status))
    with patch.object(runtime_readiness, "_http_client",
                      new=lambda: httpx.AsyncClient(transport=transport)):
        assert await runtime_readiness._probe_serving(rt) is expected


@pytest.mark.asyncio
async def test_serving_probe_connect_error_is_down():
    import httpx

    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    rt = Runtime(slug="p-refused", display_name="P", runtime_type="openai_compatible",
                 endpoint="http://192.0.2.32:8000/v1")
    with patch.object(runtime_readiness, "_http_client",
                      new=lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse))):
        assert await runtime_readiness._probe_serving(rt) is False


async def _setup_worker_with_local_runtime(slug: str):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = await _mk_local_runtime(s, slug=slug)
        s.add(Board(id=board_id, name="LocalGate", slug=f"lg-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=worker_id, name="LocalGateWorker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
            runtime_id=rt.id,
        ))
        await s.commit()
    return board_id, worker_id, token_raw


@pytest.mark.asyncio
async def test_poll_holds_task_while_local_runtime_is_down(client, fake_redis):
    """Sabotage scenario from the operator: model off, task assigned → the
    poll never delivers it (stays inbox, dispatched_at empty) and the card's
    timeline says why."""
    from sqlmodel import select

    from app.models.activity import ActivityEvent
    from app.models.task import Task

    board_id, worker_id, token = await _setup_worker_with_local_runtime("local-poll-down")
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=task_id, board_id=board_id, title="Needs the local model",
                   status="inbox", assigned_agent_id=worker_id))
        await s.commit()

    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=False)):
        resp = await client.get("/api/v1/agent/me/poll",
                                headers={"Authorization": f"Bearer {token}"})
        again = await client.get("/api/v1/agent/me/poll",
                                 headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle", body
    assert body.get("runtime_not_ready") is True
    assert again.json()["state"] == "idle"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t.status == "inbox"
        assert t.dispatched_at is None and t.ack_at is None
        events = (await s.exec(
            select(ActivityEvent).where(ActivityEvent.task_id == task_id)
        )).all()
    held = [e for e in events if e.event_type == "task.dispatch_held_runtime_down"]
    assert len(held) == 1, [e.event_type for e in events]  # debounced, not per poll
    assert "antwortet nicht" in held[0].title


@pytest.mark.asyncio
async def test_poll_delivers_once_the_local_runtime_answers(client, fake_redis):
    from app.models.task import Task

    board_id, worker_id, token = await _setup_worker_with_local_runtime("local-poll-up")
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=task_id, board_id=board_id, title="Model is up",
                   status="inbox", assigned_agent_id=worker_id))
        await s.commit()

    with patch.object(runtime_readiness, "is_local_runtime_serving", new=AsyncMock(return_value=True)):
        resp = await client.get("/api/v1/agent/me/poll",
                                headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["state"] == "new_task", resp.json()
