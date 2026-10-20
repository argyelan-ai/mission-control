"""Beweis-Tor, CLI half: `mc review` / `mc done` / `mc finish` report the
local git head (sha + branch) and an optional `--reviewer` to the server, and
`--force` never carries anything that could skip the server's gate.

The gate itself lives on the server (backend/app/services/proof_gate.py,
backend/tests/test_proof_gate.py) — the CLI only supplies evidence.
"""
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402

GOOD_REFLECTION = (
    "## Was wurde gemacht\nFeature gebaut, Tests geschrieben und den PR geoeffnet.\n\n"
    "## Was hat funktioniert\nTest zuerst, dann der Fix; CI lief gruen durch.\n\n"
    "## Was war unklar\nNichts Wesentliches, der Auftrag war eindeutig.\n\n"
    "## Lesson fuer Agent-Memory\nVor dem Abschluss immer pushen und CI abwarten.\n"
)


class _Client:
    def __init__(self, task=None):
        self.calls = []
        self.task = task or {"status": "in_progress", "assigned_agent_id": "a1"}

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/detail"):
            return dict(self.task)
        if method == "GET":
            return []
        return {"id": "t1", "status": (body or {}).get("status")}

    def patch_bodies(self):
        return [b for m, _, b in self.calls if m == "PATCH"]


def _cfg():
    cfg = MagicMock()
    cfg.require_task_context.return_value = ("b1", "t1")
    cfg.board_id, cfg.task_id = "b1", "t1"
    return cfg


class _Args:
    def __init__(self, **kw):
        self.pr = None
        self.pr_url = None
        self.force = False
        self.review = False
        self.reviewer = None
        self.needs_decision = None
        self.task_id = None
        self.message = GOOD_REFLECTION
        self.__dict__.update(kw)


@pytest.fixture
def fake_head(monkeypatch):
    monkeypatch.setattr(commands, "_local_git_head", lambda: ("c0ffee" * 6 + "abcd", "feat/x"))


def test_review_reports_local_head(fake_head):
    client = _Client()
    assert commands._cmd_review(_Args(pr=42), client, _cfg()) == 0
    body = client.patch_bodies()[-1]
    assert body["status"] == "review" and body["pr_number"] == 42
    assert body["head_sha"].startswith("c0ffee") and body["head_branch"] == "feat/x"


def test_non_closing_status_carries_no_head(fake_head):
    client = _Client()
    commands._patch_status(client, _cfg(), "in_progress")
    assert "head_sha" not in client.patch_bodies()[-1]


def test_finish_force_sends_no_bypass_and_passes_reviewer(fake_head, monkeypatch):
    monkeypatch.setattr(commands, "_agent_base", lambda cfg: ("b1", "t1", "/api/v1/agent/boards/b1/tasks/t1"))
    client = _Client()
    rc = commands._cmd_finish(_Args(force=True, reviewer="Rex"), client, _cfg())
    assert rc == 0
    body = client.patch_bodies()[-1]
    assert body["status"] == "done" and body["reviewer"] == "Rex"
    # The only keys a closing PATCH carries — nothing named like a skip switch.
    assert set(body) <= {"status", "head_sha", "head_branch", "reviewer", "pr_number", "pr_url"}


def test_force_help_says_it_does_not_touch_the_gate():
    from mc_cli.__main__ import build_parser

    parser = build_parser()
    for verb in ("finish", "done"):
        sub = parser._subparsers._group_actions[0].choices[verb]
        force = next(a for a in sub._actions if "--force" in a.option_strings)
        assert "Beweis-Tor" in force.help
        assert any("--reviewer" in a.option_strings for a in sub._actions)


@pytest.mark.real_git_head
def test_local_git_head_reads_branch_and_sha(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, check=True,  # noqa: E731
                                    capture_output=True, text=True)
    run("init", "-q", "-b", "feat/proof")
    run("commit", "-q", "--allow-empty", "-m", "x")
    sha = run("rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(repo)
    assert commands._local_git_head() == (sha, "feat/proof")

    run("checkout", "-q", "--detach")
    assert commands._local_git_head() == (None, None), "detached HEAD meldet nichts"

    monkeypatch.chdir(tmp_path)
    assert commands._local_git_head() == (None, None), "ausserhalb von git meldet nichts"
