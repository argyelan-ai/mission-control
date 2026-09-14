"""`mc delegate --repo` — Registry-Repo-Bindung (ADR-052) fuer den CLI-Pfad.

Ein Board Lead konnte eine Karte bisher nicht mit einem Registry-Repo
verbinden ohne die Operator-Route — `mc delegate` hatte kein passendes
Flag. Deckt nur die CLI-Weiterleitung ab (Flag → `repo_id` im Body);
Aufloesung + Aktiv-Check passiert server-seitig (siehe Backend-Tests
test_agent_repo_binding.py).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import _cmd_delegate  # noqa: E402


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
            title="Sub", to="d5c1e6f0-9c2a-4b1a-8f0a-000000000001",
            description="Do the concrete thing please",
            priority=None, no_callback=False, origin_thread=None, parent=None, repo=None,
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


def test_delegate_forwards_repo_slug_as_repo_id():
    client = _Client()
    args = _Args(repo="acme/mission-control")
    rc = _cmd_delegate(args, client, _Cfg())
    assert rc == 0
    body = client.calls[0][2]
    assert body["repo_id"] == "acme/mission-control"


def test_delegate_forwards_repo_uuid_as_repo_id():
    client = _Client()
    args = _Args(repo="4e9d2b74-88df-47dd-a678-c1f8f8c989bb")
    _cmd_delegate(args, client, _Cfg())
    body = client.calls[0][2]
    assert body["repo_id"] == "4e9d2b74-88df-47dd-a678-c1f8f8c989bb"


def test_delegate_without_repo_omits_the_field():
    client = _Client()
    args = _Args()
    _cmd_delegate(args, client, _Cfg())
    body = client.calls[0][2]
    assert "repo_id" not in body
