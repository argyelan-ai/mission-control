"""`mc delegate --parent` — explicit parent override (W5-F, 11.09.2026).

Live incident: `mc delegate` created two orphaned cards (b7d29be3, 70d6b417)
with `parent_task_id=None` and no error, no hint — just `{"your_status":
"no_task"}`. This covers the CLI side: `--parent` must be forwarded as
`parent_task_id` in the request body.

Task f8c9cdb9 (2026-09-16) removed the orphan path on the backend (it refuses
with 409 before inserting) and with it the `warning` field the CLI used to
print — there is no longer a response that has one. What this file adds for
that task is the other half: `mc delegate --parent` must be reachable at all
without a dispatch context in the env. `require_task_context()` used to run
unconditionally as the first statement, so the exact caller the flag exists
for — a Board Lead with no active task — was killed with a UsageError before
`--parent` could be read.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import REGISTRY, _cmd_delegate  # noqa: E402


from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class _Cfg:
    board_id: str = "b1"
    task_id: str = "t1"

    def require_task_context(self):
        return self.board_id, self.task_id


class _Args:
    def __init__(self, **kw):
        defaults = dict(
            title="Sub", to="researcher-uuid-stand-in", description="Do the concrete thing please",
            priority=None, no_callback=False, origin_thread=None, parent=None,
        )
        defaults.update(kw)
        self.__dict__.update(defaults)


class _Client:
    def __init__(self, response=None):
        self.response = response or {"subtask_id": "s1", "assigned_to": "Researcher", "your_status": "in_progress"}
        self.calls = []

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        return self.response


def test_delegate_is_registered_with_endpoint():
    spec = REGISTRY["delegate"]
    assert spec.scope == "tasks:create"
    assert "POST /boards/{board_id}/delegate" in spec.endpoints


def test_delegate_forwards_explicit_parent_uuid_since_to_arg_is_a_uuid():
    client = _Client()
    args = _Args(to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001", parent="4e9d2b74-88df-47dd-a678-c1f8f8c989bb")
    rc = _cmd_delegate(args, client, _Cfg())
    assert rc == 0
    method, path, body = client.calls[0]
    assert method == "POST" and path == "/api/v1/agent/boards/b1/delegate"
    assert body["parent_task_id"] == "4e9d2b74-88df-47dd-a678-c1f8f8c989bb"


def test_delegate_without_parent_omits_the_field():
    client = _Client()
    args = _Args(to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001")
    _cmd_delegate(args, client, _Cfg())
    body = client.calls[0][2]
    assert "parent_task_id" not in body


@dataclass(frozen=True)
class _CfgNoContext:
    """A Board Lead with no active task: poll.sh wrote no TASK_ID/BOARD_ID."""

    board_id: str | None = None
    task_id: str | None = None

    def require_task_context(self):
        raise AssertionError(
            "require_task_context() darf in _cmd_delegate nicht mehr "
            "aufgerufen werden — sie tötet genau den Aufruf, für den --parent "
            "existiert (Board Lead ohne aktive Karte)"
        )


class _MeClient:
    """Client stub whose /me resolves the board, and whose delegate call is
    recorded. `board_id` mimics what GET /api/v1/agent/me returns."""

    def __init__(self, board_id="b-from-me"):
        self.board_id = board_id
        self.calls = []

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path == "/api/v1/agent/me":
            return {"id": "a1", "name": "Boss", "board_id": self.board_id}
        return {"subtask_id": "s1", "assigned_to": "Researcher",
                "your_status": "in_progress", "parent_task_id": body["parent_task_id"]}


def test_delegate_with_parent_works_without_task_or_board_in_env(capsys):
    """The card's live scenario: no TASK_ID/BOARD_ID, --parent given."""
    client = _MeClient()
    args = _Args(to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001",
                 parent="4e9d2b74-88df-47dd-a678-c1f8f8c989bb")
    rc = _cmd_delegate(args, client, _CfgNoContext())
    assert rc == 0
    assert client.calls[0][:2] == ("GET", "/api/v1/agent/me")
    method, path, body = client.calls[1]
    assert method == "POST"
    assert path == "/api/v1/agent/boards/b-from-me/delegate", (
        "Board muss aus /me kommen, wenn die Env keinen Kontext hat"
    )
    assert body["parent_task_id"] == "4e9d2b74-88df-47dd-a678-c1f8f8c989bb"


def test_delegate_prefers_env_board_over_me_lookup(capsys):
    """With a dispatch context present, no extra round-trip is made."""
    client = _MeClient()
    args = _Args(to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001",
                 parent="4e9d2b74-88df-47dd-a678-c1f8f8c989bb")
    _cmd_delegate(args, client, _Cfg())
    assert client.calls[0][0] == "POST"
    assert client.calls[0][1] == "/api/v1/agent/boards/b1/delegate"
