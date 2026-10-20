"""Beweis-Tor (proof gate): a card carrying a PR only reaches review/done with
evidence that GitHub and MC actually hold.

Sabotage probes, each in BOTH modes (warn: transition allowed + card comment +
event; enforce: 422 + status unchanged):

  - invented PR URL                    → pr_not_found
  - PR with red CI                     → ci_red
  - no commit after a change request   → no_commit_after_change
  - reviewer closes without comment    → reviewer_silent
  - reported SHA is not the remote one → sha_mismatch

and the counter-probes:

  - a real, clean case passes without any warning (no comment, no event)
  - no GitHub token → fail-open with a visible warning, in enforce mode too
  - there is no server-side "force": an extra field is refused by the schema
    and the gate runs on every path (`mc finish --force` only closes checklist
    items client-side)
"""

import json
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.services import proof_gate
from app.utils import utcnow
from tests.conftest import test_engine

PR_URL = "https://github.com/example-org/example-repo/pull/42"
SHA = "a" * 40
OTHER_SHA = "b" * 40
_REFLECTION = (
    "## Was wurde gemacht\nFeature implementiert inklusive Unit-Tests "
    "und Doku-Update im README.\n\n"
    "## Was hat funktioniert\nTDD-Zyklus lief sauber durch, alle Tests "
    "auf Anhieb gruen.\n\n"
    "## Was war unklar\nNichts Nennenswertes.\n\n"
    "## Lesson fuer Agent-Memory\nKeine neue Lesson."
)


# ── Helpers ──────────────────────────────────────────────────────────────


@pytest.fixture
def gate_mode():
    original = (app.config.settings.proof_gate_mode, app.config.settings.proof_gate_enforce_after)

    def _set(mode: str, enforce_after: str = ""):
        app.config.settings.proof_gate_mode = mode
        app.config.settings.proof_gate_enforce_after = enforce_after

    yield _set
    app.config.settings.proof_gate_mode, app.config.settings.proof_gate_enforce_after = original


def _pr(*, sha=SHA, ci="SUCCESS", commit_at=None, reviews=None, comments=None, branch="feat/x",
        state="OPEN", author="fleet-bot"):
    commit_at = commit_at or (utcnow() - timedelta(minutes=5))
    rollup = [] if ci is None else [
        {"__typename": "CheckRun", "name": "backend", "status": "COMPLETED", "conclusion": ci},
    ]
    return {
        "number": 42,
        "state": state,
        "author": {"login": author},
        "url": PR_URL,
        "headRefName": branch,
        "headRefOid": sha,
        "statusCheckRollup": rollup,
        "commits": [{"oid": sha, "committedDate": commit_at.strftime("%Y-%m-%dT%H:%M:%SZ")}],
        "reviews": reviews or [],
        "comments": comments or [],
    }


_BUCKET = {"SUCCESS": "pass", "NEUTRAL": "pass", "SKIPPED": "skipping",
           "FAILURE": "fail", "CANCELLED": "cancel", "TIMED_OUT": "fail"}


def _gh(pr: dict | None = None, *, rc: int = 0, err: str = "", required=None,
        repo_visible: bool = True, compare_files: int | None = 3, hang: bool = False):
    """Fake `gh`: answers `pr view` with `pr`, `pr checks --required` with
    `required` (default: every rollup check counts as required), `api
    repos/<o>/<r>` with the repo visibility and `api .../compare/...` with
    `compare_files` changed files."""
    import asyncio as _asyncio

    calls = []

    async def fake(*args, env=None, timeout=None):
        calls.append((args, env))
        if hang:
            raise _asyncio.TimeoutError()
        if args[1] == "api":
            if "/compare/" in args[2]:
                return (0, str(compare_files), "") if compare_files is not None else (1, "", "boom")
            return (0, "example-org/example-repo", "") if repo_visible else (1, "", "HTTP 404: Not Found")
        if rc:
            return rc, "", err
        if args[2] == "checks":
            if required == "none-configured":
                return 1, "", "no required checks reported on the 'main' branch"
            checks = required if required is not None else [
                {"name": c["name"], "bucket": _BUCKET.get(c.get("conclusion") or "", "pending")}
                for c in pr.get("statusCheckRollup") or []
            ]
            return 0, json.dumps(checks), ""
        return 0, json.dumps(pr), ""

    fake.calls = calls
    return fake


async def _seed(*, status="in_progress", pr_url=PR_URL, review_decision=None,
                decided_at=None, assignee="dev", branch_name=None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskComment

    board = Board(id=uuid.uuid4(), name="Proof Board", slug=f"pg-{uuid.uuid4().hex[:8]}")
    dev_tok, dev_hash = generate_agent_token()
    rev_tok, rev_hash = generate_agent_token()
    dev = Agent(id=uuid.uuid4(), name=f"Cody-{uuid.uuid4().hex[:4]}", role="developer",
                board_id=board.id, agent_token_hash=dev_hash,
                scopes=["tasks:read", "tasks:write", "tasks:create"])
    rev = Agent(id=uuid.uuid4(), name=f"Rex-{uuid.uuid4().hex[:4]}", role="reviewer",
                board_id=board.id, agent_token_hash=rev_hash,
                scopes=["tasks:read", "tasks:write"])
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Feature mit PR", status=status,
        assigned_agent_id=dev.id if assignee == "dev" else rev.id,
        pr_url=pr_url, review_decision=review_decision, review_decided_at=decided_at,
        branch_name=branch_name,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add_all([board, dev, rev, task])
        await s.commit()
        s.add(TaskComment(task_id=task.id, author_type="agent", author_agent_id=dev.id,
                          comment_type="progress", content="Implementierung fertig"))
        s.add(TaskComment(task_id=task.id, author_type="agent", author_agent_id=dev.id,
                          comment_type="reflection", content=_REFLECTION))
        await s.commit()
    return {"board": board, "dev": dev, "rev": rev, "task": task,
            "dev_tok": dev_tok, "rev_tok": rev_tok}


def _silence():
    return (
        patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock),
        patch("app.services.adr_gate.guard_adr_merge", new_callable=AsyncMock),
    )


async def _patch(client, data, body, *, token="dev_tok", gh=None, token_value="tok",
                 loud_only=True):
    events = AsyncMock()
    ctx = _silence()
    with ctx[0], ctx[1], ctx[2], ctx[3], \
            patch.object(proof_gate, "_github_token", new=AsyncMock(return_value=token_value)), \
            patch("app.services.pr_merge_monitor._run_gh", new=gh or _gh(_pr())), \
            patch("app.services.activity.emit_event", new=events):
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json=body,
            headers={"Authorization": f"Bearer {data[token]}"},
        )
    gate_events = [c for c in events.await_args_list if c.args[1] == proof_gate.PROOF_GATE_EVENT]
    # a clean checked move records an `info` event (the server's own note of
    # the PR head); only `warning` events are findings
    if not loud_only:
        return resp, gate_events
    return resp, [c for c in gate_events if c.kwargs.get("severity") == "warning"]


async def _state(task_id):
    from app.models.task import Task, TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id,
                                      TaskComment.content.like("%Beweis-Tor%"))
        )).all()
    return task, comments


def _codes(resp):
    return {f["code"] for f in resp.json()["detail"]["findings"]}


# ── The clean case ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "enforce"])
async def test_clean_case_passes_without_any_warning(client, fake_redis, gate_mode, mode):
    gate_mode(mode)
    data = await _seed()
    gh = _gh(_pr())
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA,
                                               "head_branch": "feat/x"}, gh=gh)
    assert resp.status_code == 200, resp.text
    task, comments = await _state(data["task"].id)
    assert task.status == "review"
    assert comments == [] and events == []
    # the gate really asked GitHub — with the resolved token, not the process env
    assert [c[0][2] for c in gh.calls] == ["view", "checks"]
    args, env = gh.calls[0]
    assert "--repo" in args and "example-org/example-repo" in args
    assert env["GH_TOKEN"] == "tok"


# ── Sabotage probes ──────────────────────────────────────────────────────


SABOTAGE = {
    "pr_not_found": dict(gh=lambda: _gh(rc=1, err="GraphQL: Could not resolve to a PullRequest with the number of 42.")),
    "ci_red": dict(gh=lambda: _gh(_pr(ci="FAILURE"))),
    "sha_mismatch": dict(gh=lambda: _gh(_pr(sha=OTHER_SHA))),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(SABOTAGE))
async def test_sabotage_warn_allows_but_marks_card(client, fake_redis, gate_mode, code):
    gate_mode("warn")
    data = await _seed()
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA,
                                               "head_branch": "feat/x"},
                                gh=SABOTAGE[code]["gh"]())
    assert resp.status_code == 200, resp.text
    task, comments = await _state(data["task"].id)
    assert task.status == "review"
    assert len(comments) == 1 and code in comments[0].content
    assert len(events) == 1
    detail = events[0].kwargs["detail"]
    assert detail["gefangen_von"] == "tor" and detail["blocked"] is False
    assert code in {f["code"] for f in detail["findings"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(SABOTAGE))
async def test_sabotage_enforce_refuses_with_422(client, fake_redis, gate_mode, code):
    gate_mode("enforce")
    data = await _seed()
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA,
                                               "head_branch": "feat/x"},
                                gh=SABOTAGE[code]["gh"]())
    assert resp.status_code == 422, resp.text
    assert code in _codes(resp)
    task, comments = await _state(data["task"].id)
    assert task.status == "in_progress", "422 darf den Status nicht gesetzt haben"
    assert len(comments) == 1 and "abgelehnt" in comments[0].content
    assert events and events[0].kwargs["detail"]["blocked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("warn", 200), ("enforce", 422)])
async def test_no_commit_after_change_request(client, fake_redis, gate_mode, mode, expected):
    gate_mode(mode)
    decided = utcnow() - timedelta(hours=1)
    data = await _seed(review_decision="changes_requested", decided_at=decided)
    old_commit = decided - timedelta(hours=2)
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA},
                                gh=_gh(_pr(commit_at=old_commit)))
    assert resp.status_code == expected, resp.text
    assert events and "no_commit_after_change" in {
        f["code"] for f in events[0].kwargs["detail"]["findings"]}


@pytest.mark.asyncio
async def test_github_change_request_also_counts(client, fake_redis, gate_mode):
    """Mark's "Request changes" on GitHub is feedback too, not only MC's."""
    gate_mode("enforce")
    data = await _seed()
    review_at = utcnow() - timedelta(minutes=10)
    pr = _pr(commit_at=review_at - timedelta(hours=1), reviews=[
        {"author": {"login": "operator"}, "state": "CHANGES_REQUESTED",
         "submittedAt": review_at.strftime("%Y-%m-%dT%H:%M:%SZ")}])
    resp, _ = await _patch(client, data, {"status": "review", "head_sha": SHA}, gh=_gh(pr))
    assert resp.status_code == 422 and "no_commit_after_change" in _codes(resp)


@pytest.mark.asyncio
async def test_commit_after_change_request_passes(client, fake_redis, gate_mode):
    gate_mode("enforce")
    decided = utcnow() - timedelta(hours=1)
    data = await _seed(review_decision="changes_requested", decided_at=decided)
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA},
                                gh=_gh(_pr(commit_at=decided + timedelta(minutes=30))))
    assert resp.status_code == 200, resp.text
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("warn", 200), ("enforce", 422)])
async def test_reviewer_closes_without_own_comment(client, fake_redis, gate_mode, mode, expected):
    gate_mode(mode)
    data = await _seed(status="review", assignee="rev")
    resp, events = await _patch(client, data, {"status": "done"}, token="rev_tok")
    assert resp.status_code == expected, resp.text
    assert "reviewer_silent" in {f["code"] for f in events[0].kwargs["detail"]["findings"]}
    task, _ = await _state(data["task"].id)
    if mode == "enforce":
        assert task.status == "review"
        # the review_decision fallback (approved) was staged before the gate —
        # the refusal must roll it back, not leave a half-approved card
        assert task.review_decision != "approved"


@pytest.mark.asyncio
async def test_reviewer_with_own_comment_passes(client, fake_redis, gate_mode):
    from app.models.task import TaskComment

    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(task_id=data["task"].id, author_type="agent",
                          author_agent_id=data["rev"].id, comment_type="review",
                          content="Geprueft: Tests gruen, Diff sauber."))
        await s.commit()
    resp, events = await _patch(client, data, {"status": "done"}, token="rev_tok")
    assert resp.status_code == 200, resp.text
    assert events == []


@pytest.mark.asyncio
async def test_named_reviewer_must_have_commented(client, fake_redis, gate_mode):
    """`reviewer` names an MC agent who never wrote on the card or the PR."""
    gate_mode("enforce")
    data = await _seed()
    resp, _ = await _patch(client, data, {"status": "review", "head_sha": SHA,
                                          "reviewer": data["rev"].name})
    assert resp.status_code == 422 and "reviewer_silent" in _codes(resp)


@pytest.mark.asyncio
async def test_named_reviewer_found_on_github(client, fake_redis, gate_mode):
    """Not an MC agent → the GitHub review/comment authors are the evidence."""
    gate_mode("enforce")
    data = await _seed()
    pr = _pr(comments=[{"author": {"login": "outside-reviewer"}, "body": "LGTM",
                        "createdAt": "2026-10-19T10:00:00Z"}])
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA,
                                               "reviewer": "@Outside-Reviewer"}, gh=_gh(pr))
    assert resp.status_code == 200, resp.text
    assert events == []


@pytest.mark.asyncio
async def test_invalid_pr_url_is_a_hard_finding(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(pr_url="https://gitlab.example/x/y/-/merge_requests/1")
    resp, _ = await _patch(client, data, {"status": "review"})
    assert resp.status_code == 422 and "pr_url_invalid" in _codes(resp)


@pytest.mark.asyncio
async def test_pr_url_given_in_the_same_patch_is_checked(client, fake_redis, gate_mode):
    """The claim arrives with the transition (mc review --pr-url …) — checked too."""
    gate_mode("enforce")
    data = await _seed(pr_url=None)
    resp, _ = await _patch(client, data, {"status": "review", "pr_url": PR_URL},
                           gh=_gh(rc=1, err="HTTP 404: Not Found"))
    assert resp.status_code == 422 and "pr_not_found" in _codes(resp)


# ── Fail-open, loud ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["warn", "enforce"])
async def test_missing_token_fails_open_with_visible_warning(client, fake_redis, gate_mode, mode):
    gate_mode(mode)
    data = await _seed()
    gh = _gh(_pr(ci="FAILURE"))
    resp, events = await _patch(client, data, {"status": "review"}, gh=gh, token_value="")
    assert resp.status_code == 200, resp.text
    assert gh.calls == [], "ohne Token darf gh nicht gerufen werden"
    task, comments = await _state(data["task"].id)
    assert task.status == "review"
    assert len(comments) == 1 and "no_github_token" in comments[0].content
    assert events and events[0].kwargs["detail"]["blocked"] is False


@pytest.mark.asyncio
async def test_github_outage_fails_open(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed()
    resp, events = await _patch(client, data, {"status": "review"},
                                gh=_gh(rc=1, err="error connecting to api.github.com"))
    assert resp.status_code == 200, resp.text
    assert "github_unavailable" in {f["code"] for f in events[0].kwargs["detail"]["findings"]}


# ── No bypass ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_server_side_force_switch(client, fake_redis, gate_mode):
    """A client cannot send its way around the gate: unknown fields are refused."""
    gate_mode("enforce")
    data = await _seed()
    for extra in ({"force": True}, {"proof_gate": "off"}, {"skip_proof_gate": True}):
        resp, _ = await _patch(client, data, {"status": "review", **extra},
                               gh=_gh(_pr(ci="FAILURE")))
        assert resp.status_code == 422, resp.text
        assert "extra" in resp.text.lower() or "forbidden" in resp.text.lower()
    task, _ = await _state(data["task"].id)
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_off_mode_does_not_call_github(client, fake_redis, gate_mode):
    gate_mode("off")
    data = await _seed()
    gh = _gh(_pr(ci="FAILURE"))
    resp, events = await _patch(client, data, {"status": "review"}, gh=gh)
    assert resp.status_code == 200 and gh.calls == [] and events == []


@pytest.mark.asyncio
async def test_card_without_pr_is_not_gated(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(pr_url=None)
    gh = _gh(_pr(ci="FAILURE"))
    resp, events = await _patch(client, data, {"status": "review"}, gh=gh)
    assert resp.status_code == 200 and gh.calls == [] and events == []


@pytest.mark.asyncio
async def test_review_approve_endpoint_is_gated_too(client, fake_redis, gate_mode):
    """POST /review approve → done is the other road to done."""
    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    ctx = _silence()
    with ctx[0], ctx[1], ctx[2], ctx[3], \
            patch.object(proof_gate, "_github_token", new=AsyncMock(return_value="tok")), \
            patch("app.services.pr_merge_monitor._run_gh", new=_gh(_pr(ci="FAILURE"))), \
            patch("app.services.activity.emit_event", new=AsyncMock()):
        resp = await client.post(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}/review",
            json={"decision": "approve", "comment": "passt"},
            headers={"Authorization": f"Bearer {data['rev_tok']}"},
        )
    assert resp.status_code == 422, resp.text
    task, _ = await _state(data["task"].id)
    assert task.status == "review"


# ── Mode + gefangen_von ──────────────────────────────────────────────────


def test_warn_flips_to_enforce_on_the_configured_date(gate_mode):
    gate_mode("warn", "2026-10-26")
    app.config.settings.proof_gate_enforce_min_checks = 0
    try:
        assert proof_gate.effective_mode(date(2026, 10, 25)) == "warn"
        assert proof_gate.effective_mode(date(2026, 10, 26)) == "enforce"
    finally:
        app.config.settings.proof_gate_enforce_min_checks = 15
    gate_mode("warn", "kein-datum")
    assert proof_gate.effective_mode(date(2030, 1, 1)) == "warn"
    gate_mode("bogus")
    assert proof_gate.effective_mode() == "warn"


def test_ci_classification():
    assert proof_gate.classify_ci([]) == "none"
    assert proof_gate.classify_ci([{"status": "COMPLETED", "conclusion": "SUCCESS"},
                                   {"state": "SUCCESS"}]) == "green"
    assert proof_gate.classify_ci([{"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert proof_gate.classify_ci([{"state": "PENDING"}]) == "pending"
    assert proof_gate.classify_ci([{"status": "COMPLETED", "conclusion": "SUCCESS"},
                                   {"status": "COMPLETED", "conclusion": "TIMED_OUT"}]) == "red"
    assert proof_gate.classify_ci([{"state": "ERROR"}]) == "red"


def test_caught_by_for_actor():
    class A:
        def __init__(self, lead):
            self.is_board_lead = lead

    assert proof_gate.caught_by_for_actor(None) == "mark"
    assert proof_gate.caught_by_for_actor(A(True)) == "boss"
    assert proof_gate.caught_by_for_actor(A(False)) == "rolle"
    assert set(proof_gate.CAUGHT_BY_VALUES) == {"tor", "rolle", "boss", "mark", "live"}


@pytest.mark.asyncio
async def test_review_rejection_records_who_caught_it(client, fake_redis, gate_mode):
    """`gefangen_von` on task.review_rejected: an agent reviewer → "rolle"."""
    gate_mode("off")
    data = await _seed(status="review", assignee="rev")
    events = AsyncMock()
    ctx = _silence()
    with ctx[0], ctx[1], ctx[2], ctx[3], \
            patch("app.services.task_lifecycle.emit_event", new=events), \
            patch("app.services.dispatch.auto_dispatch_task", new=AsyncMock()):
        resp = await client.post(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}/review",
            json={"decision": "request_changes", "comment": "CI ist rot, bitte fixen"},
            headers={"Authorization": f"Bearer {data['rev_tok']}"},
        )
    assert resp.status_code == 200, resp.text
    rejected = [c for c in events.await_args_list if c.args[1] == "task.review_rejected"]
    assert rejected, [c.args[1] for c in events.await_args_list]
    assert rejected[0].kwargs["detail"]["gefangen_von"] == "rolle"


@pytest.mark.asyncio
async def test_failing_non_required_check_does_not_count(client, fake_redis, gate_mode):
    """Live finding 2026-10-19: "kz check (warning only)" is red even on merged
    PRs of this repo. Green = the REQUIRED checks are green."""
    gate_mode("enforce")
    data = await _seed()
    pr = _pr()
    pr["statusCheckRollup"].append(
        {"name": "kz check (warning only)", "status": "COMPLETED", "conclusion": "FAILURE"})
    gh = _gh(pr, required=[{"name": "backend", "bucket": "pass"}])
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA,
                                               "head_branch": "feat/x"}, gh=gh)
    assert resp.status_code == 200, resp.text
    assert events == []


@pytest.mark.asyncio
async def test_repo_without_required_checks_falls_back_to_rollup(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed()
    resp, _ = await _patch(client, data, {"status": "review"},
                           gh=_gh(_pr(ci="FAILURE"), required="none-configured"))
    assert resp.status_code == 422 and "ci_red" in _codes(resp)


def test_required_classification():
    assert proof_gate.classify_required([]) == "none"
    assert proof_gate.classify_required([{"bucket": "pass"}, {"bucket": "skipping"}]) == "green"
    assert proof_gate.classify_required([{"bucket": "pass"}, {"bucket": "pending"}]) == "pending"
    assert proof_gate.classify_required([{"bucket": "pending"}, {"bucket": "fail"}]) == "red"


# ── Critic round 1 (2026-10-19): the gate fetches its own evidence ──────


async def _add_snapshot(task_id, board_id, *, head_sha, at, target="review", blocked=False):
    """A recorded checked move, as `publish` writes it."""
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(
            event_type=proof_gate.PROOF_GATE_EVENT, title="snap", severity="info",
            board_id=board_id, task_id=task_id, created_at=at,
            detail={"gefangen_von": "tor", "target_status": target, "blocked": blocked,
                    "head_sha": head_sha, "findings": []},
        ))
        await s.commit()


async def _add_comment(task_id, agent_id, content, ctype="message"):
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(task_id=task_id, author_type="agent", author_agent_id=agent_id,
                          comment_type=ctype, content=content))
        await s.commit()


@pytest.mark.asyncio
async def test_merged_foreign_pr_does_not_prove_a_review(client, fake_redis, gate_mode):
    """Befund 1: a merged, green PR (e.g. #864) used to pass (a) and (c)."""
    gate_mode("enforce")
    data = await _seed()
    resp, _ = await _patch(client, data, {"status": "review"}, gh=_gh(_pr(state="MERGED")))
    assert resp.status_code == 422 and "pr_not_open" in _codes(resp)


@pytest.mark.asyncio
async def test_closed_pr_does_not_prove_done(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed()
    resp, _ = await _patch(client, data, {"status": "done"}, gh=_gh(_pr(state="CLOSED")))
    assert resp.status_code == 422 and "pr_not_open" in _codes(resp)


@pytest.mark.asyncio
async def test_pr_must_run_on_the_cards_branch(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(branch_name="feat/my-card")
    resp, _ = await _patch(client, data, {"status": "review"}, gh=_gh(_pr(branch="feat/other")))
    assert resp.status_code == 422 and "pr_branch_mismatch" in _codes(resp)


@pytest.mark.asyncio
async def test_pr_already_held_by_unrelated_card(client, fake_redis, gate_mode):
    from app.models.task import Task

    gate_mode("enforce")
    data = await _seed()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=uuid.uuid4(), board_id=data["board"].id, title="Fremde Karte",
                   status="done", pr_url=PR_URL))
        await s.commit()
    resp, _ = await _patch(client, data, {"status": "review"})
    assert resp.status_code == 422 and "pr_on_other_card" in _codes(resp)


@pytest.mark.asyncio
async def test_review_card_shares_the_pr_of_its_source(client, fake_redis, gate_mode):
    """Counter-probe: the review card of THIS card carries the same PR by design."""
    from app.models.task import Task

    gate_mode("enforce")
    data = await _seed()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=uuid.uuid4(), board_id=data["board"].id, title="Review: Feature",
                   status="inbox", pr_url=PR_URL, source_task_id=data["task"].id))
        await s.commit()
    resp, events = await _patch(client, data, {"status": "review", "head_sha": SHA})
    assert resp.status_code == 200, resp.text
    assert events == []


@pytest.mark.asyncio
async def test_clean_move_records_the_pr_head_itself(client, fake_redis, gate_mode):
    """Befund 2: the server notes the head it saw — no agent report needed."""
    gate_mode("warn")
    data = await _seed()
    resp, events = await _patch(client, data, {"status": "review"}, loud_only=False)
    assert resp.status_code == 200, resp.text
    assert len(events) == 1 and events[0].kwargs["severity"] == "info"
    detail = events[0].kwargs["detail"]
    assert detail["head_sha"] == SHA and detail["pr_state"] == "OPEN"
    assert detail["target_status"] == "review" and detail["findings"] == [] or all(
        f["level"] == "info" for f in detail["findings"])


@pytest.mark.asyncio
async def test_unchanged_head_after_change_request_despite_fake_commit_date(
        client, fake_redis, gate_mode):
    """Befund 2+12: no head_sha reported, commit date faked into the future —
    the server's own record of the head shows nothing changed."""
    gate_mode("enforce")
    decided = utcnow() - timedelta(hours=1)
    data = await _seed(review_decision="changes_requested", decided_at=decided)
    await _add_snapshot(data["task"].id, data["board"].id, head_sha=SHA,
                        at=decided - timedelta(hours=2))
    resp, _ = await _patch(client, data, {"status": "review"},
                           gh=_gh(_pr(commit_at=utcnow() + timedelta(days=1))))
    assert resp.status_code == 422 and "no_commit_after_change" in _codes(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("files,expected", [(0, 422), (3, 200)])
async def test_empty_commit_after_change_request(client, fake_redis, gate_mode, files, expected):
    """Befund 12: `git commit --allow-empty` moves the SHA but changes nothing."""
    gate_mode("enforce")
    decided = utcnow() - timedelta(hours=1)
    data = await _seed(review_decision="changes_requested", decided_at=decided)
    await _add_snapshot(data["task"].id, data["board"].id, head_sha=OTHER_SHA,
                        at=decided - timedelta(hours=2))
    gh = _gh(_pr(), compare_files=files)
    resp, _ = await _patch(client, data, {"status": "review"}, gh=gh)
    assert resp.status_code == expected, resp.text
    if expected == 422:
        assert "no_commit_after_change" in _codes(resp)
    compare = [c for c in gh.calls if c[0][1] == "api" and "/compare/" in c[0][2]]
    assert compare and compare[0][0][2].endswith(f"{OTHER_SHA}...{SHA}")


@pytest.mark.asyncio
async def test_stale_reviewer_checkout_is_no_sha_mismatch(client, fake_redis, gate_mode):
    """Befund 9: at review→done the reviewer's local HEAD is not compared."""
    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, "Geprueft: passt.", "review")
    resp, _ = await _patch(client, data, {"status": "done", "head_sha": OTHER_SHA,
                                          "head_branch": "feat/x"}, token="rev_tok")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_reflection_is_not_a_reviewers_verdict(client, fake_redis, gate_mode):
    """Befund 3: `mc finish` writes a reflection right before its PATCH."""
    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, _REFLECTION, "reflection")
    resp, _ = await _patch(client, data, {"status": "done"}, token="rev_tok")
    assert resp.status_code == 422 and "reviewer_silent" in _codes(resp)


@pytest.mark.asyncio
async def test_developer_cannot_close_his_own_review(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(status="review")
    await _add_comment(data["task"].id, data["dev"].id, "Sieht gut aus.", "review")
    resp, _ = await _patch(client, data, {"status": "done"})
    assert resp.status_code == 422 and "self_review" in _codes(resp)


@pytest.mark.asyncio
async def test_developer_cannot_name_himself_as_reviewer(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed()
    resp, _ = await _patch(client, data, {"status": "review", "reviewer": data["dev"].name})
    assert resp.status_code == 422 and "reviewer_is_developer" in _codes(resp)


@pytest.mark.asyncio
async def test_pr_author_login_is_no_reviewer(client, fake_redis, gate_mode):
    """Befund 4 (verified live: every fleet PR is authored by one shared
    account) — the author's own comment on GitHub proves no review."""
    gate_mode("enforce")
    data = await _seed()
    pr = _pr(author="fleet-bot", comments=[{"author": {"login": "fleet-bot"}, "body": "LGTM"}])
    resp, _ = await _patch(client, data, {"status": "review", "reviewer": "fleet-bot"}, gh=_gh(pr))
    assert resp.status_code == 422 and "reviewer_silent" in _codes(resp)


@pytest.mark.asyncio
async def test_hung_gh_fails_open_instead_of_hanging(client, fake_redis, gate_mode):
    """Befund 6: a gh timeout is an outage → allowed, loud."""
    gate_mode("enforce")
    data = await _seed()
    resp, events = await _patch(client, data, {"status": "review"}, gh=_gh(_pr(), hang=True))
    assert resp.status_code == 200, resp.text
    assert "github_unavailable" in {f["code"] for f in events[0].kwargs["detail"]["findings"]}


@pytest.mark.asyncio
async def test_run_gh_kills_a_hung_process():
    """Befund 6, the real subprocess: `_run_gh` gives up after `timeout`."""
    import asyncio
    import time

    from app.services import pr_merge_monitor

    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await pr_merge_monitor._run_gh("sleep", "5", timeout=0.3)
    assert time.monotonic() - start < 3


@pytest.mark.asyncio
@pytest.mark.parametrize("err", ["HTTP 404: Not Found (https://api.github.com/graphql)",
                                 "GraphQL: Could not resolve to a Repository with the name 'x/y'."])
async def test_repo_the_token_cannot_see_is_no_false_claim(client, fake_redis, gate_mode, err):
    """Befund 7: GitHub answers 404 for a private repo without access."""
    gate_mode("enforce")
    data = await _seed()
    resp, events = await _patch(client, data, {"status": "review"},
                                gh=_gh(rc=1, err=err, repo_visible=False))
    assert resp.status_code == 200, resp.text
    assert "repo_not_visible" in {f["code"] for f in events[0].kwargs["detail"]["findings"]}


@pytest.mark.asyncio
async def test_refused_approve_keeps_the_reviewers_verdict(client, fake_redis, gate_mode):
    """Befund 8: CI still running → 422, but the verdict text survives."""
    from app.models.task import TaskComment

    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    pr = _pr()
    pr["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion="")
    ctx = _silence()
    with ctx[0], ctx[1], ctx[2], ctx[3], \
            patch.object(proof_gate, "_github_token", new=AsyncMock(return_value="tok")), \
            patch("app.services.pr_merge_monitor._run_gh", new=_gh(pr, required=[{"name": "b", "bucket": "pending"}])), \
            patch("app.services.activity.emit_event", new=AsyncMock()):
        resp = await client.post(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}/review",
            json={"decision": "approve", "comment": "Diff gelesen, Tests decken alles ab."},
            headers={"Authorization": f"Bearer {data['rev_tok']}"},
        )
    assert resp.status_code == 422 and "ci_pending" in _codes(resp)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        kept = (await s.exec(select(TaskComment).where(
            TaskComment.task_id == data["task"].id,
            TaskComment.author_agent_id == data["rev"].id))).all()
    assert any("Tests decken alles ab" in c.content and c.comment_type == "review" for c in kept)
    task, _ = await _state(data["task"].id)
    assert task.status == "review"


def test_auto_flip_waits_for_enough_checks(gate_mode):
    """Befund 10: a date alone is no sample."""
    gate_mode("warn", "2026-10-26")
    day = date(2026, 11, 2)
    assert proof_gate.effective_mode(day, checks_so_far=None) == "warn"
    assert proof_gate.effective_mode(day, checks_so_far=14) == "warn"
    assert proof_gate.effective_mode(day, checks_so_far=15) == "enforce"
    assert proof_gate.effective_mode(date(2026, 10, 1), checks_so_far=99) == "warn"


@pytest.mark.asyncio
async def test_repeated_refusals_do_not_flood_the_card(client, fake_redis, gate_mode):
    """Befund 11: a looping agent gets one comment, not one per attempt."""
    gate_mode("enforce")
    data = await _seed()
    for _ in range(3):
        resp, _ = await _patch(client, data, {"status": "review"}, gh=_gh(_pr(ci="FAILURE")))
        assert resp.status_code == 422
    _, comments = await _state(data["task"].id)
    assert len(comments) == 1


@pytest.mark.asyncio
async def test_pr_claimed_in_comment_must_exist(client, fake_redis, gate_mode):
    """Befund 5: the costly pattern is the TEXT "PR open, CI green" — also on
    a card that carries no pr_url."""
    gate_mode("enforce")
    data = await _seed(pr_url=None)
    await _add_comment(data["task"].id, data["dev"].id,
                       "Fertig. PR offen: https://github.com/example-org/example-repo/pull/77 — CI gruen.")
    resp, _ = await _patch(client, data, {"status": "review"},
                           gh=_gh(rc=1, err="GraphQL: Could not resolve to a PullRequest with the number of 77."))
    assert resp.status_code == 422 and "claimed_pr_not_found" in _codes(resp)


@pytest.mark.asyncio
async def test_pr_claimed_in_comment_that_exists_passes(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(pr_url=None)
    await _add_comment(data["task"].id, data["dev"].id,
                       "PR: https://github.com/example-org/example-repo/pull/42")
    resp, events = await _patch(client, data, {"status": "review"})
    assert resp.status_code == 200, resp.text
    assert events == []
