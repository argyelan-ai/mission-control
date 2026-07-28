"""Tests for `mc thread` — re-read your own task thread after a restart.

The verb is a pure look-up: it issues exactly one GET and must never POST.
`mc inbox` is what consumes messages; if `mc thread` ever acked, an agent
rebuilding its context would swallow its own unread mail.
"""
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402

TASK = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _Args:
    task_id = None
    limit = 50
    since_seq = None
    before_seq = None
    json = False


def _mock_client(payload):
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append({"method": method, "path": path, "body": body, "kw": kw})
        return payload

    client.request.side_effect = request
    return client


def _payload(**over):
    base = {
        "task_id": TASK,
        "task_title": "Thread probe",
        "task_status": "in_progress",
        "messages": [
            {"seq": 1, "id": "m1", "direction": "user_to_agent",
             "author": {"kind": "user", "id": None, "display": "Operator"},
             "body": "erste", "body_format": "text",
             "created_at": "2026-07-28T10:00:00Z"},
            {"seq": 2, "id": "m2", "direction": "agent_to_user",
             "author": {"kind": "agent", "id": "rex", "display": "Rex"},
             "body": "zweite", "body_format": "text",
             "created_at": "2026-07-28T10:01:00Z"},
        ],
        "has_more_before": False,
        "latest_seq": 2,
        "my_acked_seq": 1,
    }
    base.update(over)
    return base


def test_never_posts(capsys):
    """The whole point of the verb: reading consumes nothing."""
    client = _mock_client(_payload())
    rc = commands._cmd_thread(_Args(), client, MagicMock())
    assert rc == 0
    assert [c["method"] for c in client.calls] == ["GET"]


def test_prints_messages_in_order_with_author_and_seq(capsys):
    client = _mock_client(_payload())
    commands._cmd_thread(_Args(), client, MagicMock())
    out = capsys.readouterr().out

    assert "Thread probe" in out
    assert "[seq 1 · Operator" in out
    assert "[seq 2 · Rex" in out
    assert out.index("erste") < out.index("zweite")


def test_empty_thread_says_so(capsys):
    client = _mock_client(_payload(messages=[], latest_seq=0, my_acked_seq=0))
    rc = commands._cmd_thread(_Args(), client, MagicMock())
    assert rc == 0
    assert "Keine Nachrichten" in capsys.readouterr().out


def test_no_active_task_is_not_an_error(capsys):
    client = _mock_client(_payload(task_id=None, task_title=None,
                                   task_status=None, messages=[],
                                   latest_seq=0, my_acked_seq=0))
    rc = commands._cmd_thread(_Args(), client, MagicMock())
    assert rc == 0
    assert "Kein aktiver Task" in capsys.readouterr().out


def test_has_more_before_prints_the_paging_hint(capsys):
    client = _mock_client(_payload(has_more_before=True))
    commands._cmd_thread(_Args(), client, MagicMock())
    assert "mc thread --before-seq 1" in capsys.readouterr().out


def test_passes_filters_as_query_params():
    args = _Args()
    args.task_id = TASK
    args.limit = 10
    args.since_seq = 4
    client = _mock_client(_payload())
    commands._cmd_thread(args, client, MagicMock())

    call = client.calls[0]
    assert call["path"] == "/api/v1/agent/me/thread"
    query = call["kw"]["query"]
    assert query["task_id"] == TASK
    assert query["limit"] == 10
    assert query["since_seq"] == 4
    # None is dropped by the client layer — never sent as an empty value.
    assert query["before_seq"] is None


def test_json_mode_emits_raw_payload(capsys):
    args = _Args()
    args.json = True
    client = _mock_client(_payload())
    commands._cmd_thread(args, client, MagicMock())
    out = capsys.readouterr().out
    assert '"my_acked_seq": 1' in out


def test_registry_entry_is_read_only():
    assert "thread" in commands.REGISTRY
    spec = commands.REGISTRY["thread"]
    assert spec.scope == "tasks:read"
    assert spec.endpoints == ("GET /me/thread",)


# ── Discoverability: `mc recover` must point at `mc thread` ───────────────
# A verb nobody knows about is dead weight. The moment an agent needs to know
# the thread is re-readable is the moment it recovers, so the pointer lives in
# the recovery output rather than only in SOUL.md.

def test_recover_points_at_thread(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = {
        "active": True,
        "task": {
            "id": TASK,
            "title": "Nach dem Crash",
            "status": "in_progress",
            "board_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "dispatch_attempt_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "prompt": "der eigentliche prompt",
        },
    }
    client = _mock_client(payload)
    rc = commands._cmd_recover(_Args(), client, MagicMock())
    assert rc == 0

    out = capsys.readouterr().out
    assert "mc thread" in out
    # The prompt itself must still be the payload — the pointer is a header line.
    assert "der eigentliche prompt" in out


def test_budget_truncation_is_visible(capsys):
    """If the server dropped messages to stay inside the char budget, the
    agent must see that — silently showing a partial history is how you get an
    agent confidently acting on half the story."""
    client = _mock_client(_payload(budget_truncated=True, has_more_before=True))
    commands._cmd_thread(_Args(), client, MagicMock())
    out = capsys.readouterr().out
    assert "gekuerzt" in out or "Budget" in out
    assert "--before-seq" in out
