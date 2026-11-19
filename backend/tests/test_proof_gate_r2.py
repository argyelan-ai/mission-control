"""Beweis-Tor round 2: the gate finds its own evidence, an agent that sends
nothing along is not "unchecked".

Loopholes closed here (critic round 1 on the gate), each with a
sabotage probe:

  b1  a card without pr_url — the server finds the PR itself (branch on
      GitHub, the "PR erstellt:" marker, the source card of a review card)
  b2  a code card without any PR is itself a finding (``pr_missing``)
  b3  a PR without a single changed file proves nothing (``pr_empty``)
  d1  after a change request: an empty commit, a merge from main ("Update
      branch") or a force-push of the same content is no change — the
      server compares the PR's own diff (fingerprint), not commit dates
  d2  same without a recorded fingerprint: merge and empty commits are
      skipped when walking the new commits
  e1  in_progress → done by the developer himself, without any review, on
      a PR card is refused (it used to be a notice)
  e2  a progress note is no reviewer's verdict
  e3  the developer is known even when the activity log cannot name him
  s   ``gefangen_von`` is counted (``caught_by_stats`` + GET endpoint)
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import proof_gate
from app.utils import utcnow
from tests.conftest import test_engine
from tests.test_proof_gate import (  # noqa: F401 — fixtures are used by name
    OTHER_SHA, PR_URL, SHA, _add_comment, _add_snapshot, _codes, _pr, _seed, _silence,
    gate_mode,
)

REPO = "example-org/example-repo"


def _fake_gh(pr: dict | None = None, *, pr_list=None, files=None, compare=None,
             commit_files=None, trees=None):
    """Fake ``gh`` for round 2.

    pr_list:      what ``gh pr list --head <branch>`` returns (list of dicts)
    files:        the PR's files as ``[(filename, blob_sha, status)]`` (None = call fails)
    compare:      ``{"files": n, "commits": [(sha, parents)]}`` for ``compare/a...b``
    commit_files: ``{sha: n}`` changed files per single commit
    trees:        ``{sha: tree_sha}`` for ``commits/<sha> --jq .commit.tree.sha``
    """
    calls = []

    async def fake(*args, env=None, timeout=None):
        calls.append(args)
        sub = args[1]
        if sub == "api":
            path = args[2]
            if "/pulls/" in path and path.endswith("/files"):
                if files is None:
                    return 1, "", "boom"
                return 0, "\n".join(f"{n} {s} {st}" for n, s, st in files), ""
            if "/compare/" in path:
                if compare is None:
                    return 1, "", "boom"
                return 0, json.dumps({
                    "status": compare.get("status", "ahead"),
                    "files": compare["files"],
                    "commits": [{"sha": s, "p": p} for s, p in compare["commits"]],
                }), ""
            if "/commits/" in path:
                sha = path.rsplit("/", 1)[1]
                if ".commit.tree.sha" in args:
                    return 0, (trees or {}).get(sha, "t" + sha[:39]), ""
                return 0, str((commit_files or {}).get(sha, 0)), ""
            return 0, REPO, ""  # repo visible
        if args[2] == "list":
            return 0, json.dumps(pr_list or []), ""
        if args[2] == "checks":
            return 0, json.dumps([{"name": "backend", "bucket": "pass"}]), ""
        if pr is None:
            return 1, "", "GraphQL: Could not resolve to a PullRequest with the number of 42."
        return 0, json.dumps(pr), ""

    fake.calls = calls
    return fake


async def _call(client, data, body, gh, *, token="dev_tok", path="", method="patch",
                gh_token="tok"):
    events = AsyncMock()
    ctx = _silence()
    with ctx[0], ctx[1], ctx[2], ctx[3], \
            patch.object(proof_gate, "_github_token", new=AsyncMock(return_value=gh_token)), \
            patch("app.services.pr_merge_monitor._run_gh", new=gh), \
            patch("app.services.activity.emit_event", new=events):
        url = f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}{path}"
        resp = await getattr(client, method)(
            url, json=body, headers={"Authorization": f"Bearer {data[token]}"})
    gate = [c for c in events.await_args_list if c.args[1] == proof_gate.PROOF_GATE_EVENT]
    return resp, gate


async def _set(task_id, **fields):
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        for key, value in fields.items():
            setattr(task, key, value)
        s.add(task)
        await s.commit()


async def _repo(board_id) -> uuid.UUID:
    from app.models.repo import Repo

    full = f"{REPO}-{uuid.uuid4().hex[:6]}"
    repo = Repo(id=uuid.uuid4(), full_name=full, url=f"https://github.com/{full}")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(repo)
        await s.commit()
    return repo.id


def _all_codes(events) -> set[str]:
    return {f["code"] for c in events for f in c.kwargs["detail"]["findings"]}


_FILES = [("app.py", "1" * 40, "modified"), ("test_app.py", "2" * 40, "added")]


# ── b1: the server finds the PR itself ──────────────────────────────────


@pytest.mark.asyncio
async def test_pr_found_by_branch_when_the_card_has_no_pr_url(client, fake_redis, gate_mode):
    """Sabotage: the agent leaves pr_url away — the server still finds the PR
    on the card's branch and checks it (here: red CI)."""
    gate_mode("enforce")
    data = await _seed(pr_url=None, branch_name="feat/x")
    await _set(data["task"].id, repo_id=await _repo(data["board"].id))
    pr = _pr(ci="FAILURE")
    gh = _fake_gh(pr, pr_list=[{"number": 42, "url": PR_URL}], files=_FILES)
    gh_red = gh

    async def red_checks(*args, env=None, timeout=None):
        if args[1] == "pr" and args[2] == "checks":
            return 0, json.dumps([{"name": "backend", "bucket": "fail"}]), ""
        return await gh_red(*args, env=env, timeout=timeout)

    resp, _ = await _call(client, data, {"status": "done"}, red_checks)
    assert resp.status_code == 422, resp.text
    codes = _codes(resp)
    assert "ci_red" in codes
    listed = [c for c in gh.calls if c[1] == "pr" and c[2] == "list"]
    assert listed and "feat/x" in listed[0]


@pytest.mark.asyncio
async def test_pr_found_by_branch_is_named_on_the_card(client, fake_redis, gate_mode):
    gate_mode("warn")
    data = await _seed(pr_url=None, branch_name="feat/x", status="review", assignee="rev")
    await _set(data["task"].id, repo_id=await _repo(data["board"].id))
    await _add_comment(data["task"].id, data["rev"].id, "Geprueft, passt.", "review")
    gh = _fake_gh(_pr(), pr_list=[{"number": 42, "url": PR_URL}], files=_FILES)
    resp, events = await _call(client, data, {"status": "done"}, gh, token="rev_tok")
    assert resp.status_code == 200, resp.text
    assert "pr_not_on_card" in _all_codes(events)
    assert events[-1].kwargs["detail"]["pr_url"] == PR_URL


@pytest.mark.asyncio
async def test_pr_found_by_creation_marker(client, fake_redis, gate_mode):
    """The project path writes "**PR erstellt:** <url>" — that is task data."""
    from app.models.task import TaskComment

    gate_mode("enforce")
    data = await _seed(pr_url=None)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(task_id=data["task"].id, author_type="system", comment_type="progress",
                          content=f"**PR erstellt:** {PR_URL}"))
        await s.commit()
    resp, _ = await _call(client, data, {"status": "done"},
                          _fake_gh(_pr(state="CLOSED"), files=_FILES))
    assert resp.status_code == 422 and "pr_not_open" in _codes(resp)


@pytest.mark.asyncio
async def test_review_card_uses_the_pr_of_its_source(client, fake_redis, gate_mode):
    from app.models.task import Task

    gate_mode("enforce")
    data = await _seed(pr_url=None, assignee="rev")
    source = Task(id=uuid.uuid4(), board_id=data["board"].id, title="Feature", status="review",
                  pr_url=PR_URL, assigned_agent_id=data["dev"].id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(source)
        await s.commit()
    await _set(data["task"].id, source_task_id=source.id)
    from tests.test_proof_gate import _REFLECTION
    await _add_comment(data["task"].id, data["rev"].id, _REFLECTION, "reflection")
    resp, _ = await _call(client, data, {"status": "done"},
                          _fake_gh(_pr(state="CLOSED"), files=_FILES), token="rev_tok")
    assert resp.status_code == 422, resp.text
    assert "pr_not_open" in _codes(resp)


# ── b2: a code card without any PR ──────────────────────────────────────


@pytest.mark.asyncio
async def test_code_card_without_any_pr_is_refused(client, fake_redis, gate_mode):
    """Sabotage: code_change card on a registry repo, no PR anywhere → done refused."""
    gate_mode("enforce")
    data = await _seed(pr_url=None, branch_name="feat/x")
    await _set(data["task"].id, repo_id=await _repo(data["board"].id),
               delegation_type="code_change")
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(None, pr_list=[]))
    assert resp.status_code == 422, resp.text
    assert "pr_missing" in _codes(resp)


@pytest.mark.asyncio
async def test_branch_without_pr_is_a_visible_notice(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(pr_url=None, branch_name="feat/x")
    resp, events = await _call(client, data, {"status": "done"}, _fake_gh(None, pr_list=[]))
    assert resp.status_code == 200, resp.text
    assert "pr_missing" in _all_codes(events)
    assert events[0].kwargs["severity"] == "warning"


@pytest.mark.asyncio
async def test_card_without_code_hints_stays_unchecked(client, fake_redis, gate_mode):
    """Counter-probe: a research card (no repo, no branch, no code) is no code card."""
    gate_mode("enforce")
    data = await _seed(pr_url=None)
    resp, events = await _call(client, data, {"status": "done"}, _fake_gh(None))
    assert resp.status_code == 200, resp.text
    assert "pr_missing" not in _all_codes(events)


# ── b3: an empty PR ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_without_changed_files_proves_nothing(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed()
    pr = _pr()
    pr["changedFiles"] = 0
    resp, _ = await _call(client, data, {"status": "review"}, _fake_gh(pr, files=[]))
    assert resp.status_code == 422 and "pr_empty" in _codes(resp)


# ── d: real change after a change request ───────────────────────────────


async def _changes_requested_card(*, fp: str | None):
    decided = utcnow() - timedelta(hours=1)
    data = await _seed(review_decision="changes_requested", decided_at=decided)
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        detail = {"gefangen_von": "tor", "target_status": "review", "blocked": False,
                  "head_sha": OTHER_SHA, "findings": []}
        if fp is not None:
            detail["diff_fp"] = fp
        s.add(ActivityEvent(event_type=proof_gate.PROOF_GATE_EVENT, title="snap", severity="info",
                            board_id=data["board"].id, task_id=data["task"].id,
                            created_at=decided - timedelta(hours=2), detail=detail))
        await s.commit()
    return data


@pytest.mark.asyncio
async def test_merge_from_main_after_change_request_is_no_change(client, fake_redis, gate_mode):
    """Sabotage: "Update branch" moves the head and brings files from main,
    but the PR's own diff is identical → refused."""
    gate_mode("enforce")
    data = await _changes_requested_card(fp=proof_gate.diff_fingerprint(
        [f"{n} {s} {st}" for n, s, st in _FILES]))
    gh = _fake_gh(_pr(), files=_FILES,
                  compare={"files": 12, "commits": [("m" * 40, 2)]})
    resp, _ = await _call(client, data, {"status": "review"}, gh)
    assert resp.status_code == 422, resp.text
    assert "no_commit_after_change" in _codes(resp)


@pytest.mark.asyncio
async def test_real_change_after_change_request_passes(client, fake_redis, gate_mode):
    """Counter-probe: one file of the PR got a new blob → real change."""
    gate_mode("enforce")
    data = await _changes_requested_card(fp=proof_gate.diff_fingerprint(
        [f"{n} {s} {st}" for n, s, st in _FILES]))
    changed = [("app.py", "9" * 40, "modified"), _FILES[1]]
    resp, events = await _call(client, data, {"status": "review"}, _fake_gh(_pr(), files=changed))
    assert resp.status_code == 200, resp.text
    assert "no_commit_after_change" not in _all_codes(events)


@pytest.mark.asyncio
async def test_new_snapshot_records_the_diff_fingerprint(client, fake_redis, gate_mode):
    gate_mode("warn")
    data = await _seed()
    resp, events = await _call(client, data, {"status": "review"}, _fake_gh(_pr(), files=_FILES))
    assert resp.status_code == 200, resp.text
    assert events[0].kwargs["detail"]["diff_fp"] == proof_gate.diff_fingerprint(
        [f"{n} {s} {st}" for n, s, st in _FILES])


@pytest.mark.asyncio
@pytest.mark.parametrize("commits,commit_files,expected", [
    # a merge commit's own file list is main's changes (12) — it must not count
    ([("m" * 40, 2)], {"m" * 40: 12}, 422),                              # only a merge from main
    ([("e" * 40, 1), ("m" * 40, 2)], {"e" * 40: 0, "m" * 40: 12}, 422),  # empty commit + merge
    ([("c" * 40, 1), ("m" * 40, 2)], {"c" * 40: 2, "m" * 40: 12}, 200),  # one real commit
])
async def test_commit_walk_without_fingerprint_skips_merge_and_empty(
        client, fake_redis, gate_mode, commits, commit_files, expected):
    """Older snapshot without diff_fp: the new commits are walked, merge and
    empty commits do not count, even though the compare shows 12 files."""
    gate_mode("enforce")
    data = await _changes_requested_card(fp=None)
    gh = _fake_gh(_pr(), files=None, compare={"files": 12, "commits": commits},
                  commit_files=commit_files)
    resp, _ = await _call(client, data, {"status": "review"}, gh)
    assert resp.status_code == expected, resp.text
    if expected == 422:
        assert "no_commit_after_change" in _codes(resp)


# ── e: reviewer determined on the server ────────────────────────────────


@pytest.mark.asyncio
async def test_developer_skipping_review_on_a_pr_card_is_refused(client, fake_redis, gate_mode):
    """Sabotage: in_progress → done straight away by the developer, nobody
    reviewed — used to be a notice only."""
    gate_mode("enforce")
    data = await _seed()
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(_pr(), files=_FILES))
    assert resp.status_code == 422, resp.text
    assert "no_independent_review" in _codes(resp)


@pytest.mark.asyncio
async def test_skip_review_card_may_close_itself(client, fake_redis, gate_mode):
    """Counter-probe: automation cards (skip_review) are built to skip review."""
    gate_mode("enforce")
    data = await _seed()
    await _set(data["task"].id, skip_review=True)
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(_pr(), files=_FILES))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_progress_note_is_no_verdict(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, "Schaue mir das an …", "progress")
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(_pr(), files=_FILES),
                          token="rev_tok")
    assert resp.status_code == 422 and "reviewer_silent" in _codes(resp)


@pytest.mark.asyncio
async def test_chat_reply_is_no_verdict(client, fake_redis, gate_mode):
    """The reviewer's chat answering the operator ("looks fine so far") is not
    the review run's verdict (round-3 re-review S2: ``chat_reply`` in
    ``_NOT_A_VERDICT`` had no test). The server stores it with that type."""
    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, "Looks fine so far.", "chat_reply")
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(_pr(), files=_FILES),
                          token="rev_tok")
    assert resp.status_code == 422 and "reviewer_silent" in _codes(resp)


@pytest.mark.asyncio
async def test_reviewer_message_is_a_verdict(client, fake_redis, gate_mode):
    """Counter-probe: the same words as the review run's own ``message`` count."""
    gate_mode("enforce")
    data = await _seed(status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, "Looks fine so far.", "message")
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(_pr(), files=_FILES),
                          token="rev_tok")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_developer_known_without_log_entries(client, fake_redis, gate_mode):
    """Sabotage: the activity log names no developer — the card's assignee
    moving it to done is still the developer."""
    gate_mode("enforce")
    data = await _seed()
    with patch("app.services.work_context.find_last_developer", new=AsyncMock(return_value=None)):
        resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(_pr(), files=_FILES))
    assert resp.status_code == 422, resp.text
    assert "no_independent_review" in _codes(resp)


# ── s: gefangen_von is counted ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_caught_by_stats_counts_every_catcher(client, fake_redis):
    from app.models.activity import ActivityEvent

    board_id = uuid.uuid4()
    from app.models.board import Board

    rows = [
        (proof_gate.PROOF_GATE_EVENT, {"gefangen_von": "tor", "blocked": False, "findings": [
            {"code": "ci_red", "level": "hard", "message": ""}]}),
        (proof_gate.PROOF_GATE_EVENT, {"gefangen_von": "tor", "blocked": True, "findings": [
            {"code": "pr_not_found", "level": "hard", "message": ""}]}),
        (proof_gate.PROOF_GATE_EVENT, {"gefangen_von": "tor", "blocked": False, "findings": [
            {"code": "pr_not_on_card", "level": "notice", "message": ""}]}),
        (proof_gate.PROOF_GATE_EVENT, {"gefangen_von": "tor", "blocked": False, "findings": []}),
        ("task.review_rejected", {"gefangen_von": "rolle"}),
        ("task.review_rejected", {"gefangen_von": "mark"}),
        ("task.review_rejected", {}),
    ]
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Stats", slug=f"st-{uuid.uuid4().hex[:8]}"))
        await s.commit()
        for etype, detail in rows:
            s.add(ActivityEvent(event_type=etype, title="x", board_id=board_id, detail=detail))
        await s.commit()
        stats = await proof_gate.caught_by_stats(s, days=30)

    assert stats["checks"] == 4
    assert stats["caught_by"]["tor"] == 2          # only hard findings count as "caught"
    assert stats["caught_by"]["rolle"] == 1 and stats["caught_by"]["mark"] == 1
    assert stats["caught_by"]["unbekannt"] == 1    # rejections from before the field existed
    assert stats["blocked"] == 1
    assert stats["findings"]["ci_red"] == 1 and stats["findings"]["pr_not_on_card"] == 1
    assert stats["notices"] == 1


@pytest.mark.asyncio
async def test_stats_endpoint_needs_a_user(client, fake_redis):
    resp = await client.get("/api/v1/activity/proof-gate/stats")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_stats_endpoint_answers_a_user(auth_client, fake_redis):
    resp = await auth_client.get("/api/v1/activity/proof-gate/stats?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window_days"] == 7
    assert set(proof_gate.CAUGHT_BY_VALUES) <= set(body["caught_by"])
    assert "mode" in body and "enforce_min_checks" in body


# ── a: branch binding without false alarms (dry run 2026-10-19) ─────────
# The dry run over the last 30 done cards showed `pr_branch_mismatch` on 6 of
# 8 real code cards: MC's workspace pushes `task/<slug>` and agents pick their
# own branch names, the card keeps the branch it was dispatched with.


def _dated(created):
    pr = _pr(branch="task/other")
    pr["createdAt"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    return pr


@pytest.mark.asyncio
async def test_workspace_branch_of_the_card_is_its_branch(client, fake_redis, gate_mode):
    from app.services.git_service import slugify_workspace_slug

    gate_mode("enforce")
    data = await _seed(branch_name="fix/what-the-dispatcher-asked-for")
    # a long title: the workspace caps the slug and appends a hash
    title = "Chat scrollt weiter seitwaerts, der echte Fall des Operators war ein anderer"
    await _set(data["task"].id, title=title)
    pr = _pr(branch=f"task/{slugify_workspace_slug(title)}")
    resp, events = await _call(client, data, {"status": "review"}, _fake_gh(pr, files=_FILES))
    assert resp.status_code == 200, resp.text
    assert "pr_branch_mismatch" not in _all_codes(events)


@pytest.mark.asyncio
async def test_own_branch_name_on_a_newer_pr_is_a_notice(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _seed(branch_name="fix/card-branch")
    resp, events = await _call(client, data, {"status": "review"},
                               _fake_gh(_dated(utcnow() + timedelta(minutes=5)), files=_FILES))
    assert resp.status_code == 200, resp.text
    assert "pr_branch_mismatch" in _all_codes(events)


@pytest.mark.asyncio
async def test_foreign_pr_older_than_the_card_is_refused(client, fake_redis, gate_mode):
    """Sabotage: someone else's PR, opened before this card existed."""
    gate_mode("enforce")
    data = await _seed(branch_name="fix/card-branch")
    resp, _ = await _call(client, data, {"status": "review"},
                          _fake_gh(_dated(utcnow() - timedelta(days=3)), files=_FILES))
    assert resp.status_code == 422, resp.text
    assert "pr_branch_mismatch" in _codes(resp)


# ── round 3: critic findings on round 2 ─────────────────────────────────


def _gh_list_fails(pr_list_rc=1, err="HTTP 502: Bad Gateway"):
    base = _fake_gh(None, pr_list=[])

    async def fake(*args, env=None, timeout=None):
        if args[1] == "pr" and args[2] == "list":
            base.calls.append(args)
            return pr_list_rc, "", err
        return await base(*args, env=env, timeout=timeout)

    fake.calls = base.calls
    return fake


async def _code_card(**seed):
    data = await _seed(pr_url=None, branch_name="feat/x", **seed)
    await _set(data["task"].id, repo_id=await _repo(data["board"].id),
               delegation_type="code_change")
    return data


@pytest.mark.asyncio
async def test_no_token_is_no_proof_of_a_missing_pr(client, fake_redis, gate_mode):
    """Critic r2 blocker A: without a token the branch was never looked at —
    "could not look" must not read as "no PR" (fail-open, loud)."""
    gate_mode("enforce")
    data = await _code_card()
    resp, events = await _call(client, data, {"status": "done"}, _fake_gh(None, pr_list=[]),
                               gh_token="")
    assert resp.status_code == 200, resp.text
    codes = {(f["code"], f["level"]) for c in events for f in c.kwargs["detail"]["findings"]}
    assert ("pr_lookup_unavailable", "notice") in codes
    assert not any(code == "pr_missing" and lvl == "hard" for code, lvl in codes)


@pytest.mark.asyncio
async def test_github_error_on_lookup_is_no_proof_of_a_missing_pr(client, fake_redis, gate_mode):
    """Critic r2 blocker B: `gh pr list` answers 502 → unknown, not "none"."""
    gate_mode("enforce")
    data = await _code_card()
    resp, events = await _call(client, data, {"status": "done"}, _gh_list_fails())
    assert resp.status_code == 200, resp.text
    assert "pr_lookup_unavailable" in _all_codes(events)


@pytest.mark.asyncio
async def test_lookup_timeout_is_no_proof_of_a_missing_pr(client, fake_redis, gate_mode):
    gate_mode("enforce")
    data = await _code_card()
    base = _fake_gh(None, pr_list=[])

    async def hang(*args, env=None, timeout=None):
        if args[1] == "pr" and args[2] == "list":
            raise __import__("asyncio").TimeoutError()
        return await base(*args, env=env, timeout=timeout)

    resp, events = await _call(client, data, {"status": "done"}, hang)
    assert resp.status_code == 200, resp.text
    assert "pr_lookup_unavailable" in _all_codes(events)


def _foreign(created, *, branch="feat/totally-unrelated-other-work", body="", title="Other"):
    pr = _pr(branch=branch)
    pr["createdAt"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    pr["title"], pr["body"] = title, body
    return pr


async def _reviewed_card(branch="fix/card-branch"):
    data = await _seed(branch_name=branch, status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, "Geprueft, passt.", "review")
    return data


@pytest.mark.asyncio
async def test_newer_unrelated_pr_cannot_close_the_card(client, fake_redis, gate_mode):
    """Critic r2 major C: a colleague's fresh PR on an unrelated branch used
    to pass with a notice. Without anything tying it to the card → done refused."""
    gate_mode("enforce")
    data = await _reviewed_card()
    pr = _foreign(utcnow() + timedelta(minutes=5))
    resp, _ = await _call(client, data, {"status": "done"}, _fake_gh(pr, files=_FILES),
                          token="rev_tok")
    assert resp.status_code == 422, resp.text
    assert "pr_branch_mismatch" in _codes(resp)


@pytest.mark.asyncio
async def test_newer_pr_naming_the_card_may_close_it(client, fake_redis, gate_mode):
    """Counter-probe: the PR names the card id in its body — tied to the card."""
    gate_mode("enforce")
    data = await _reviewed_card()
    pr = _foreign(utcnow() + timedelta(minutes=5),
                  body=f"Fixes the bug.\n\nMC card: {str(data['task'].id)[:8]}")
    resp, events = await _call(client, data, {"status": "done"}, _fake_gh(pr, files=_FILES),
                               token="rev_tok")
    assert resp.status_code == 200, resp.text
    assert "pr_branch_mismatch" in _all_codes(events)


@pytest.mark.asyncio
async def test_pr_on_the_branch_of_another_open_card_is_refused(client, fake_redis, gate_mode):
    """Critic r2 major C: the PR runs on a branch an open colleague's card
    carries → it is that card's work, refused already on review."""
    from app.models.task import Task

    gate_mode("enforce")
    data = await _seed(branch_name="fix/card-branch")
    other = Task(id=uuid.uuid4(), board_id=data["board"].id, title="Kollegen-Karte",
                 status="in_progress", branch_name="feat/colleague-work")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(other)
        await s.commit()
    pr = _foreign(utcnow() + timedelta(minutes=5), branch="feat/colleague-work")
    resp, _ = await _call(client, data, {"status": "review"}, _fake_gh(pr, files=_FILES))
    assert resp.status_code == 422, resp.text
    assert "pr_branch_of_other_card" in _codes(resp)


@pytest.mark.asyncio
async def test_pr_on_the_workspace_branch_of_another_open_card_is_refused(
        client, fake_redis, gate_mode):
    from app.models.task import Task
    from app.services.git_service import slugify_workspace_slug

    gate_mode("enforce")
    data = await _seed(branch_name="fix/card-branch")
    other = Task(id=uuid.uuid4(), board_id=data["board"].id, title="Kollegen Arbeit am Chat",
                 status="review")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(other)
        await s.commit()
    pr = _foreign(utcnow() + timedelta(minutes=5),
                  branch=f"task/{slugify_workspace_slug(other.title)}")
    resp, _ = await _call(client, data, {"status": "review"}, _fake_gh(pr, files=_FILES))
    assert resp.status_code == 422, resp.text
    assert "pr_branch_of_other_card" in _codes(resp)


@pytest.mark.asyncio
async def test_done_card_on_the_same_branch_is_no_owner(client, fake_redis, gate_mode):
    """Counter-probe: a finished card with the same branch name (re-dispatch)
    does not own the branch any more."""
    from app.models.task import Task

    gate_mode("enforce")
    data = await _seed(branch_name="fix/card-branch")
    old = Task(id=uuid.uuid4(), board_id=data["board"].id, title="Alte Runde", status="done",
               branch_name="fix/card-branch")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(old)
        await s.commit()
    resp, events = await _call(client, data, {"status": "review"},
                               _fake_gh(_pr(branch="fix/card-branch"), files=_FILES))
    assert resp.status_code == 200, resp.text
    assert "pr_branch_of_other_card" not in _all_codes(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("compare", [
    # 6 empty own commits on top of a merge from main: the walk cannot see them all
    {"files": 12, "commits": [(c * 40, 1) for c in "abcdef"] + [("m" * 40, 2)]},
    None,  # compare fails (old head gone after a force-push)
    {"files": 3, "status": "diverged", "commits": [("c" * 40, 1)]},  # history rewritten
])
async def test_unverifiable_change_after_request_is_visible(
        client, fake_redis, gate_mode, compare):
    """Critic r2 major D: (d) returned None and the gate said nothing at all."""
    gate_mode("enforce")
    data = await _changes_requested_card(fp=None)
    gh = _fake_gh(_pr(), files=None, compare=compare, commit_files={})
    resp, events = await _call(client, data, {"status": "review"}, gh)
    assert resp.status_code == 200, resp.text
    assert "change_unverifiable" in _all_codes(events)


@pytest.mark.asyncio
async def test_force_push_of_the_same_tree_is_no_change(client, fake_redis, gate_mode):
    """Diverged history with an identical tree = same content force-pushed."""
    gate_mode("enforce")
    data = await _changes_requested_card(fp=None)
    gh = _fake_gh(_pr(), files=None,
                  compare={"files": 3, "status": "diverged", "commits": [("c" * 40, 1)]},
                  trees={OTHER_SHA: "f" * 40, SHA: "f" * 40})
    resp, _ = await _call(client, data, {"status": "review"}, gh)
    assert resp.status_code == 422, resp.text
    assert "no_commit_after_change" in _codes(resp)


@pytest.mark.asyncio
async def test_linear_own_commits_with_files_are_a_change(client, fake_redis, gate_mode):
    """Counter-probe: only own, linear commits and the compare shows files →
    real change, however many commits (no walk, no cap)."""
    gate_mode("enforce")
    data = await _changes_requested_card(fp=None)
    gh = _fake_gh(_pr(), files=None,
                  compare={"files": 3, "commits": [(c * 40, 1) for c in "abcdefgh"]})
    resp, events = await _call(client, data, {"status": "review"}, gh)
    assert resp.status_code == 200, resp.text
    assert not {"change_unverifiable", "no_commit_after_change"} & _all_codes(events)


@pytest.mark.asyncio
async def test_stats_count_cards_not_retries(client, fake_redis):
    """Critic r2 major E: a looping agent retrying 5× is one catch, not five."""
    from app.models.activity import ActivityEvent
    from app.models.board import Board

    board_id, card, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hard = {"gefangen_von": "tor", "blocked": True,
            "findings": [{"code": "ci_red", "level": "hard", "message": ""}]}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Stats2", slug=f"st-{uuid.uuid4().hex[:8]}"))
        await s.commit()
        for _ in range(5):
            s.add(ActivityEvent(event_type=proof_gate.PROOF_GATE_EVENT, title="x",
                                board_id=board_id, task_id=card, detail=hard))
        s.add(ActivityEvent(event_type=proof_gate.PROOF_GATE_EVENT, title="x", board_id=board_id,
                            task_id=other, detail={"gefangen_von": "tor", "findings": []}))
        await s.commit()
        before = await proof_gate.caught_by_stats(s, days=30)
        checks = await proof_gate.count_checks(s)
    # other tests in the session may have written events → compare deltas via own cards
    assert before["caught_by"]["tor"] >= 1
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(event_type=proof_gate.PROOF_GATE_EVENT, title="x",
                            board_id=board_id, task_id=card, detail=hard))
        await s.commit()
        after = await proof_gate.caught_by_stats(s, days=30)
        checks_after = await proof_gate.count_checks(s)
    assert after["caught_by"]["tor"] == before["caught_by"]["tor"]   # same card again
    assert after["findings"]["ci_red"] == before["findings"]["ci_red"]
    assert after["attempts"] == before["attempts"] + 1               # retries stay visible
    assert checks_after == checks                                    # rollout counts cards


@pytest.mark.asyncio
async def test_board_lead_closing_is_not_the_developer(client, fake_redis, gate_mode):
    """Critic r2 minor F: the log heuristic can name the lead as developer —
    the lead closing a reviewed card must not be refused as self-review."""
    gate_mode("enforce")
    data = await _reviewed_card()
    await _set_agent(data["rev"].id, is_board_lead=True)
    data["rev"].is_board_lead = True  # what find_last_developer would load
    with patch("app.services.work_context.find_last_developer",
               new=AsyncMock(return_value=data["rev"])):
        resp, _ = await _call(client, data, {"status": "done"},
                              _fake_gh(_pr(branch="fix/card-branch"), files=_FILES),
                              token="rev_tok")
    assert resp.status_code == 200, resp.text


async def _set_agent(agent_id, **fields):
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        for key, value in fields.items():
            setattr(agent, key, value)
        s.add(agent)
        await s.commit()


OLD_PR = "https://github.com/example-org/example-repo/pull/7"


@pytest.mark.asyncio
async def test_branch_lookup_prefers_the_open_pr(client, fake_redis, gate_mode):
    """Critic r2 minor G: the newest PR on the branch is an old merged one,
    an open one exists too → the open one is the card's."""
    gate_mode("warn")
    data = await _code_card(status="review", assignee="rev")
    await _add_comment(data["task"].id, data["rev"].id, "Geprueft, passt.", "review")
    gh = _fake_gh(_pr(), files=_FILES, pr_list=[
        {"number": 7, "url": OLD_PR, "state": "MERGED"},
        {"number": 42, "url": PR_URL, "state": "OPEN"},
    ])
    resp, events = await _call(client, data, {"status": "done"}, gh, token="rev_tok")
    assert resp.status_code == 200, resp.text
    assert events[-1].kwargs["detail"]["pr_url"] == PR_URL


@pytest.mark.asyncio
async def test_branch_lookup_skips_a_pr_of_another_card(client, fake_redis, gate_mode):
    """Re-dispatch on the same branch: the previous card's merged PR is not
    this card's PR → pr_missing, not a false pr_on_other_card."""
    from app.models.task import Task

    gate_mode("enforce")
    data = await _code_card()
    old = Task(id=uuid.uuid4(), board_id=data["board"].id, title="Vorgaenger", status="done",
               pr_url=OLD_PR, branch_name="feat/x")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(old)
        await s.commit()
    gh = _fake_gh(_pr(state="MERGED"), files=_FILES,
                  pr_list=[{"number": 7, "url": OLD_PR, "state": "MERGED"}])
    resp, _ = await _call(client, data, {"status": "done"}, gh)
    assert resp.status_code == 422, resp.text
    codes = _codes(resp)
    assert "pr_missing" in codes and "pr_on_other_card" not in codes
