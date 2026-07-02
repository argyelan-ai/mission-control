"""Tests: `?include_subtasks=true` Query-Param fuer Deliverable-LIST Endpoints.

Use-Case: Orchestrator-Parent-Tasks (z.B. Boss DNA+Skill) zeigen im UI alle
Deliverables der Subtasks mit — damit Operator/Reviewer alle Endprodukte des
Task-Trees auf einen Blick sieht ohne jeden Subtask einzeln zu oeffnen.
"""

import uuid
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_tree():
    """Baut eine 3-Ebenen-Hierarchie:
    root
      ├─ child_a (with deliverable D_A)
      │    └─ grand_a (with deliverable D_GA)
      ├─ child_b (with deliverable D_B)
      └─ child_c (KEIN deliverable)
    plus: root hat eigenes deliverable D_ROOT.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.deliverable import TaskDeliverable
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Tree", slug=f"tr-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Worker", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        root = Task(id=uuid.uuid4(), board_id=board_id, title="Root", status="in_progress",
                    assigned_agent_id=agent_id, owner_agent_id=agent_id)
        child_a = Task(id=uuid.uuid4(), board_id=board_id, title="Child A", status="done",
                       parent_task_id=root.id, assigned_agent_id=agent_id, owner_agent_id=agent_id)
        child_b = Task(id=uuid.uuid4(), board_id=board_id, title="Child B", status="done",
                       parent_task_id=root.id, assigned_agent_id=agent_id, owner_agent_id=agent_id)
        child_c = Task(id=uuid.uuid4(), board_id=board_id, title="Child C (empty)", status="done",
                       parent_task_id=root.id, assigned_agent_id=agent_id, owner_agent_id=agent_id)
        grand_a = Task(id=uuid.uuid4(), board_id=board_id, title="Grandchild A", status="done",
                       parent_task_id=child_a.id, assigned_agent_id=agent_id, owner_agent_id=agent_id)
        for t in (root, child_a, child_b, child_c, grand_a):
            s.add(t)
        # Deliverables
        s.add(TaskDeliverable(task_id=root.id, agent_id=agent_id, deliverable_type="document",
                              title="D_ROOT", content="root-content"))
        s.add(TaskDeliverable(task_id=child_a.id, agent_id=agent_id, deliverable_type="document",
                              title="D_A", content="a-content"))
        s.add(TaskDeliverable(task_id=child_b.id, agent_id=agent_id, deliverable_type="document",
                              title="D_B", content="b-content"))
        s.add(TaskDeliverable(task_id=grand_a.id, agent_id=agent_id, deliverable_type="document",
                              title="D_GA", content="ga-content"))
        await s.commit()
    return {
        "board_id": board_id, "token": token_raw,
        "root_id": root.id, "child_a_id": child_a.id, "child_b_id": child_b.id,
        "grand_a_id": grand_a.id,
    }


# ── Agent-scoped Endpoint (include_subtasks) ───────────────────────────────


@pytest.mark.asyncio
async def test_agent_list_without_include_subtasks_returns_only_self(client, fake_redis):
    """Default-Behavior: nur eigene Deliverables, keine Subtask-Entries."""
    ctx = await _setup_tree()
    resp = await client.get(
        f"/api/v1/agent/boards/{ctx['board_id']}/tasks/{ctx['root_id']}/deliverables",
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "D_ROOT"
    # Keine source_* Felder wenn include_subtasks=false (Backward-Compat)
    assert "source_task_id" not in data[0]


@pytest.mark.asyncio
async def test_agent_list_include_subtasks_default_depth_2(client, fake_redis):
    """Mit include_subtasks=true + default depth=2: root + direct + grand."""
    ctx = await _setup_tree()
    resp = await client.get(
        f"/api/v1/agent/boards/{ctx['board_id']}/tasks/{ctx['root_id']}/deliverables?include_subtasks=true",
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    titles = sorted(d["title"] for d in data)
    assert titles == ["D_A", "D_B", "D_GA", "D_ROOT"]
    # Check source_depth: root=0, child_a=1, grand_a=2
    depth_by_title = {d["title"]: d["source_depth"] for d in data}
    assert depth_by_title["D_ROOT"] == 0
    assert depth_by_title["D_A"] == 1
    assert depth_by_title["D_B"] == 1
    assert depth_by_title["D_GA"] == 2


@pytest.mark.asyncio
async def test_agent_list_include_subtasks_depth_1_skips_grandchildren(client, fake_redis):
    """depth=1 — nur direkte Kinder, Enkel werden ausgelassen."""
    ctx = await _setup_tree()
    resp = await client.get(
        f"/api/v1/agent/boards/{ctx['board_id']}/tasks/{ctx['root_id']}/deliverables?include_subtasks=true&depth=1",
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert resp.status_code == 200
    titles = sorted(d["title"] for d in resp.json())
    assert titles == ["D_A", "D_B", "D_ROOT"]  # D_GA fehlt (wäre depth=2)


@pytest.mark.asyncio
async def test_agent_list_include_subtasks_depth_clamped_to_max_5(client, fake_redis):
    """depth=999 wird auf Server-Max=5 geclampt."""
    ctx = await _setup_tree()
    resp = await client.get(
        f"/api/v1/agent/boards/{ctx['board_id']}/tasks/{ctx['root_id']}/deliverables?include_subtasks=true&depth=999",
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert resp.status_code == 200
    # Unsere Tree-Tiefe ist nur 2, also clamp passiert unsichtbar.
    # Aber: kein 422, kein Error.


@pytest.mark.asyncio
async def test_agent_list_include_subtasks_populates_source_task_title(client, fake_redis):
    """Jedes Subtask-Deliverable hat source_task_title fuer UI-Gruppierung."""
    ctx = await _setup_tree()
    resp = await client.get(
        f"/api/v1/agent/boards/{ctx['board_id']}/tasks/{ctx['root_id']}/deliverables?include_subtasks=true",
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    data = resp.json()
    title_by_deliv = {d["title"]: d["source_task_title"] for d in data}
    assert title_by_deliv["D_ROOT"] == "Root"
    assert title_by_deliv["D_A"] == "Child A"
    assert title_by_deliv["D_B"] == "Child B"
    assert title_by_deliv["D_GA"] == "Grandchild A"


# ── User-facing Endpoint (tasks.py) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_list_include_subtasks(auth_client, make_board, make_task, make_agent, session):
    """User-Endpoint (frontend) — gleiche Semantik."""
    from app.models.deliverable import TaskDeliverable
    board = await make_board()
    agent = await make_agent(board_id=board.id)
    root = await make_task(board.id, title="Root")
    child = await make_task(board.id, title="Child", parent_task_id=root.id)

    session.add(TaskDeliverable(
        task_id=root.id, agent_id=agent.id, deliverable_type="document",
        title="Root Doc", content="r",
    ))
    session.add(TaskDeliverable(
        task_id=child.id, agent_id=agent.id, deliverable_type="document",
        title="Child Doc", content="c",
    ))
    await session.commit()

    # Default: nur Root
    r1 = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{root.id}/deliverables"
    )
    assert r1.status_code == 200
    assert len(r1.json()) == 1
    assert r1.json()[0]["title"] == "Root Doc"

    # Mit include_subtasks
    r2 = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{root.id}/deliverables?include_subtasks=true"
    )
    assert r2.status_code == 200
    titles = sorted(d["title"] for d in r2.json())
    assert titles == ["Child Doc", "Root Doc"]
    # Source-Info present
    for d in r2.json():
        assert "source_task_title" in d
        assert "source_depth" in d
