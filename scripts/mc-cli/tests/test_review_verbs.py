"""Tests for the reviewer verbs `mc approve` / `mc reject` (B3).

Both wrap POST /boards/{board_id}/tasks/{task_id}/review
(backend agent_task_status.agent_review_decision), body
{"decision": "approve"|"request_changes", "comment": ...}. The backend
requires a non-empty comment, so `mc approve` supplies a default when no
--feedback is given; `mc reject` hard-requires --feedback.
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


def _mock_cfg():
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, TASK_ID)
    return cfg


def _mock_client():
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append({"method": method, "path": path, "body": body})
        return {"status": "ok", "decision": (body or {}).get("decision")}

    client.request.side_effect = request
    return client


class _ApproveArgs:
    def __init__(self, feedback=None, task_id=None):
        self.feedback = feedback
        self.task_id = task_id


class _RejectArgs:
    def __init__(self, feedback=None, task_id=None):
        self.feedback = feedback
        self.task_id = task_id


# ── approve ────────────────────────────────────────────────────────────────

def test_approve_hits_review_endpoint_with_decision_approve():
    cfg = _mock_cfg()
    client = _mock_client()
    rc = commands._cmd_approve(_ApproveArgs(), client, cfg)
    assert rc == 0
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/agent/boards/{BOARD_ID}/tasks/{TASK_ID}/review"
    assert call["body"]["decision"] == "approve"
    # Backend requires a non-empty comment even for approve.
    assert call["body"]["comment"].strip()


def test_approve_passes_feedback_as_comment():
    cfg = _mock_cfg()
    client = _mock_client()
    commands._cmd_approve(_ApproveArgs(feedback="LGTM, sauber gebaut."), client, cfg)
    assert client.calls[0]["body"]["comment"] == "LGTM, sauber gebaut."


# ── reject ─────────────────────────────────────────────────────────────────

def test_reject_hits_review_endpoint_with_request_changes():
    cfg = _mock_cfg()
    client = _mock_client()
    rc = commands._cmd_reject(_RejectArgs(feedback="Tests fehlen, bitte nachziehen."), client, cfg)
    assert rc == 0
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/agent/boards/{BOARD_ID}/tasks/{TASK_ID}/review"
    assert call["body"]["decision"] == "request_changes"
    assert call["body"]["comment"] == "Tests fehlen, bitte nachziehen."


def test_reject_without_feedback_errors():
    cfg = _mock_cfg()
    client = _mock_client()
    with pytest.raises(UsageError):
        commands._cmd_reject(_RejectArgs(feedback=None), client, cfg)
    # No HTTP call must happen.
    assert client.calls == []


def test_reject_empty_feedback_errors():
    cfg = _mock_cfg()
    client = _mock_client()
    with pytest.raises(UsageError):
        commands._cmd_reject(_RejectArgs(feedback="   "), client, cfg)
    assert client.calls == []


# ── Registry / help wiring ─────────────────────────────────────────────────

def test_verbs_registered_with_review_endpoint():
    for name in ("approve", "reject"):
        assert name in commands.REGISTRY
        spec = commands.REGISTRY[name]
        assert any("/review" in e for e in spec.endpoints)
        assert spec.help  # non-empty help text for `mc help`


def test_verbs_reachable_via_argparse():
    from mc_cli.__main__ import build_parser
    parser = build_parser()
    ns = parser.parse_args(["approve", "--feedback", "ok"])
    assert ns.command == "approve"
    ns2 = parser.parse_args(["reject", "--feedback", "no"])
    assert ns2.command == "reject"
