"""Tests for `mc inbox` — Nudge+Pull message fetch (W2.1).

`mc inbox` GETs /agent/me/inbox, prints each message in the format poll.sh used
to paste (with the `[thread … · seq …]` footer), then POSTs /agent/me/inbox/ack
once per thread with that thread's highest delivered seq. The API GET is the
delivery; the ack is what advances the server cursor.
"""
import os
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402

T1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _Args:
    task_id = None


def _mock_client(inbox_payload):
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and path.endswith("/me/inbox"):
            return inbox_payload
        return {"ok": True}

    client.request.side_effect = request
    return client


def test_empty_inbox_prints_message_and_no_ack(capsys):
    client = _mock_client({"messages": [], "threads": {}})
    rc = commands._cmd_inbox(_Args(), client, MagicMock())
    assert rc == 0
    assert "Keine neuen Nachrichten." in capsys.readouterr().out
    # Only the GET happened, no ack.
    assert [c["method"] for c in client.calls] == ["GET"]


def test_prints_messages_with_footer_and_acks_per_thread(capsys):
    payload = {
        "messages": [
            {"id": "m1", "thread_id": T1, "seq": 5, "sender": "user",
             "message_type": "message", "body": "erste"},
            {"id": "m2", "thread_id": T1, "seq": 6, "sender": "user",
             "message_type": "status", "body": "zweite"},
            {"id": "m3", "thread_id": T2, "seq": 2, "sender": "boss",
             "message_type": "message", "body": "andere thread"},
        ],
        "threads": {T1: 6, T2: 2},
    }
    client = _mock_client(payload)
    rc = commands._cmd_inbox(_Args(), client, MagicMock())
    assert rc == 0

    out = capsys.readouterr().out
    assert "# Neue Nachricht (Interaction 2.0)" in out
    assert "erste" in out and "zweite" in out and "andere thread" in out
    # Footer format matches poll.sh's queue_or_deliver output.
    assert f"[thread {T1} · seq 5 · von user · typ message]" in out
    assert f"[thread {T2} · seq 2 · von boss · typ message]" in out

    # One GET + one ack per thread, each with that thread's max seq.
    gets = [c for c in client.calls if c["method"] == "GET"]
    acks = [c for c in client.calls if c["method"] == "POST"]
    assert len(gets) == 1
    assert {c["path"] for c in acks} == {"/api/v1/agent/me/inbox/ack"}
    ack_bodies = sorted((c["body"]["thread_id"], c["body"]["seq"]) for c in acks)
    assert ack_bodies == sorted([(T1, 6), (T2, 2)])


def test_ack_failure_does_not_abort_other_threads(capsys):
    payload = {
        "messages": [
            {"id": "m1", "thread_id": T1, "seq": 1, "sender": "user",
             "message_type": "message", "body": "a"},
            {"id": "m2", "thread_id": T2, "seq": 1, "sender": "user",
             "message_type": "message", "body": "b"},
        ],
        "threads": {T1: 1, T2: 1},
    }
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append({"method": method, "path": path, "body": body})
        if method == "GET":
            return payload
        if body and body.get("thread_id") == T1:
            raise RuntimeError("boom on T1 ack")
        return {"ok": True}

    client.request.side_effect = request
    rc = commands._cmd_inbox(_Args(), client, MagicMock())
    assert rc == 0

    err = capsys.readouterr().err
    assert T1 in err  # the failing thread is reported
    # T2 ack still attempted despite T1 failing.
    ack_threads = [c["body"]["thread_id"] for c in client.calls if c["method"] == "POST"]
    assert T1 in ack_threads and T2 in ack_threads


def test_inbox_registered_and_reachable_via_argparse():
    assert "inbox" in commands.REGISTRY
    spec = commands.REGISTRY["inbox"]
    assert "GET /me/inbox" in spec.endpoints
    assert "POST /me/inbox/ack" in spec.endpoints
    assert spec.help

    from mc_cli.__main__ import build_parser
    ns = build_parser().parse_args(["inbox"])
    assert ns.command == "inbox"
