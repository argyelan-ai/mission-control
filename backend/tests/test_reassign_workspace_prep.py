"""Tests: reassigning a card must prepare the NEW assignee's workspace.

Incident 2026-09-13: a Board Lead reassigned a review card from a host
agent (no `/workspace`) to a container agent via `mc reassign`. The
container agent's turn aborted immediately:

    RuntimeError: ACP-Workspace nicht vorbereitet:
      '/workspace/_tasks/df387ec6-...' existiert nicht

Root cause: `POST .../reassign` (agent_reassign_task) rotated the
dispatch_attempt_id and wrote the TaskAttemptAudit row, but never touched
`task.workspace_path` — the one thing that actually prepares a directory
the new agent can see. Two twin locations had the identical gap: the
generic `PATCH .../{task_id}` assigned_agent_id branch, and the
self-review-escalation-to-Board-Lead branch inside execute_review_decision.

All three now route through `task_context_builder.prepare_agent_workspace_
for_task` — the same git/worktree + Phase-C non-code sequence
`dispatch.auto_dispatch_task` already runs on every first dispatch.

Covers:
- POST .../reassign calls prepare_agent_workspace_for_task for the target
- PATCH .../{task_id} assigned_agent_id branch does the same
- execute_review_decision's self-review escalation does the same
- Host -> Container reassign actually sets task.workspace_path (real
  orchestration logic runs; only the git subprocess itself is stubbed,
  same convention as test_workspace_isolation.py)
- Container -> Host reassign is a genuine no-op: git_service is never
  touched at all, task.workspace_path is untouched
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.task import Task
from tests.conftest import test_engine


async def _setup_board_with_agents(
    session: AsyncSession,
    *,
    task_status: str = "review",
    old_workspace_path: str | None = None,
    old_agent_runtime: str = "host",
    new_workspace_path: str | None = "/tmp/mc-test-not-used",
    new_agent_runtime: str = "cli-bridge",
    project_id: uuid.UUID | None = None,
):
    board = Board(name="Workspace-Prep Board", slug=f"ws-prep-{uuid.uuid4().hex[:8]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    lead_raw, lead_hash = generate_agent_token()
    lead = Agent(
        name="Boss", role="lead", board_id=board.id, agent_token_hash=lead_hash,
        is_board_lead=True, scopes=["tasks:read", "tasks:write", "tasks:manage"],
    )
    session.add(lead)

    old_raw, old_hash = generate_agent_token()
    old_agent = Agent(
        name="HostRunner", role="developer", board_id=board.id, agent_token_hash=old_hash,
        is_board_lead=False, scopes=["tasks:read", "tasks:write"],
        agent_runtime=old_agent_runtime, workspace_path=old_workspace_path,
    )
    session.add(old_agent)

    new_raw, new_hash = generate_agent_token()
    new_agent = Agent(
        name="ContainerRunner", role="developer", board_id=board.id, agent_token_hash=new_hash,
        is_board_lead=False, scopes=["tasks:read", "tasks:write"],
        agent_runtime=new_agent_runtime, workspace_path=new_workspace_path,
    )
    session.add(new_agent)

    task = Task(
        board_id=board.id, assigned_agent_id=old_agent.id,
        title=f"Reassigned card {uuid.uuid4().hex[:6]}", status=task_status,
        project_id=project_id,
    )
    session.add(task)

    await session.commit()
    for obj in (lead, old_agent, new_agent, task):
        await session.refresh(obj)
    return board, lead, lead_raw, old_agent, old_raw, new_agent, new_raw, task


# ── Wiring: POST .../reassign ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reassign_endpoint_prepares_new_agents_workspace(
    client: AsyncClient, async_session,
):
    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(async_session)
    )

    with patch(
        "app.services.task_context_builder.prepare_agent_workspace_for_task",
        new=AsyncMock(return_value=True),
    ) as mock_prep:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
            json={"to": "ContainerRunner"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text
    mock_prep.assert_awaited_once()
    call_task, call_agent, _call_session = mock_prep.await_args.args
    assert call_task.id == task.id
    assert call_agent.id == new_agent.id


@pytest.mark.asyncio
async def test_reassign_endpoint_blocks_on_workspace_setup_failure(
    client: AsyncClient, async_session,
):
    """When workspace prep fails, task_context_builder already blocked the
    task (comment + status=blocked + unassign) — reassign must surface
    that, not silently report the reassignment as a clean success."""
    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(async_session)
    )

    async def _fail(task_obj, agent_obj, session):
        task_obj.status = "blocked"
        session.add(task_obj)
        await session.commit()
        return False

    with patch(
        "app.services.task_context_builder.prepare_agent_workspace_for_task",
        new=AsyncMock(side_effect=_fail),
    ):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
            json={"to": "ContainerRunner"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "blocked"


# ── Wiring: PATCH .../{task_id} assigned_agent_id ────────────────────────


@pytest.mark.asyncio
async def test_patch_assigned_agent_id_prepares_new_agents_workspace(
    client: AsyncClient, async_session,
):
    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(async_session, task_status="inbox")
    )

    with patch(
        "app.services.task_context_builder.prepare_agent_workspace_for_task",
        new=AsyncMock(return_value=True),
    ) as mock_prep:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"assigned_agent_id": str(new_agent.id)},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text
    mock_prep.assert_awaited_once()
    call_task, call_agent, _call_session = mock_prep.await_args.args
    assert call_task.id == task.id
    assert call_agent.id == new_agent.id


@pytest.mark.asyncio
async def test_patch_same_assignee_does_not_trigger_workspace_prep(
    client: AsyncClient, async_session,
):
    """PATCHing assigned_agent_id to the agent that already owns the task
    is a no-op (existing '_new_assignee != _old_assigned' guard) — must
    stay a no-op for workspace prep too, not reprovision on every no-op
    PATCH."""
    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(async_session, task_status="inbox")
    )

    with patch(
        "app.services.task_context_builder.prepare_agent_workspace_for_task",
        new=AsyncMock(return_value=True),
    ) as mock_prep:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"assigned_agent_id": str(old_agent.id)},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text
    mock_prep.assert_not_awaited()


# ── Wiring: self-review escalation to Board Lead ─────────────────────────


@pytest.mark.asyncio
async def test_self_review_escalation_prepares_board_leads_workspace(
    make_board, make_agent, make_task,
):
    from app.models.task import TaskEvent
    import datetime as dt

    board = await make_board(name="Escalation Workspace Board", slug=f"esc-ws-{uuid.uuid4().hex[:8]}")
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    henry = await make_agent(
        name="Henry", is_board_lead=True, board_id=board.id,
        agent_runtime="cli-bridge", workspace_path="/tmp/mc-test-not-used-henry",
    )
    task_obj = await make_task(
        board_id=board.id, title="Fact-Check", status="review", assigned_agent_id=rex.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            id=uuid.uuid4(), task_id=task_obj.id, from_status="inbox", to_status="in_progress",
            changed_by="agent", agent_id=rex.id, created_at=dt.datetime.utcnow(),
        ))
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
        patch(
            "app.services.task_context_builder.prepare_agent_workspace_for_task",
            new=AsyncMock(return_value=True),
        ) as mock_prep,
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = await s.get(Task, task_obj.id)
            rex_agent = await s.get(Agent, rex.id)

            from app.services.task_lifecycle import execute_review_decision
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Alles OK", actor_agent=rex_agent,
            )

    mock_prep.assert_awaited_once()
    call_task, call_agent, _call_session = mock_prep.await_args.args
    assert call_task.id == task_obj.id
    assert call_agent.id == henry.id


# ── Behavioral: Host -> Container actually sets workspace_path ──────────


@pytest.mark.asyncio
async def test_reassign_host_to_container_sets_workspace_path_for_real(
    client: AsyncClient, async_session, tmp_path,
):
    """No mocking of the orchestration layer — only the git subprocess
    itself is stubbed (same convention as test_workspace_isolation.py).
    Proves the real prepare_agent_workspace_for_task -> setup_git_
    workspace_for_dispatch -> git_service chain actually runs and sets
    task.workspace_path to a real, new-agent-scoped path — not just that
    *some* function got called."""
    project = Project(
        board_id=uuid.uuid4(),  # overwritten once board exists
        name="WS Prep Project",
        github_repo_url="https://example.invalid/argyelan-ai/ws-prep-project.git",
    )

    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(
            async_session,
            old_workspace_path=None, old_agent_runtime="host",
            new_workspace_path=str(tmp_path / "container-runner"), new_agent_runtime="cli-bridge",
        )
    )
    project.board_id = board.id
    async_session.add(project)
    await async_session.commit()
    await async_session.refresh(project)
    task.project_id = project.id
    async_session.add(task)
    await async_session.commit()

    from app.services.git_service import git_service

    async def _fake_run_cmd(*args, **kwargs):
        return ""

    with patch.object(git_service, "_run_cmd", new=AsyncMock(side_effect=_fake_run_cmd)) as mock_cmd:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
            json={"to": "ContainerRunner"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert mock_cmd.await_count >= 1, "git_service never ran — no real workspace prep happened"

    body = resp.json()
    new_workspace = body["workspace_path"]
    assert new_workspace, "task.workspace_path is still empty after reassign"
    assert str(tmp_path / "container-runner") in new_workspace, (
        f"workspace_path {new_workspace!r} is not scoped under the NEW agent's "
        f"workspace ({tmp_path / 'container-runner'!r}) — looks like it kept a stale/foreign path"
    )


# ── Behavioral: Container -> Host is a genuine no-op ─────────────────────


@pytest.mark.asyncio
async def test_reassign_container_to_host_touches_no_git(
    client: AsyncClient, async_session,
):
    """Reverse direction of the incident: reassigning TO a host agent
    (no workspace_path) must not invoke git at all and must not fabricate
    a directory just to satisfy the ACP guard — it's a real no-op, proven
    by making git_service._run_cmd raise if it's ever called."""
    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(
            async_session,
            old_workspace_path="/tmp/mc-test-not-used-old", old_agent_runtime="cli-bridge",
            new_workspace_path=None, new_agent_runtime="host",
        )
    )

    from app.services.git_service import git_service

    async def _explode(*args, **kwargs):
        raise AssertionError(f"git_service._run_cmd must not run for a workspace_path-less agent: {args}")

    with patch.object(git_service, "_run_cmd", new=AsyncMock(side_effect=_explode)):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
            json={"to": "ContainerRunner"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_agent_id"] == str(new_agent.id)
