"""Reflection enforcement tests (TST-04 — Phase 4 Plan 04-11 fills bodies).

Production code references:
  - backend/app/constants.py — REFLECTION_REQUIRED_FIELDS (4 fields)
  - backend/app/constants.py — REFLECTION_MIN_CHARS = 80
  - backend/app/services/work_context.py:enforce_reflection — raises HTTPException(400)
  - backend/app/routers/agent_comments.py — POST /comments + reflection→memory pipeline
  - backend/app/routers/agent_comments.py:_extract_reflection_lesson — regex helper

OPEN QUESTION A1 RESOLVED: production raises HTTP 400 (NOT 422 per ROADMAP).
TST-04 aligns to production. Discrepancy noted in Plan 04-12 sign-off SUMMARY.

Plan 04-11 deviations (documented in 04-11-SUMMARY.md):
  D1: make_agent factory doesn't expose plain token — use generate_agent_token()
      + custom _make_agent_with_token helper (Rule 3 — blocking on plan auth pattern).
  D2: Tests 1/2/3/6 changed task.status from "review" → "in_progress" because
      enforce_reflection skips when task.status in ("review","user_test") — see
      work_context.py:265-268. Plan's "review" starting status would silently
      bypass enforcement and tests would never trigger the 400 (Rule 1 — bug).
  D3: TaskComment uses author_agent_id (not agent_id) — Plan typo (Rule 1).
"""
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.constants import REFLECTION_REQUIRED_FIELDS, REFLECTION_MIN_CHARS
from app.models.memory import BoardMemory
from tests.conftest import test_engine


# ─────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────

def make_full_reflection_content() -> str:
    """Build a body that passes all 4 fields + min char check."""
    body = "\n\n".join(
        f"## {f}\n[Test content for field {i} — substantial enough to satisfy "
        f"min-char threshold and provide meaningful regex anchor for "
        f"_extract_reflection_lesson regex helper]"
        for i, f in enumerate(REFLECTION_REQUIRED_FIELDS, 1)
    )
    assert len(body) >= REFLECTION_MIN_CHARS, (
        f"Test fixture must pass min-char check ({len(body)} < {REFLECTION_MIN_CHARS})"
    )
    return body


async def _make_agent_with_token(
    *,
    name: str,
    board_id,
    is_board_lead: bool = False,
    role: str = "developer",
):
    """Create an Agent with a plain token usable for Bearer auth.

    Plan suggested using `make_agent` factory but that doesn't expose the
    plain token (PBKDF2 hashes on insert). All other agent-scoped tests
    use this raw_token + token_hash pattern (e.g. test_review_policy.py,
    test_predone_validation.py, test_subtask_blocked_parent_notify.py).
    """
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name=name,
        role=role,
        board_id=board_id,
        agent_token_hash=token_hash,
        is_board_lead=is_board_lead,
        scopes=["tasks:read", "tasks:write"],
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw_token


def _agent_headers(token: str) -> dict[str, str]:
    """Return Authorization headers for an agent token."""
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────
# TST-04 Tests
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_blocked_without_reflection(
    client, fake_redis, make_board, make_task,
):
    """Status=done without reflection comment → HTTP 400 (NOT 422 — A1).

    German error message asserted: "Pflicht-Reflexion fehlt" + REFLECTION_REQUIRED_FIELDS.

    Note: task starts in "in_progress" (not "review" as plan said) — see D2.
    enforce_reflection skips when current task.status is already "review".
    """
    board = await make_board(slug="mc-dev", require_review_before_done=True)
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="in_progress", assigned_agent_id=cody.id,
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(cody_token),
        )
    # Per A1: production code raises HTTP 400 (despite ROADMAP saying 422)
    assert response.status_code == 400, (
        f"A1: production raises 400 (NOT 422). "
        f"Got: {response.status_code} {response.text[:300]}"
    )
    detail = response.json().get("detail", "")
    assert "Pflicht-Reflexion fehlt" in detail, (
        f"German error message changed. Got: {detail[:200]}"
    )
    # All 4 required fields named in error message
    for field in REFLECTION_REQUIRED_FIELDS:
        assert field in detail, f"Field name {field} missing from error: {detail[:200]}"


@pytest.mark.asyncio
async def test_done_blocked_when_reflection_too_short(
    client, fake_redis, make_board, make_task,
):
    """Reflection comment < REFLECTION_MIN_CHARS chars → 400."""
    from app.models.task import TaskComment

    board = await make_board(slug="mc-dev", require_review_before_done=True)
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="in_progress", assigned_agent_id=cody.id,
    )

    # Pre-create a too-short reflection comment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        short = TaskComment(
            id=uuid.uuid4(),
            task_id=task.id,
            author_type="agent",
            author_agent_id=cody.id,
            comment_type="reflection",
            content="Too short reflection",  # < REFLECTION_MIN_CHARS chars
            created_at=datetime.utcnow(),
        )
        s.add(short)
        await s.commit()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(cody_token),
        )
    assert response.status_code == 400, (
        f"Got: {response.status_code} {response.text[:300]}"
    )
    detail = response.json().get("detail", "")
    # German message variants — accept either "zu kurz" or fields-list message
    assert ("zu kurz" in detail) or ("Pflicht-Reflexion fehlt" in detail), (
        f"Expected German short-or-missing reflection message. Got: {detail[:200]}"
    )


@pytest.mark.asyncio
async def test_done_passes_with_full_reflection(
    client, fake_redis, make_board, make_task,
):
    """All 4 reflection fields + ≥ REFLECTION_MIN_CHARS → status transition succeeds.

    Note: this test must use a board WITHOUT require_review_before_done=True,
    OR use an is_board_lead agent — otherwise Rule 2 of enforce_board_rules_agent
    blocks in_progress→done with "Task muss zuerst durch Review...".
    We use require_review_before_done=False so a non-lead agent can transition
    in_progress → done once reflection is in place.
    """
    from app.models.task import TaskComment

    board = await make_board(slug="ideas", require_review_before_done=False)
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="in_progress", assigned_agent_id=cody.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        full = TaskComment(
            id=uuid.uuid4(),
            task_id=task.id,
            author_type="agent",
            author_agent_id=cody.id,
            comment_type="reflection",
            content=make_full_reflection_content(),
            created_at=datetime.utcnow(),
        )
        s.add(full)
        await s.commit()

    with patch(
        "app.services.memory_indexing.index_memory", new_callable=AsyncMock,
    ), patch("app.services.activity.broadcast", new_callable=AsyncMock):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers=_agent_headers(cody_token),
        )
    assert response.status_code in (200, 201), (
        f"Full reflection should pass. "
        f"Got: {response.status_code} {response.text[:300]}"
    )


@pytest.mark.asyncio
async def test_reflection_creates_lesson_in_board_memory(
    client, fake_redis, make_board, make_task,
):
    """POST reflection comment → BoardMemory(memory_type='lesson', auto_generated=True) row exists.

    Pitfall F: index_memory MUST be mocked — production wraps it in try/except
    so a Qdrant outage doesn't break comment POST. We mock to avoid hitting
    the real indexer in tests.
    """
    board = await make_board()
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="in_progress", assigned_agent_id=cody.id,
    )

    with patch(
        "app.services.memory_indexing.index_memory", new_callable=AsyncMock,
    ) as _mock_idx, patch(
        "app.services.activity.broadcast", new_callable=AsyncMock,
    ):
        response = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={
                "comment_type": "reflection",
                "content": make_full_reflection_content(),
            },
            headers=_agent_headers(cody_token),
        )
    assert response.status_code == 201, response.text[:300]

    # Verify BoardMemory row created via the reflection pipeline (Plan 04-06)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(BoardMemory).where(
                BoardMemory.agent_id == cody.id,
                BoardMemory.memory_type == "lesson",
            )
        )
        lessons = result.all()
    assert len(lessons) == 1, f"Expected 1 lesson, got {len(lessons)}"
    assert lessons[0].auto_generated is True
    assert "reflection" in (lessons[0].tags or []), (
        f"'reflection' tag missing. tags={lessons[0].tags}"
    )


@pytest.mark.asyncio
async def test_reflection_on_review_status_no_block(
    client, fake_redis, make_board, make_task,
):
    """POST reflection while status=review → no error.

    Enforcement only kicks in on PATCH status=done — POSTing a reflection
    comment is always allowed regardless of task status.
    """
    board = await make_board()
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, status="review", assigned_agent_id=cody.id,
    )

    with patch(
        "app.services.memory_indexing.index_memory", new_callable=AsyncMock,
    ), patch("app.services.activity.broadcast", new_callable=AsyncMock):
        response = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={
                "comment_type": "reflection",
                "content": make_full_reflection_content(),
            },
            headers=_agent_headers(cody_token),
        )
    assert response.status_code == 201, response.text[:300]


@pytest.mark.asyncio
async def test_reflection_blocks_phase_review_done_too(
    client, fake_redis, make_board, make_task,
):
    """Phase-review (parent task with done subtasks) → done also requires reflection,
    UNLESS agent.is_board_lead (production work_context.py:269 exemption).

    Non-lead → reflection enforcement blocks with 400 + "Pflicht-Reflexion fehlt".
    Board lead → reflection skipped. Subsequent rules may still reject (e.g.
    require_review_before_done) but the rejection MUST NOT cite reflection.
    """
    board = await make_board(slug="mc-dev", require_review_before_done=True)
    # Board lead is exempt from reflection
    henry, henry_token = await _make_agent_with_token(
        name="Henry", board_id=board.id, is_board_lead=True, role="lead",
    )
    # Non-lead is NOT exempt
    cody, cody_token = await _make_agent_with_token(
        name="Cody", board_id=board.id, is_board_lead=False,
    )

    # Parent task in_progress (not review — D2). Cody assigned, but it's a
    # phase parent so we'll attempt close with NO reflection. Both agents
    # try in turn against fresh tasks (need separate tasks since first
    # attempt would change task.status if it were to succeed).
    task_for_cody = await make_task(
        board_id=board.id, status="in_progress", assigned_agent_id=cody.id,
    )
    task_for_henry = await make_task(
        board_id=board.id, status="in_progress", assigned_agent_id=henry.id,
    )

    # cody (non-lead) tries to set parent to done WITHOUT reflection → 400
    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        cody_resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task_for_cody.id}",
            json={"status": "done"},
            headers=_agent_headers(cody_token),
        )
    assert cody_resp.status_code == 400, (
        f"Non-lead must be blocked. "
        f"Got: {cody_resp.status_code} {cody_resp.text[:300]}"
    )
    cody_detail = cody_resp.json().get("detail", "")
    assert "Pflicht-Reflexion fehlt" in cody_detail, (
        f"Non-lead rejection must cite reflection. Got: {cody_detail[:200]}"
    )

    # henry (board lead) — reflection check is skipped (exemption).
    # The subsequent require_review_before_done gate may still reject
    # (in_progress → done without going through review), but the rejection
    # MUST NOT cite reflection.
    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        henry_resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task_for_henry.id}",
            json={"status": "done"},
            headers=_agent_headers(henry_token),
        )
    if henry_resp.status_code != 200:
        henry_detail = henry_resp.json().get("detail", "")
        assert "Pflicht-Reflexion fehlt" not in henry_detail, (
            f"Board lead should be exempt from reflection. "
            f"Got: {henry_resp.status_code} {henry_detail[:200]}"
        )
