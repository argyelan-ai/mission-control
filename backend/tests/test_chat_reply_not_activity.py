"""A chat session's reply is never the run's activity — in every reader.

Round-5 code review (23.10.2026), r1 S1 + N5: since #969 the chat answers
"how far are you?" with ``mc comment message … --task-id`` (stored as
``chat_reply``). The watchdogs already skipped it, but the task list
(``list_tasks.last_activity_at`` → "no activity for X min") and the daily
digest's stale cards took ``max(TaskComment.created_at)`` over every type:
the chat's answer turned a red, hanging card back to "0m" and took it out of
the digest for 4 h. Further readers took the latest comment of any type as
the run's word (builder of a rework, failure reason, reviewer verdict, …).

One rule now: ``comment_types.NOT_ACTIVITY_COMMENT_TYPES`` with
``activity_comment_filter()`` — the tests below pin the visible readers by
behaviour and every other reader by a source guard, so a new
"latest comment" query cannot forget it silently.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.utils import utcnow
from tests.conftest import test_engine

APP = Path(__file__).resolve().parent.parent / "app"


async def _comment(task_id, *, comment_type, created_at, author_agent_id=None, content="x"):
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        c = TaskComment(
            id=uuid.uuid4(), task_id=task_id, author_type="agent",
            author_agent_id=author_agent_id, comment_type=comment_type,
            content=content, created_at=created_at,
        )
        s.add(c)
        await s.commit()
        return c


# ── The one rule ───────────────────────────────────────────────────────────

def test_chat_reply_is_the_one_type_that_is_not_activity():
    from app.comment_types import (
        ALL_COMMENT_TYPES, CHAT_REPLY_COMMENT_TYPE, NOT_ACTIVITY_COMMENT_TYPES,
        is_activity_comment_type,
    )

    assert CHAT_REPLY_COMMENT_TYPE in NOT_ACTIVITY_COMMENT_TYPES
    assert not is_activity_comment_type("chat_reply")
    # every type an agent run can send stays activity
    assert all(is_activity_comment_type(t) for t in ALL_COMMENT_TYPES)


@pytest.mark.asyncio
async def test_activity_comment_filter_drops_only_chat_replies(make_board, make_task):
    from app.comment_types import activity_comment_filter
    from app.models.task import TaskComment

    board = await make_board(slug=f"f-{uuid.uuid4().hex[:8]}")
    task = await make_task(board.id)
    now = utcnow()
    for ct in ("progress", "message", "chat_reply", "system"):
        await _comment(task.id, comment_type=ct, created_at=now)

    async with AsyncSession(test_engine) as s:
        kept = (await s.exec(
            select(TaskComment.comment_type).where(
                TaskComment.task_id == task.id, activity_comment_filter(),
            )
        )).all()
    assert sorted(kept) == ["message", "progress", "system"]


# ── r1 S1: task list "no activity for X min" ───────────────────────────────

@pytest.mark.asyncio
async def test_task_list_last_activity_ignores_a_later_chat_reply(auth_client, make_board, make_task):
    """Card in progress for 40 min, last run comment 25 min ago, the chat
    answers "how far are you?" 1 min ago → the row still says 25 min."""
    board = await make_board(slug=f"l-{uuid.uuid4().hex[:8]}")
    now = utcnow()
    task = await make_task(
        board.id, title="Hanging run", status="in_progress",
        started_at=now - timedelta(minutes=40), updated_at=now - timedelta(minutes=40),
    )
    progress_at = now - timedelta(minutes=25)
    await _comment(task.id, comment_type="progress", created_at=progress_at)
    await _comment(task.id, comment_type="chat_reply", created_at=now - timedelta(minutes=1),
                   content="Still on step 2.")

    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks")
    assert resp.status_code == 200, resp.text
    row = next(t for t in resp.json() if t["id"] == str(task.id))
    last = row["last_activity_at"]
    assert last is not None
    from datetime import datetime
    from app.utils import ensure_aware
    got = ensure_aware(datetime.fromisoformat(last))
    assert abs((got - ensure_aware(progress_at)).total_seconds()) < 1, (
        f"the chat's reply moved the card's activity clock: {last}"
    )


@pytest.mark.asyncio
async def test_task_list_still_counts_a_real_run_comment(auth_client, make_board, make_task):
    """Counter-probe: a run's own message 1 min ago does move the clock."""
    board = await make_board(slug=f"r-{uuid.uuid4().hex[:8]}")
    now = utcnow()
    task = await make_task(
        board.id, title="Live run", status="in_progress",
        started_at=now - timedelta(minutes=40), updated_at=now - timedelta(minutes=40),
    )
    msg_at = now - timedelta(minutes=1)
    await _comment(task.id, comment_type="message", created_at=msg_at)

    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks")
    row = next(t for t in resp.json() if t["id"] == str(task.id))
    from datetime import datetime
    from app.utils import ensure_aware
    got = ensure_aware(datetime.fromisoformat(row["last_activity_at"]))
    assert abs((got - ensure_aware(msg_at)).total_seconds()) < 1


# ── r1 S1: daily digest stale cards ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_digest_stale_card_stays_stale_after_a_chat_reply(make_board, make_task, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)
    board = await make_board(slug=f"d-{uuid.uuid4().hex[:8]}")
    now = utcnow()
    stale = await make_task(
        board.id, title="Silent 5h", status="in_progress", updated_at=now - timedelta(hours=5),
    )
    await _comment(stale.id, comment_type="chat_reply", created_at=now - timedelta(minutes=2))

    metrics = await compute_daily_metrics(None, now=now)
    assert str(stale.id) in {c["id"] for c in metrics["stale_cards"]}, (
        "a chat reply took a 5h-silent card out of the stale list"
    )


# ── r1 N5: the builder a rework goes back to ────────────────────────────────

@pytest.mark.asyncio
async def test_rework_builder_is_not_the_lead_who_only_answered_in_chat(
    fake_redis, make_board, make_task, make_agent,
):
    """No attributed status event, the builder left a plain message, then the
    Lead's chat answered the operator on the card → the builder, not the Lead."""
    from app.models.task import Task
    from app.services.work_context import find_last_developer

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", role="lead", board_id=board.id, is_board_lead=True)
    dev = await make_agent(name="Builder", role="developer", board_id=board.id)
    task = await make_task(board.id, title="t", status="review")
    now = utcnow()
    await _comment(task.id, comment_type="message", author_agent_id=dev.id,
                   created_at=now - timedelta(minutes=30), content="built it")
    await _comment(task.id, comment_type="chat_reply", author_agent_id=lead.id,
                   created_at=now - timedelta(minutes=1), content="Builder is done.")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        found = await find_last_developer(s, await s.get(Task, task.id))
    assert found is not None and found.id == dev.id


# ── Guard: every other "latest comment" reader uses the one rule ────────────

def _source(obj) -> str:
    return textwrap.dedent(inspect.getsource(obj))


def _filter_calls(node: ast.AST) -> int:
    """Real ``activity_comment_filter()`` calls under ``node`` — a code
    comment or docstring that only mentions it does not count."""
    return sum(
        1 for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "activity_comment_filter"
    )


@pytest.mark.parametrize("target, min_uses", [
    ("app.routers.tasks:list_tasks", 1),
    ("app.services.daily_metrics_digest:_stale_cards", 1),
    ("app.services.messaging:last_task_activity", 1),
    ("app.services.task_evidence:last_real_activity_at", 1),
    ("app.services.intelligence:IntelligenceService._detect_failure_patterns", 1),
    ("app.services.intelligence:IntelligenceService._populate_agent_metrics", 1),
    ("app.services.watchdog.task_monitor:TaskMonitorMixin._send_phase_screenshots_bg", 1),
    ("app.services.watchdog.task_monitor:TaskMonitorMixin._check_review_decision_missing", 1),
    ("app.services.watchdog.task_monitor:TaskMonitorMixin._lead_reacted_after", 1),
    ("app.services.watchdog.task_monitor:TaskMonitorMixin._silent_mailbox_last_activity_at", 1),
    ("app.services.task_runner:TaskRunnerService._handle_cli_bridge_inprogress_recovery", 1),
    ("app.services.task_runner:TaskRunnerService._check_stale_in_progress", 1),
    ("app.services.task_runner:TaskRunnerService._check_stuck_in_progress", 1),
    ("app.services.task_lifecycle:trigger_feedback_lesson", 1),
    ("app.services.work_context:find_last_developer", 1),
    ("app.services.proof_gate:_claimed_pr_urls", 1),
    ("app.services.auto_memory:_load_recent_comments", 1),
    ("app.services.blocker_triage:escalate_blocker_to_operator", 1),
    ("app.routers.consensus:get_consensus_status", 1),
    ("app.routers.agent_task_status:dispatch_callback_to_parent", 1),
    ("app.routers.agent_task_status:dispatch_resume_to_agent", 1),
    # review safeguard, review consistency guard, blocker text, unblock hint
    ("app.routers.agent_task_status:agent_update_task", 4),
])
def test_activity_reader_uses_the_shared_rule(target, min_uses):
    import importlib

    mod_name, qual = target.split(":")
    obj = importlib.import_module(mod_name)
    for part in qual.split("."):
        obj = getattr(obj, part)
    src = _source(obj)
    # Count real calls, not the text: a code comment or docstring that
    # mentions the helper must not satisfy the guard (round-6 review NICE 3).
    assert _filter_calls(ast.parse(src)) >= min_uses, (
        f"{target} reads comments as the run's activity without "
        "comment_types.activity_comment_filter()"
    )
    assert "CHAT_REPLY_COMMENT_TYPE" not in src, f"{target}: second spelling of the rule"


# Readers that DISPLAY every comment (the chat reply is shown, labelled) or
# look for one specific marker text — no activity semantics.
_DISPLAY_OR_MARKER_READERS = {
    ("routers/tasks.py", "get_task_timeline"),
    ("routers/tasks.py", "get_task_transcript"),
    # scoped, but by a filter built one statement earlier
    # (``operator_lead_filter``: operator ``message`` / non-system ``handoff``)
    ("services/task_context_builder.py", "build_recovery_context"),
}


def _unscoped_latest_comment_queries() -> set[tuple[str, str]]:
    """(file, function) of every statement that selects TaskComment ordered
    newest-first (or takes its max created_at) without ANY comment-type
    scoping: no positive ``comment_type`` filter, no ``activity_comment_filter()``,
    no marker-text search."""
    hits: set[tuple[str, str]] = set()
    newest = ("TaskComment.created_at.desc()", "max(TaskComment.created_at)",
              "col(TaskComment.created_at).desc()")
    # ``trusted_pr_note_filter(`` = system + ``progress`` + the "PR erstellt:"
    # note prefix, built in services/pr_marker.py (round 8 / L6).
    scoped = ("content.like", "content ==", "trusted_pr_note_filter(")
    # Only a POSITIVE type scope counts (``comment_type == "x"`` /
    # ``comment_type.in_(...)``). A negative one (``!= "system"``,
    # ``not_in({...})``) still lets a chat reply through (round-6 review NICE 3).
    positive_type_scope = re.compile(r"comment_type\)?\s*(==|\.in_\()")
    compound = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For,
                ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
    for path in APP.rglob("*.py"):
        text = path.read_text()
        if "TaskComment" not in text:
            continue
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in ast.walk(fn):
                if not isinstance(stmt, ast.stmt) or isinstance(stmt, compound):
                    continue
                seg = ast.get_source_segment(text, stmt) or ""
                if "select(" not in seg or not any(n in seg for n in newest):
                    continue
                if (_filter_calls(stmt) or any(s in seg for s in scoped)
                        or positive_type_scope.search(seg)):
                    continue
                hits.add((str(path.relative_to(APP)), fn.name))
    return hits


def test_no_unscoped_latest_comment_reader_outside_display():
    unscoped = _unscoped_latest_comment_queries() - _DISPLAY_OR_MARKER_READERS
    assert not unscoped, (
        "these read the newest comment of ANY type — a chat session's reply "
        "would count as the run. Add comment_types.activity_comment_filter() "
        f"(or list a pure display reader above): {sorted(unscoped)}"
    )
