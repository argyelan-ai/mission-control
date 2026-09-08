"""`mc park` — lead parks a worker's sub-task without raising a blocker (08.09.2026)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import REGISTRY, _cmd_park, _cmd_patch  # noqa: E402
from mc_cli.errors import ClientError, UsageError  # noqa: E402


from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class _Cfg:
    board_id: str = "b1"
    task_id: str = "t1"
    dispatch_attempt_id: str | None = "lead-attempt"

    def require_task_context(self):
        return self.board_id, self.task_id


CALLS: list = []


class _Client:
    detail_attempt = "worker-attempt"

    def __init__(self, cfg=None):
        self.cfg = cfg or _Cfg()
        self.calls = CALLS

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
        if path.endswith("/detail"):
            return {"id": "t1", "dispatch_attempt_id": self.detail_attempt}
        return {"ok": True}


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_park_is_registered_with_both_endpoints():
    spec = REGISTRY["park"]
    assert spec.scope == "tasks:write"
    assert "PATCH /boards/{board_id}/tasks/{task_id}" in spec.endpoints
    assert "GET /boards/{board_id}/tasks/{task_id}/detail" in spec.endpoints
    assert "POST /boards/{board_id}/tasks/{task_id}/comments" in spec.endpoints


def test_park_reads_target_attempt_then_notes_then_requeues():
    CALLS.clear()
    rc = _cmd_park(_Args(note="erst Fix 3b, dann das hier"), _Client(), _Cfg())
    assert rc == 0
    methods = [c[0] for c in CALLS]
    assert methods == ["GET", "POST", "PATCH"]
    assert CALLS[0][1].endswith("/tasks/t1/detail")
    assert CALLS[1][2]["comment_type"] == "handoff" and "erst Fix 3b" in CALLS[1][2]["content"]
    assert CALLS[2][1] == "/api/v1/agent/boards/b1/tasks/t1" and CALLS[2][2] == {"status": "inbox"}
    # POST + PATCH go out with the TARGET task's attempt id, not the lead's own
    assert CALLS[1][3] == "worker-attempt" and CALLS[2][3] == "worker-attempt"
    assert CALLS[0][3] == "lead-attempt"


def test_park_keeps_own_attempt_when_target_has_none():
    CALLS.clear()

    class _NoAttempt(_Client):
        detail_attempt = None

    assert _cmd_park(_Args(note="x"), _NoAttempt(), _Cfg()) == 0
    assert [c[3] for c in CALLS] == ["lead-attempt", "lead-attempt", "lead-attempt"]


def test_patch_accepts_waiting_and_inbox():
    CALLS.clear()
    assert _cmd_patch(_Args(status="waiting"), _Client(), _Cfg()) == 0
    assert CALLS[-1][2] == {"status": "waiting"}
    assert _cmd_patch(_Args(status="inbox"), _Client(), _Cfg()) == 0
    assert CALLS[-1][2] == {"status": "inbox"}


def test_patch_still_rejects_unknown_status():
    try:
        _cmd_patch(_Args(status="parked"), _Client(), _Cfg())
    except UsageError:
        return
    raise AssertionError("unknown status must raise UsageError")


def test_park_already_inbox_skips_patch():
    """Detail says status=inbox → post the note, skip the PATCH entirely."""
    CALLS.clear()

    class _AlreadyInbox(_Client):
        def request(self, method, path, body=None, **kw):
            self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
            if path.endswith("/detail"):
                return {"id": "t1", "status": "inbox", "dispatch_attempt_id": self.detail_attempt}
            return {"ok": True}

    rc = _cmd_park(_Args(note="schon in inbox"), _AlreadyInbox(), _Cfg())
    assert rc == 0
    methods = [c[0] for c in CALLS]
    assert methods == ["GET", "POST"]
    assert CALLS[1][2]["comment_type"] == "handoff" and "schon in inbox" in CALLS[1][2]["content"]


def test_park_patch_inbox_to_inbox_error_is_success():
    """PATCH raises the 400 'Inbox -> Inbox' transition error → treated as success."""
    CALLS.clear()

    class _RaceInbox(_Client):
        def request(self, method, path, body=None, **kw):
            self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
            if path.endswith("/detail"):
                return {"id": "t1", "status": "waiting", "dispatch_attempt_id": self.detail_attempt}
            if method == "PATCH":
                raise ClientError(
                    "HTTP 400 PATCH /api/v1/agent/boards/b1/tasks/t1: "
                    '{"detail":"Ungültiger Status-Übergang: Inbox -> Inbox"}'
                )
            return {"ok": True}

    rc = _cmd_park(_Args(note="race mit queue-drain"), _RaceInbox(), _Cfg())
    assert rc == 0
    methods = [c[0] for c in CALLS]
    assert methods == ["GET", "POST", "PATCH"]


def test_park_other_patch_error_is_reraised():
    """A PATCH error unrelated to the Inbox->Inbox transition must propagate."""
    CALLS.clear()

    class _OtherError(_Client):
        def request(self, method, path, body=None, **kw):
            self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
            if path.endswith("/detail"):
                return {"id": "t1", "status": "waiting", "dispatch_attempt_id": self.detail_attempt}
            if method == "PATCH":
                raise ClientError("HTTP 409 PATCH /api/v1/agent/boards/b1/tasks/t1: conflict")
            return {"ok": True}

    try:
        _cmd_park(_Args(note="x"), _OtherError(), _Cfg())
    except ClientError:
        return
    raise AssertionError("non-Inbox->Inbox PATCH errors must be re-raised")
