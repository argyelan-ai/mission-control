"""#6e828ffc — a review card re-entering in_progress→review must not spawn a
SECOND review_handoff at a different reviewer.

Reproduction (activity_events cards c0fb45c1/fd4ac7c6, 14.09.2026): the
assigned reviewer of a `dispatch_intent == "review_handoff"` card finishes
their review turn via the generic in_progress→review transition (the same
verb a developer uses to submit code, e.g. `mc finish --review`) instead of
the dedicated decision verbs (`mc review approve|reject`). handle_review_
handoff's own dedupe ("already assigned to a reviewer") only recognises the
current assignee when `existing_reviewer.role == "reviewer"` LITERALLY —
but `find_reviewer` (work_context.py) also matches agents via a legacy
name-based fallback ("rex"/"review" in the name) for agents whose `role`
is freetext, a state this board's own Rex has been in before (work_context.
py's own "Vorfall 94fda9f9" comment). For such an agent the dedupe fails to
recognise them as already assigned, `_find_reviewer(exclude_agent_id=<this
reviewer>)` runs a fresh search excluding them, and — since the same
name-fallback still has candidates — a genuinely DIFFERENT second reviewer
gets the card and reviews it a second time.

Part A reproduces the bug end-to-end with the guard monkeypatched out
(sabotage: each test disarms only the ONE call site it pins, proving that
site's own guard call — not the shared helper in isolation — is what stops
the chain). Part B pins the guard behaviour going forward.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment

from tests.conftest import test_engine


async def _make_review_handoff_fixture(
    *, reviewer_role: str | None = "Rex", second_reviewer_role: str | None = "Rex-2",
):
    """Board with a developer, a review-assigned agent with a FREETEXT role
    (matches only via find_reviewer's name fallback, not role=="reviewer"),
    a second name-matching agent (the one the bug hands the card to), and a
    task already mid-review (dispatch_intent="review_handoff",
    assigned_agent_id=reviewer, status="in_progress" — the reviewer ACKed
    in and is now finishing their turn).
    """
    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    second_reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Self-Dispatch Guard", slug=f"sdg-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=dev_id, board_id=board_id, name="Dev", role="developer",
            agent_runtime="cli-bridge",
        ))
        s.add(Agent(
            id=reviewer_id, board_id=board_id, name="Rex", role=reviewer_role,
            agent_runtime="cli-bridge", agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        ))
        s.add(Agent(
            id=second_reviewer_id, board_id=board_id, name="Rex-Standby",
            role=second_reviewer_role, agent_runtime="cli-bridge",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="PR under review",
            status="in_progress", assigned_agent_id=reviewer_id,
            dispatch_intent="review_handoff",
        ))
        # Evidence guard + ADR-023 mandatory reflection on the agent PATCH path.
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=reviewer_id,
            comment_type="progress", content="Review in Arbeit.",
        ))
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=reviewer_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\n"
                "Den kompletten Diff des eingereichten PRs durchgesehen, "
                "Logik und Tests nachvollzogen.\n\n"
                "## Was hat funktioniert\n"
                "Alle Aenderungen waren gut nachvollziehbar und die Tests "
                "liefen sauber durch.\n\n"
                "## Was war unklar\n"
                "Nichts war unklar, die Implementierung war eindeutig.\n\n"
                "## Lesson für Agent-Memory\n"
                "Keine neue Lesson aus diesem Review-Durchgang."
            ),
        ))
        await s.commit()

    return board_id, dev_id, reviewer_id, second_reviewer_id, task_id, raw_token


# ── Part A: reproduction (guard disarmed at ONE call site per test) ──────


@pytest.mark.asyncio
async def test_repro_agent_patch_path_without_guard_dispatches_second_reviewer(client, fake_redis):
    """Sabotage: patch review_card_would_self_dispatch to always False at
    the agent_task_status.py call site only — the exact pre-fix behaviour.
    Must reproduce the incident: the card ends up assigned to the SECOND
    (different) reviewer, not the original one."""
    board_id, dev_id, reviewer_id, second_reviewer_id, task_id, token = (
        await _make_review_handoff_fixture()
    )

    with (
        patch("app.services.task_lifecycle.review_card_would_self_dispatch", return_value=False),
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.assigned_agent_id == second_reviewer_id, (
            "Reproduction failed: expected the SAME bug as the incident — "
            f"card handed to the second reviewer, got {task.assigned_agent_id}"
        )
        assert task.assigned_agent_id != reviewer_id


# ── Part B: the guard, active ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_patch_path_blocks_review_card_self_dispatch(client, fake_redis):
    """Aufrufer 1 (agent_task_status.py): the reviewer patching their own
    review_handoff card back to 'review' gets a 409, not a silent handoff
    to someone else. Card stays exactly where it was."""
    board_id, dev_id, reviewer_id, second_reviewer_id, task_id, token = (
        await _make_review_handoff_fixture()
    )

    with (
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock) as mock_pr,
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 409, resp.text
    assert "mc review approve|reject" in resp.text
    mock_pr.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        # The generic status write commits earlier in the request than this
        # guard (same pre-existing structure as the Evidence-guard above it —
        # see test_phase2a_observability.py's no-evidence 409, which leaves
        # the same committed status behind). That is the RIGHT outcome here:
        # status="review" is exactly the precondition execute_review_decision
        # needs, so the reviewer's very next `mc review approve|reject` call
        # (which the 409 tells them to use) works immediately — no separate
        # recovery step, no card stuck in a status the decision endpoint
        # would reject.
        assert task.status == "review"
        assert task.assigned_agent_id == reviewer_id, (
            "No second handoff must have happened — same reviewer still assigned"
        )


@pytest.mark.asyncio
async def test_repro_auto_promote_path_without_guard_dispatches_second_reviewer(client, fake_redis):
    """Sabotage: disarm the guard at the agent_comments.py auto-promote call
    site only. A resolution comment on the reviewer's own review_handoff
    card must reproduce the incident there too."""
    board_id, dev_id, reviewer_id, second_reviewer_id, task_id, token = (
        await _make_review_handoff_fixture()
    )

    with (
        patch("app.services.task_lifecycle.review_card_would_self_dispatch", return_value=False),
        patch("app.routers.agent_git.handle_review_pr_creation", new_callable=AsyncMock),
        patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock),
    ):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            json={"content": "**Update** — Review fertig.", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.assigned_agent_id == second_reviewer_id, (
            "Reproduction failed: expected the auto-promote path to hand "
            f"the card to the second reviewer too, got {task.assigned_agent_id}"
        )


@pytest.mark.asyncio
async def test_auto_promote_path_skips_review_card_self_dispatch(client, fake_redis):
    """Aufrufer 3 (agent_comments.py resolution auto-promote): the reviewer
    posting a resolution comment on their own review_handoff card must NOT
    auto-promote (which would spawn a second handoff) — the card stays
    in_progress with an explanatory system comment, not stuck silently."""
    board_id, dev_id, reviewer_id, second_reviewer_id, task_id, token = (
        await _make_review_handoff_fixture()
    )

    with (
        patch("app.routers.agent_git.handle_review_pr_creation", new_callable=AsyncMock),
        patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock),
    ):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            json={"content": "**Update** — Review fertig.", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status == "in_progress"
        assert task.assigned_agent_id == reviewer_id

        from sqlmodel import select
        notices = (await s.exec(
            select(TaskComment).where(
                TaskComment.task_id == task_id,
                TaskComment.comment_type == "system_notify",
            )
        )).all()
        assert any("Auto-Promote uebersprungen" in c.content for c in notices), (
            "Kein sichtbarer Hinweis, warum die Karte nicht promoted wurde"
        )


@pytest.mark.asyncio
async def test_board_lead_bypass_stays_open(client, fake_redis):
    """Lead-Bypass darf nicht eingeschraenkt werden (#6e828ffc DoD): ein
    Board Lead, der eine review_handoff-Karte selbst auf review zurueck-
    setzt (z.B. um sie bewusst umzuhaengen), wird NICHT blockiert."""
    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    task_id = uuid.uuid4()
    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Lead Bypass", slug=f"lb-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, board_id=board_id, name="Boss", role="orchestrator",
            is_board_lead=True, agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Lead reroutes this",
            status="in_progress", assigned_agent_id=lead_id,
            dispatch_intent="review_handoff",
        ))
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=lead_id,
            comment_type="progress", content="Sehe mir das nochmal an.",
        ))
        await s.commit()

    with (
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff,
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert resp.status_code == 200, resp.text
    mock_handoff.assert_called_once()
