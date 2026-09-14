"""`mc delegate --parent` — explicit parent override (W5-F, 11.09.2026).

Live incident: `mc delegate` created two orphaned cards (b7d29be3, 70d6b417)
with `parent_task_id=None` and no error, no hint — just `{"your_status":
"no_task"}`. This covers the CLI side: `--parent` must be forwarded as
`parent_task_id` in the request body, and a `warning` field on the response
must reach the operator/agent (stderr), not get silently dropped.
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


def test_delegate_prints_root_warning_to_stderr(capsys):
    client = _Client(response={
        "subtask_id": "s1", "assigned_to": "Researcher", "your_status": "no_task",
        "parent_task_id": None,
        "warning": "Kein Parent, kein Callback — diese Karte haengt an nichts.",
    })
    args = _Args(to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001")
    rc = _cmd_delegate(args, client, _Cfg())
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNUNG" in captured.err
    assert "Kein Parent, kein Callback" in captured.err


def test_delegate_no_warning_when_parent_set(capsys):
    client = _Client(response={
        "subtask_id": "s1", "assigned_to": "Researcher", "your_status": "in_progress",
        "parent_task_id": "4e9d2b74-88df-47dd-a678-c1f8f8c989bb",
        "warning": None,
    })
    args = _Args(to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001", parent="4e9d2b74-88df-47dd-a678-c1f8f8c989bb")
    _cmd_delegate(args, client, _Cfg())
    captured = capsys.readouterr()
    assert "WARNUNG" not in captured.err
