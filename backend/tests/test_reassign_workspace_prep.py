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

PR #568 review (Rex, SHA a31745f4) follow-up — B1/B2:

- B1: the first fix only reset `task.workspace_path` when it was UNSET
  (`if not task.workspace_path`). On a reassign the field is usually SET
  — to the OLD agent's layout — so the guard never fired for exactly the
  task class the 2026-09-13 incident was about (a project task without a
  `github_repo_url`, i.e. no git worktree step to overwrite it first).
  `_needs_non_code_workspace()` now also treats a foreign path (one that
  doesn't live under the *new* agent's own workspace tree) as needing
  (re)provisioning. `test_reassign_prepares_workspace_when_field_is_stale`
  below is Rex's incident-shaped probe, seeded at the one starting state
  none of the original 7 tests used: task.workspace_path already set (W1).
- B2: `dispatch.auto_dispatch_task` had its own inline copy of the same
  two steps instead of calling `prepare_agent_workspace_for_task` — so a
  fix to the shared function never reached first-dispatch, review-handoff,
  test-handoff, or review-rejection re-dispatches. `auto_dispatch_task` now
  calls `prepare_agent_workspace_for_task` directly (see
  `test_auto_dispatch_task_uses_shared_workspace_prep`).

Covers:
- POST .../reassign calls prepare_agent_workspace_for_task for the target
- PATCH .../{task_id} assigned_agent_id branch does the same
- execute_review_decision's self-review escalation does the same
- Host -> Container reassign actually sets task.workspace_path (real
  orchestration logic runs; only the git subprocess itself is stubbed,
  same convention as test_workspace_isolation.py)
- Container -> Host reassign is a genuine no-op: git_service is never
  touched at all, and the resulting workspace_path proves the shared
  no-op branch was actually *reached*, not merely never invoked (W3)
- Non-code project + stale foreign task.workspace_path gets re-pointed at
  the new agent on reassign (B1)
- auto_dispatch_task delegates to prepare_agent_workspace_for_task instead
  of duplicating its two steps (B2)
- _needs_non_code_workspace() unit coverage for all four states: unset,
  foreign, already-current, agent has no workspace_path at all
"""
from __future__ import annotations

import os
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
    (no workspace_path) must not invoke git at all — proven by making
    git_service._run_cmd raise if it's ever called.

    W3 correction (Rex, PR #568 review): the "no-op" here is specifically
    about GIT, not about task.workspace_path overall. It does NOT hold in
    general that "host agents have no workspace_path" — Hermes (a host
    agent) has one (alembic/versions/0095_hermes_runtime_and_single_
    instance.py:154); this test's `new_agent` genuinely has none only
    because the fixture explicitly sets `new_workspace_path=None`. And
    since the target has no workspace_path to build a Phase-C directory
    from, `_ensure_task_workspace` falls back to a scratch dir under
    `/tmp/mc_tasks/<task_id>` (pre-existing, unrelated to B1/B2) — so
    task.workspace_path does NOT stay untouched. The assertion below
    checks for exactly that fallback path, which is what actually proves
    the shared no-op-for-git branch was *reached* rather than the whole
    function *never being called* (had it never run, workspace_path would
    have stayed at its initial `None`, not turned into a /tmp path)."""
    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(
            async_session,
            old_workspace_path="/tmp/mc-test-not-used-old", old_agent_runtime="cli-bridge",
            new_workspace_path=None, new_agent_runtime="host",
        )
    )
    assert task.workspace_path is None, "precondition: task starts with no workspace_path"

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

    # Distinguishes "no-op reached" from "prepare_agent_workspace_for_task
    # never ran at all": if it had never run, workspace_path would still be
    # None. Instead the Phase-C /tmp fallback proves the function executed
    # and its git-side no-op branch (asserted above via _explode) was the
    # one actually taken.
    new_workspace = resp.json()["workspace_path"]
    assert new_workspace and os.path.join("mc_tasks", str(task.id)) in new_workspace, (
        f"workspace_path {new_workspace!r} does not look like the Phase-C /tmp "
        "fallback — prepare_agent_workspace_for_task may not have run at all"
    )


# ── B1: _needs_non_code_workspace unit coverage ──────────────────────────


def test_needs_non_code_workspace_unset_field_returns_true():
    """Regular first dispatch (or any never-provisioned task): must (re)provision."""
    from app.services.task_context_builder import _needs_non_code_workspace

    assert _needs_non_code_workspace(None, "/agents/new-agent") is True


def test_needs_non_code_workspace_foreign_path_returns_true():
    """The B1 bug: a SET but foreign (old agent's) path must also be
    treated as needing (re)provisioning — this is the half the original
    `if not task.workspace_path` guard never covered."""
    from app.services.task_context_builder import _needs_non_code_workspace

    assert _needs_non_code_workspace(
        "/agents/old-agent/_tasks/11111111-1111-1111-1111-111111111111",
        "/agents/new-agent",
    ) is True


def test_needs_non_code_workspace_already_current_returns_false():
    """No-op reassign / already-correct path: must NOT reprovision."""
    from app.services.task_context_builder import _needs_non_code_workspace

    assert _needs_non_code_workspace(
        "/agents/new-agent/_tasks/11111111-1111-1111-1111-111111111111",
        "/agents/new-agent",
    ) is False


def test_needs_non_code_workspace_no_agent_workspace_returns_false():
    """Target agent has no workspace_path at all (host agent without one) —
    nothing to build a Phase-C path from, existing path stands untouched."""
    from app.services.task_context_builder import _needs_non_code_workspace

    assert _needs_non_code_workspace(
        "/agents/old-agent/_tasks/11111111-1111-1111-1111-111111111111",
        None,
    ) is False


# ── B1: behavioral — stale foreign path gets re-pointed on reassign ─────


@pytest.mark.asyncio
async def test_reassign_prepares_workspace_when_field_is_stale_not_empty(
    client: AsyncClient, async_session, tmp_path,
):
    """Rex's incident-shaped probe (PR #568 review, B1): a project WITHOUT
    a github_repo_url (so step 1 — setup_git_workspace_for_dispatch — never
    touches task.workspace_path) whose task.workspace_path is already SET
    to the OLD agent's directory, seeded BEFORE reassign — the one
    production starting state none of the original 7 tests used (W1).

    Before the fix: `UNCHANGED? True` (Rex's own probe result). After:
    the path must be re-pointed at the NEW agent's own workspace tree.
    """
    old_ws = tmp_path / "old-agent"
    new_ws = tmp_path / "new-agent"
    old_ws.mkdir()
    new_ws.mkdir()

    project = Project(board_id=uuid.uuid4(), name="Non-Code Project")  # no github_repo_url

    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(
            async_session,
            old_workspace_path=str(old_ws), old_agent_runtime="host",
            new_workspace_path=str(new_ws), new_agent_runtime="cli-bridge",
        )
    )
    project.board_id = board.id
    async_session.add(project)
    await async_session.commit()
    await async_session.refresh(project)

    stale_path = str(old_ws / "_tasks" / str(task.id))
    os.makedirs(stale_path, exist_ok=True)
    task.project_id = project.id
    task.workspace_path = stale_path
    async_session.add(task)
    await async_session.commit()

    print(f"[PROBE-B1] workspace_path BEFORE reassign: {task.workspace_path}")

    from app.services.git_service import git_service

    async def _explode(*args, **kwargs):
        raise AssertionError(f"git_service._run_cmd must not run for a non-code project: {args}")

    with patch.object(git_service, "_run_cmd", new=AsyncMock(side_effect=_explode)):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/reassign",
            json={"to": "ContainerRunner"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 200, resp.text

    after = resp.json()["workspace_path"]
    print(f"[PROBE-B1] workspace_path AFTER  reassign: {after}")
    print(f"[PROBE-B1] UNCHANGED? {after == stale_path}")

    assert after != stale_path, "task.workspace_path still points at the OLD agent — B1 regressed"
    assert str(new_ws) in after, f"workspace_path {after!r} is not scoped under the NEW agent's workspace"


@pytest.mark.asyncio
async def test_reassign_same_agent_noop_does_not_reprovision(
    client: AsyncClient, async_session, tmp_path,
):
    """Nebenbedingung 2 (B1 DoD): a reassign that lands the task back on a
    workspace_path it already has (already scoped under the target agent)
    must not recompute/overwrite it — calling prepare_agent_workspace_for_task
    twice in a row for the same agent is idempotent."""
    ws = tmp_path / "same-agent"
    ws.mkdir()
    project = Project(board_id=uuid.uuid4(), name="Non-Code Project 2")

    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(
            async_session,
            old_workspace_path=str(tmp_path / "unrelated-old"), old_agent_runtime="host",
            new_workspace_path=str(ws), new_agent_runtime="cli-bridge",
        )
    )
    project.board_id = board.id
    async_session.add(project)
    await async_session.commit()
    await async_session.refresh(project)
    task.project_id = project.id
    async_session.add(task)
    await async_session.commit()

    from app.services.task_context_builder import prepare_agent_workspace_for_task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        a = await s.get(Agent, new_agent.id)
        assert await prepare_agent_workspace_for_task(t, a, s) is True
        first_path = t.workspace_path
    assert first_path and str(ws) in first_path

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        a = await s.get(Agent, new_agent.id)
        assert await prepare_agent_workspace_for_task(t, a, s) is True
        second_path = t.workspace_path

    assert second_path == first_path, "same-agent no-op reassign must not recompute the workspace path"


# ── B1 Nebenbedingung 1: regular first dispatch (empty field) unchanged ──


@pytest.mark.asyncio
async def test_prepare_workspace_first_dispatch_empty_field_unchanged_behavior(
    async_session, tmp_path,
):
    """The regular case (task.workspace_path unset, e.g. a fresh dispatch)
    must keep working exactly as before B1 — this is the one starting
    state all 7 original tests already covered; kept here as an explicit
    regression pin at the helper/behavioral level."""
    ws = tmp_path / "fresh-agent"
    ws.mkdir()
    project = Project(board_id=uuid.uuid4(), name="Non-Code Project 3")

    (board, lead, lead_token, old_agent, _, new_agent, _, task) = (
        await _setup_board_with_agents(
            async_session,
            old_workspace_path=None, old_agent_runtime="host",
            new_workspace_path=str(ws), new_agent_runtime="cli-bridge",
        )
    )
    project.board_id = board.id
    async_session.add(project)
    await async_session.commit()
    await async_session.refresh(project)
    task.project_id = project.id
    assert task.workspace_path is None
    async_session.add(task)
    await async_session.commit()

    from app.services.task_context_builder import prepare_agent_workspace_for_task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        a = await s.get(Agent, new_agent.id)
        assert await prepare_agent_workspace_for_task(t, a, s) is True
        result_path = t.workspace_path

    assert result_path and str(ws) in result_path


# ── B2: auto_dispatch_task delegates to the shared, fixed function ──────


@pytest.mark.asyncio
async def test_auto_dispatch_task_uses_shared_workspace_prep(
    make_board, make_agent, make_task,
):
    """Wiring: auto_dispatch_task must call prepare_agent_workspace_for_task
    instead of duplicating its two steps inline (PR #568 review B2) — the
    duplication was 0 Deletions (a copy, not an extraction), so a fix to the
    shared function (like B1) never reached first dispatch."""
    from unittest.mock import AsyncMock as _AsyncMock

    board = await make_board(
        name=f"B2-wiring-{uuid.uuid4().hex[:8]}",
        slug=f"b2-wiring-{uuid.uuid4().hex[:8]}",
        auto_dispatch_enabled=True,
    )
    agent = await make_agent(
        name="B2Agent", role="developer", board_id=board.id,
        agent_runtime="cli-bridge", workspace_path="/tmp/mc-test-b2-agent",
    )
    task = await make_task(
        board_id=board.id, status="inbox", title="B2 wiring probe",
        assigned_agent_id=agent.id,
    )

    with (
        patch("app.services.activity.broadcast", new=_AsyncMock()),
        patch("app.services.dispatch.engine", test_engine),
        patch(
            "app.services.task_context_builder.prepare_agent_workspace_for_task",
            new=_AsyncMock(return_value=True),
        ) as mock_prep,
    ):
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task.id, board.id)

    mock_prep.assert_awaited_once()
    call_task, call_agent, _call_session = mock_prep.await_args.args
    assert call_task.id == task.id
    assert call_agent.id == agent.id
