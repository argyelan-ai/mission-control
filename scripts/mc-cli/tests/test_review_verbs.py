"""Tests for the reviewer verbs `mc approve` / `mc reject` / `mc review-note`.

approve/reject wrap POST /boards/{board_id}/tasks/{task_id}/review
(backend agent_task_status.agent_review_decision), body
{"decision": "approve"|"request_changes", "comment": ...}. The backend
requires a non-empty comment, so `mc approve` supplies a default when no
--feedback is given; `mc reject` hard-requires --feedback.

Target resolution is the part with teeth: a review verb decides SOMEONE
ELSE'S card, so the env TASK_ID poll.sh injects is not a safe default. It is
accepted only in the review-handoff shape (the env card IS the author's card:
status review, no source_task_id) and refused everywhere else with the id the
caller actually needs.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

BOARD_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
AUTHOR_TASK_ID = "33333333-3333-3333-3333-333333333333"


def _mock_cfg(task_id=TASK_ID):
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, task_id)
    return cfg


def _mock_client(detail=None):
    """detail = what GET .../detail returns for the env card."""
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and path.endswith("/detail"):
            return detail if detail is not None else {}
        return {"status": "ok", "decision": (body or {}).get("decision")}

    client.request.side_effect = request
    return client


# The review-handoff shape: handle_review_handoff assigned the AUTHOR's card to
# the reviewer and dispatched it, so the env card really is the target.
HANDOFF_CARD = {"status": "review", "source_task_id": None}
# A reviewer's OWN review card: created via delegation_type="review", points at
# the reviewed card through source_task_id.
OWN_REVIEW_CARD = {"status": "review", "source_task_id": AUTHOR_TASK_ID}


class _ApproveArgs:
    def __init__(self, feedback=None, task_id=None):
        self.feedback = feedback
        self.task_id = task_id


class _RejectArgs:
    def __init__(self, feedback=None, task_id=None):
        self.feedback = feedback
        self.task_id = task_id


class _ReviewNoteArgs:
    def __init__(self, feedback=None, task_id=None, decision="request_changes"):
        self.feedback = feedback
        self.task_id = task_id
        self.decision = decision


# ── approve ────────────────────────────────────────────────────────────────

def test_approve_hits_review_endpoint_with_decision_approve():
    cfg = _mock_cfg()
    client = _mock_client(HANDOFF_CARD)
    rc = commands._cmd_approve(_ApproveArgs(), client, cfg)
    assert rc == 0
    call = client.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/agent/boards/{BOARD_ID}/tasks/{TASK_ID}/review"
    assert call["body"]["decision"] == "approve"
    # Backend requires a non-empty comment even for approve.
    assert call["body"]["comment"].strip()


def test_approve_passes_feedback_as_comment():
    cfg = _mock_cfg()
    client = _mock_client(HANDOFF_CARD)
    commands._cmd_approve(_ApproveArgs(feedback="LGTM, sauber gebaut."), client, cfg)
    assert client.calls[-1]["body"]["comment"] == "LGTM, sauber gebaut."


# ── reject ─────────────────────────────────────────────────────────────────

def test_reject_hits_review_endpoint_with_request_changes():
    cfg = _mock_cfg()
    client = _mock_client(HANDOFF_CARD)
    rc = commands._cmd_reject(_RejectArgs(feedback="Tests fehlen, bitte nachziehen."), client, cfg)
    assert rc == 0
    call = client.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/agent/boards/{BOARD_ID}/tasks/{TASK_ID}/review"
    assert call["body"]["decision"] == "request_changes"
    assert call["body"]["comment"] == "Tests fehlen, bitte nachziehen."


def test_reject_without_feedback_errors():
    cfg = _mock_cfg()
    client = _mock_client(HANDOFF_CARD)
    with pytest.raises(UsageError):
        commands._cmd_reject(_RejectArgs(feedback=None), client, cfg)
    # No HTTP call must happen.
    assert client.calls == []


def test_reject_empty_feedback_errors():
    cfg = _mock_cfg()
    client = _mock_client(HANDOFF_CARD)
    with pytest.raises(UsageError):
        commands._cmd_reject(_RejectArgs(feedback="   "), client, cfg)
    assert client.calls == []


# ── Target resolution: explicit id wins, env id is not guessed ─────────────

def test_explicit_task_id_skips_the_detail_lookup():
    """An id the caller typed is taken as given — no inference, no round trip.

    __main__ folds the positional into cfg.task_id before the handler runs, so
    cfg already reports the author's card here.
    """
    cfg = _mock_cfg(AUTHOR_TASK_ID)
    client = _mock_client()
    commands._cmd_approve(_ApproveArgs(task_id=AUTHOR_TASK_ID), client, cfg)
    assert [c["method"] for c in client.calls] == ["POST"]
    assert client.calls[0]["path"].endswith(f"/tasks/{AUTHOR_TASK_ID}/review")


def test_reject_refuses_own_review_card_and_names_the_real_target():
    """Incident E-review: `mc reject` with no id hit the reviewer's OWN round-1
    review card — the only card in `review` — and bounced it to inbox carrying
    changes_requested. The card carries source_task_id, so it can never be the
    target; we refuse and hand back the id that is."""
    cfg = _mock_cfg()
    client = _mock_client(OWN_REVIEW_CARD)
    with pytest.raises(UsageError) as exc:
        commands._cmd_reject(_RejectArgs(feedback="Blocker in der Migration."), client, cfg)
    msg = str(exc.value)
    assert AUTHOR_TASK_ID in msg          # the id to use, not just "something is missing"
    assert "mc reject" in msg             # the command to run
    # Nothing was decided.
    assert [c["method"] for c in client.calls] == ["GET"]


def test_approve_refuses_own_review_card():
    cfg = _mock_cfg()
    client = _mock_client(OWN_REVIEW_CARD)
    with pytest.raises(UsageError) as exc:
        commands._cmd_approve(_ApproveArgs(), client, cfg)
    assert AUTHOR_TASK_ID in str(exc.value)
    assert [c["method"] for c in client.calls] == ["GET"]


def test_refuses_when_own_card_is_not_in_review():
    """A card that is not in review has no decision to make — ask for the id
    instead of firing at it, and point at review-note for the closed case."""
    cfg = _mock_cfg()
    client = _mock_client({"status": "in_progress", "source_task_id": None})
    with pytest.raises(UsageError) as exc:
        commands._cmd_reject(_RejectArgs(feedback="Nein."), client, cfg)
    msg = str(exc.value)
    assert "in_progress" in msg
    assert "review-note" in msg
    assert [c["method"] for c in client.calls] == ["GET"]


# ── review-note (late review, author card already closed) ──────────────────

def test_review_note_posts_to_review_note_endpoint():
    cfg = _mock_cfg(AUTHOR_TASK_ID)
    client = _mock_client()
    rc = commands._cmd_review_note(
        _ReviewNoteArgs(task_id=AUTHOR_TASK_ID, feedback="Blocker: fehlende Rollback-Pfad."),
        client, cfg,
    )
    assert rc == 0
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/agent/boards/{BOARD_ID}/tasks/{AUTHOR_TASK_ID}/review-note"
    assert call["body"]["decision"] == "request_changes"
    assert call["body"]["comment"] == "Blocker: fehlende Rollback-Pfad."


def test_review_note_requires_explicit_task_id():
    """The whole point is that it targets a foreign, already-closed card —
    falling back to the env id would recreate the bug it exists to fix."""
    cfg = _mock_cfg()
    client = _mock_client()
    with pytest.raises(UsageError) as exc:
        commands._cmd_review_note(_ReviewNoteArgs(feedback="..."), client, cfg)
    assert "task-id" in str(exc.value).lower()
    assert client.calls == []


def test_review_note_requires_feedback():
    cfg = _mock_cfg(AUTHOR_TASK_ID)
    client = _mock_client()
    with pytest.raises(UsageError):
        commands._cmd_review_note(_ReviewNoteArgs(task_id=AUTHOR_TASK_ID), client, cfg)
    assert client.calls == []


# ── Registry / help wiring ─────────────────────────────────────────────────

def test_verbs_registered_with_review_endpoint():
    for name in ("approve", "reject"):
        assert name in commands.REGISTRY
        spec = commands.REGISTRY[name]
        assert any("/review" in e for e in spec.endpoints)
        assert spec.help  # non-empty help text for `mc help`


def test_review_note_registered():
    spec = commands.REGISTRY["review-note"]
    assert any("/review-note" in e for e in spec.endpoints)
    assert spec.help


def test_verbs_reachable_via_argparse():
    from mc_cli.__main__ import build_parser
    parser = build_parser()
    ns = parser.parse_args(["approve", "--feedback", "ok"])
    assert ns.command == "approve"
    ns2 = parser.parse_args(["reject", "--feedback", "no"])
    assert ns2.command == "reject"
    ns3 = parser.parse_args([
        "review-note", AUTHOR_TASK_ID, "--decision", "request_changes", "--feedback", "spaet",
    ])
    assert ns3.command == "review-note"
    assert ns3.task_id == AUTHOR_TASK_ID
    assert ns3.decision == "request_changes"
