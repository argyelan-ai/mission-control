"""Tests for app.services.operator_minutes — operator minutes per job
(ROADMAP E1 acceptance "operator minutes <= 15 per job", Stop-1 checklist).

The numbers are an estimate built from what MC records about the operator's
side of a job; these tests pin the rules, not a precision MC does not have.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

T0 = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)  # Wednesday of 2026-W39


def _m(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def _add(*rows):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for row in rows:
            s.add(row)
        await s.commit()


def _event(task_id, minute, *, changed_by="user", reason="manual_update", to_status="in_progress"):
    from app.models.task import TaskEvent

    return TaskEvent(
        id=uuid.uuid4(), task_id=task_id, from_status="inbox", to_status=to_status,
        changed_by=changed_by, reason=reason, created_at=_m(minute),
    )


def _comment(task_id, minute, *, author_type="user", comment_type="message"):
    from app.models.task import TaskComment

    return TaskComment(
        id=uuid.uuid4(), task_id=task_id, author_type=author_type,
        comment_type=comment_type, content="x", created_at=_m(minute),
    )


def _approval(task_id, minute, *, action_type="blocker_decision", status="approved", resolved_minute=None):
    from app.models.approval import Approval

    return Approval(
        id=uuid.uuid4(), task_id=task_id, action_type=action_type, description="q",
        status=status, created_at=_m(minute),
        resolved_at=_m(resolved_minute) if resolved_minute is not None else None,
    )


# ── The clustering rule ────────────────────────────────────────────────

def test_active_minutes_clusters_touches_into_sittings():
    from app.services.operator_minutes import MIN_SESSION, SESSION_GAP, active_minutes

    assert SESSION_GAP == timedelta(minutes=10)
    assert MIN_SESSION == timedelta(minutes=3)
    assert active_minutes([]) == (0.0, 0)
    # one lone click still costs a sitting of at least MIN_SESSION
    assert active_minutes([_m(0)]) == (3.0, 1)
    # two clicks 5 min apart: one sitting, its span counts
    assert active_minutes([_m(5), _m(0)]) == (5.0, 1)
    # 30 min apart: two sittings, each at least MIN_SESSION
    assert active_minutes([_m(0), _m(30)]) == (6.0, 2)
    # a chain of gaps <= SESSION_GAP stays one sitting
    assert active_minutes([_m(0), _m(8), _m(16)]) == (16.0, 1)
    # exactly SESSION_GAP apart still belongs to the same sitting
    assert active_minutes([_m(0), _m(10)]) == (10.0, 1)


# ── Per task ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_estimate_counts_operator_touches_questions_and_rescues(make_board, make_task, session):
    from app.models.thread import Message, Thread
    from app.services.operator_minutes import operator_minutes_for_task

    board = await make_board()
    task = await make_task(board.id, title="job")
    thread = Thread(id=uuid.uuid4(), kind="task", task_id=task.id, title="t")
    await _add(thread)
    await _add(
        # sitting 1 (0..6): operator starts a head, writes a comment, replies in the thread
        _event(task.id, 0, changed_by="head", reason="head_start"),
        _comment(task.id, 4),
        Message(id=uuid.uuid4(), thread_id=thread.id, seq=1, sender_type="user",
                message_type="message", body="go", created_at=_m(6)),
        # the head fails, the operator restarts it (a rescue) — sitting 2 at 60
        _event(task.id, 40, changed_by="head", reason="head_failed", to_status="failed"),
        _event(task.id, 60, changed_by="head", reason="head_restart"),
        # the head asks the operator (follow-up), the answer restarts it (no rescue)
        _event(task.id, 90, changed_by="head", reason="head_needs_you", to_status="waiting"),
        _event(task.id, 120, changed_by="head", reason="head_restart"),
        # an agent asks via approval, answered at 200 (sitting 4)
        _approval(task.id, 150, action_type="clarification_question", resolved_minute=200),
        # an agent asks for a decision in a comment (follow-up, not an operator touch)
        _comment(task.id, 160, author_type="agent", comment_type="needs_decision"),
        # a manual status fix at 300 (sitting 5, a rescue)
        _event(task.id, 300, changed_by="user", reason="manual_update", to_status="done"),
        # the operator stops a head (sitting 6, a rescue)
        _event(task.id, 400, changed_by="head", reason="head_stopped", to_status="blocked"),
        # noise that is NOT the operator
        _event(task.id, 500, changed_by="agent", reason="review_handoff"),
        _event(task.id, 501, changed_by="watchdog", reason="aborted_recovery"),
        _event(task.id, 502, changed_by="head", reason="head_passed", to_status="review"),
        _comment(task.id, 503, author_type="agent"),
        _comment(task.id, 504, author_type="system"),
        _approval(task.id, 505, action_type="dispatch_escalation", status="superseded", resolved_minute=506),
    )

    est = await operator_minutes_for_task(session, task.id)

    assert est["estimate"] is True
    assert est["sessions"] == 6
    # 6 (0..6) + 3 + 3 + 3 (approval answer) + 3 + 3
    assert est["active_minutes"] == 21
    assert est["touches"] == 8
    assert est["touches_by_kind"] == {
        "head_start": 1, "comment": 1, "thread_reply": 1, "head_restart": 2,
        "approval_answer": 1, "status_change": 1, "head_stop": 1,
    }
    assert est["follow_up_questions"] == 3  # head_needs_you + clarification_question + needs_decision
    assert est["manual_status_changes"] == 1
    assert est["head_rescues"] == 2  # restart after failed + operator stop
    assert est["rescues"] == 3
    assert est["path"] == "head"


@pytest.mark.asyncio
async def test_children_count_into_the_parent_job(make_board, make_task, session):
    from app.services.operator_minutes import operator_minutes_for_task

    board = await make_board()
    parent = await make_task(board.id, title="epic")
    child = await make_task(board.id, title="child", parent_task_id=parent.id)
    grandchild = await make_task(board.id, title="grandchild", parent_task_id=child.id)
    await _add(
        _comment(parent.id, 0),
        _event(child.id, 30),
        _event(grandchild.id, 32, reason="manual_stop"),
    )

    est = await operator_minutes_for_task(session, parent.id)

    assert est["sessions"] == 2
    assert est["active_minutes"] == 6
    assert est["manual_status_changes"] == 2
    assert est["path"] == "fleet"


@pytest.mark.asyncio
async def test_writing_the_order_in_the_ui_is_a_touch(make_board, make_task, session):
    from app.models.user import User
    from app.services.operator_minutes import operator_minutes_for_task

    user = User(id=uuid.uuid4(), email="op@mc.local", name="op", role="admin", is_active=True)
    await _add(user)
    board = await make_board()
    task = await make_task(board.id, created_by_user_id=user.id, created_at=_m(0))
    agent_made = await make_task(board.id, created_at=_m(0))
    await _add(_comment(task.id, 60))

    est = await operator_minutes_for_task(session, task.id)

    assert est["touches_by_kind"] == {"task_created": 1, "comment": 1}
    assert est["sessions"] == 2
    assert est["active_minutes"] == 6
    assert (await operator_minutes_for_task(session, agent_made.id))["touches"] == 0


@pytest.mark.asyncio
async def test_task_without_operator_touches_is_zero(make_board, make_task, session):
    from app.services.operator_minutes import operator_minutes_for_task

    board = await make_board()
    task = await make_task(board.id)
    await _add(_event(task.id, 0, changed_by="agent", reason=None))

    est = await operator_minutes_for_task(session, task.id)

    assert est["active_minutes"] == 0
    assert est["sessions"] == 0
    assert est["rescues"] == 0


# ── Per week ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_week_view_median_over_finished_jobs_split_by_path(make_board, make_task, session):
    from app.services.operator_minutes import operator_minutes_by_week

    board = await make_board()
    # fleet job, done in W39, 2 sittings = 6 min
    fleet = await make_task(board.id, title="fleet", status="done", completed_at=_m(600))
    # head job, PR open (review) in W39 = finished for the head path, 1 sitting = 3 min
    head = await make_task(board.id, title="head", status="review")
    # a done child is not a job of its own (it counts into its parent)
    await make_task(board.id, title="child", status="done", completed_at=_m(600), parent_task_id=fleet.id)
    # still open, not finished
    open_task = await make_task(board.id, title="open", status="in_progress")
    # done in the previous week (W38)
    old = await make_task(board.id, title="old", status="done", completed_at=_m(-3 * 24 * 60))
    await _add(
        _comment(fleet.id, 0), _comment(fleet.id, 100),
        _event(head.id, 0, changed_by="head", reason="head_start"),
        _event(head.id, 30, changed_by="head", reason="head_passed", to_status="review"),
        _comment(open_task.id, 0),
        _comment(old.id, -3 * 24 * 60 - 60),
    )

    view = await operator_minutes_by_week(session, weeks=2, now=_m(24 * 60))

    assert [w["week"] for w in view["weeks"]] == ["2026-W38", "2026-W39"]
    assert view["estimate"] is True
    w38, w39 = view["weeks"]
    assert w38["jobs"] == 1 and w38["median_minutes"] == 3
    assert w39["jobs"] == 2
    assert w39["median_minutes"] == 4.5  # median of 6 and 3
    assert w39["total_minutes"] == 9
    assert w39["by_path"]["head"]["jobs"] == 1
    assert w39["by_path"]["head"]["median_minutes"] == 3
    assert w39["by_path"]["fleet"]["jobs"] == 1
    assert w39["by_path"]["fleet"]["median_minutes"] == 6
    assert w39["jobs_over_target"] == 0
    assert view["target_minutes"] == 15


@pytest.mark.asyncio
async def test_week_view_empty_week_has_no_median(session):
    from app.services.operator_minutes import operator_minutes_by_week

    view = await operator_minutes_by_week(session, weeks=1, now=_m(0))

    assert view["weeks"][0]["jobs"] == 0
    assert view["weeks"][0]["median_minutes"] is None


# ── Exposure: run record, digest M6, endpoint ──────────────────────────

@pytest.mark.asyncio
async def test_run_record_carries_the_estimate_under_zeiten(make_board, make_task, session):
    from app.services.run_record import build_run_record, render_run_record_markdown

    board = await make_board()
    task = await make_task(board.id)
    await _add(_comment(task.id, 0), _event(task.id, 1))

    record = await build_run_record(session, task.id)

    bedienung = record["zeiten"]["bedienung"]
    assert bedienung["active_minutes"] == 3
    assert bedienung["manual_status_changes"] == 1
    md = render_run_record_markdown(record)
    assert "- Bedienzeit (Schaetzung): ~3 min in 1 Sitzung(en), 0 Rueckfragen, 1 Rettung(en)" in md


@pytest.mark.asyncio
async def test_digest_m6_operator_minutes_last_7_days(make_board, make_task, session):
    from app.services.daily_metrics_digest import compute_daily_metrics, format_digest

    board = await make_board()
    job = await make_task(board.id, status="done", completed_at=_m(60))
    await _add(_comment(job.id, 0), _comment(job.id, 50))

    metrics = await compute_daily_metrics(session, now=_m(24 * 60))

    m6 = metrics["operator_minutes_7d"]
    assert m6["jobs"] == 1
    assert m6["median_minutes"] == 6
    line = [l for l in format_digest(metrics, now=_m(24 * 60)).splitlines() if l.startswith("M6:")]
    assert line == ["M6: Bedienminuten je fertigem Auftrag (Median, Schaetzung, 7 T): 6 min bei 1 Auftraegen, 0 ueber 15 min"]


def test_digest_m6_without_jobs_and_on_error():
    from app.services.daily_metrics_digest import format_digest

    lines = format_digest({"operator_minutes_7d": {"jobs": 0, "median_minutes": None}}).splitlines()
    assert "M6: Bedienminuten je fertigem Auftrag: keine fertigen Auftraege (7 T)" in lines
    lines = format_digest({"operator_minutes_7d": {"error": True}}).splitlines()
    assert "M6: Bedienminuten je fertigem Auftrag: nicht verfuegbar" in lines


@pytest.mark.asyncio
async def test_digest_m6_failure_keeps_the_other_metrics(session, monkeypatch):
    from app.services import daily_metrics_digest as dmd

    async def boom(*_a, **_k):
        raise RuntimeError("broken")

    monkeypatch.setattr(dmd, "_operator_minutes_7d", boom)

    metrics = await dmd.compute_daily_metrics(session)

    assert metrics["operator_minutes_7d"] == {"error": True}
    assert "stale_cards" in metrics


@pytest.mark.asyncio
async def test_operator_minutes_endpoint(auth_client, make_board, make_task):
    board = await make_board()
    job = await make_task(board.id, status="done", completed_at=datetime.now(timezone.utc))
    await _add(_comment(job.id, 0))

    resp = await auth_client.get("/api/v1/system/operator-minutes?weeks=2")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["weeks"]) == 2
    assert body["weeks"][-1]["jobs"] == 1
    assert body["rules"]["session_gap_minutes"] == 10
    assert body["rules"]["min_session_minutes"] == 3


@pytest.mark.asyncio
async def test_operator_minutes_endpoint_requires_login(client):
    assert (await client.get("/api/v1/system/operator-minutes")).status_code in (401, 403)
