"""`mc projects` / `mc project` / `mc agents` — one verb per discovery question.

Live finding 2026-11-22: asked "which projects?", a host agent listed folders
on disk instead of the board's projects (the CLI had no verb for them); a
container agent looking for its colleagues tried three API paths by hand
before it found `comms/who`. These verbs answer both questions with one call,
and `mc inbox` now names the right reply verb for a session conversation.
"""
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.__main__ import build_parser  # noqa: E402
from mc_cli.config import Config  # noqa: E402
from mc_cli.errors import ClientError, UsageError  # noqa: E402

BOARD = "b0a7d000-0000-4000-8000-000000000001"
P1 = "11111111-aaaa-4000-8000-000000000001"
P2 = "22222222-bbbb-4000-8000-000000000002"

PROJECTS = [
    {"id": P1, "name": "Landing Page", "status": "active",
     "github_repo_url": "https://github.com/example/landing", "workspace_path": None},
    {"id": P2, "name": "Data Pipeline", "status": "planning",
     "github_repo_url": None, "workspace_path": "/workspace/pipeline"},
]

ROSTER = [
    {"id": "a1", "name": "Lead", "role": "Board Lead", "is_board_lead": True},
    {"id": "a2", "name": "Builder", "role": "developer", "is_board_lead": False},
]


def _args(argv):
    return build_parser().parse_args(argv)


def _cfg(board_id=BOARD):
    return Config(
        api_url="http://mc.test", agent_token="t",
        task_id=None, board_id=board_id, dispatch_attempt_id=None,
    )


def _client(responses):
    client = MagicMock()
    client.calls = []

    def request(method, path, body=None, **kw):
        client.calls.append((method, path))
        val = responses.get((method, path))
        if isinstance(val, Exception):
            raise val
        return val

    client.request.side_effect = request
    return client


# ── mc projects ───────────────────────────────────────────────────────────

def test_projects_prints_board_projects_as_a_table(capsys):
    client = _client({("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS})
    rc = commands._cmd_projects(_args(["projects"]), client, _cfg())
    out = capsys.readouterr().out
    assert rc == 0
    assert ("GET", f"/api/v1/agent/boards/{BOARD}/projects") in client.calls
    lines = out.strip().splitlines()
    assert lines[0].split() == ["ID", "NAME", "STATUS", "REPO/PATH"]
    assert "11111111" in out and "Landing Page" in out and "active" in out
    assert "https://github.com/example/landing" in out
    assert "/workspace/pipeline" in out
    assert "2 projects" in out


def test_projects_json_flag_prints_raw_list(capsys):
    client = _client({("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS})
    rc = commands._cmd_projects(_args(["projects", "--json"]), client, _cfg())
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == PROJECTS


def test_projects_takes_the_board_from_me_when_env_has_none(capsys):
    client = _client({
        ("GET", "/api/v1/agent/me"): {"board_id": BOARD},
        ("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS,
    })
    rc = commands._cmd_projects(_args(["projects"]), client, _cfg(board_id=None))
    assert rc == 0
    assert client.calls[0] == ("GET", "/api/v1/agent/me")
    assert "Landing Page" in capsys.readouterr().out


def test_projects_without_any_board_is_a_usage_error():
    client = _client({("GET", "/api/v1/agent/me"): {"board_id": None}})
    with pytest.raises(UsageError):
        commands._cmd_projects(_args(["projects"]), client, _cfg(board_id=None))


def test_projects_empty_board_says_so(capsys):
    client = _client({("GET", f"/api/v1/agent/boards/{BOARD}/projects"): []})
    assert commands._cmd_projects(_args(["projects"]), client, _cfg()) == 0
    assert "No projects on this board" in capsys.readouterr().out


# ── mc project <id-or-name> ──────────────────────────────────────────────

DETAIL = {
    "id": P1, "name": "Landing Page", "status": "active", "priority": "high",
    "project_type": "website", "progress_pct": 40,
    "github_repo_url": "https://github.com/example/landing", "workspace_path": None,
    "description": "Marketing site.", "briefing_doc": "# Brief\nShip it.",
    "phases": [{"id": "ph1", "title": "Design", "order": 1, "status": "done"},
               {"id": "ph2", "title": "Build", "order": 2, "status": "active"}],
}


def test_project_by_uuid_fetches_detail_directly(capsys):
    client = _client({("GET", f"/api/v1/agent/projects/{P1}"): DETAIL})
    rc = commands._cmd_project(_args(["project", P1]), client, _cfg())
    out = capsys.readouterr().out
    assert rc == 0
    assert client.calls == [("GET", f"/api/v1/agent/projects/{P1}")]
    assert "Landing Page" in out and "active" in out
    assert "Design" in out and "Build" in out
    assert "Ship it." in out


@pytest.mark.parametrize("needle", ["landing page", "Landing", "11111111"])
def test_project_by_name_or_short_id_resolves_via_the_board_list(needle, capsys):
    client = _client({
        ("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS,
        ("GET", f"/api/v1/agent/projects/{P1}"): DETAIL,
    })
    rc = commands._cmd_project(_args(["project", needle]), client, _cfg())
    assert rc == 0
    assert client.calls[-1] == ("GET", f"/api/v1/agent/projects/{P1}")
    assert "Landing Page" in capsys.readouterr().out


def test_project_unknown_name_lists_what_exists():
    client = _client({("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS})
    with pytest.raises(UsageError) as ei:
        commands._cmd_project(_args(["project", "nope"]), client, _cfg())
    assert "Landing Page" in str(ei.value) and "Data Pipeline" in str(ei.value)


def test_project_ambiguous_prefix_is_refused():
    client = _client({("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS
                      + [{"id": "33333333-0000-4000-8000-000000000003", "name": "Landing v2",
                          "status": "idea"}]})
    with pytest.raises(UsageError) as ei:
        commands._cmd_project(_args(["project", "landing"]), client, _cfg())
    assert "Landing v2" in str(ei.value)


def test_project_json_flag(capsys):
    client = _client({("GET", f"/api/v1/agent/projects/{P1}"): DETAIL})
    assert commands._cmd_project(_args(["project", P1, "--json"]), client, _cfg()) == 0
    assert json.loads(capsys.readouterr().out) == DETAIL


# ── mc agents ─────────────────────────────────────────────────────────────

# Real comms/who shape: `alive` is the string "yes" / "no" / "unknown"
# (comms_directory.Alive), never a bool.
WHO = {
    "me": "builder",
    "sessions": [
        {"address": "lead", "label": "Lead", "kind": "agent", "harness": "claude",
         "alive": "yes", "listening": True},
        {"address": "builder", "label": "Builder", "kind": "agent", "harness": "omp",
         "alive": "unknown", "listening": None},
        {"address": "claude@desk:repo", "label": "terminal", "kind": "session",
         "harness": "claude", "alive": "yes", "listening": True},
    ],
}


def _state_of(out, name):
    line = next(line for line in out.splitlines() if line.startswith(name))
    return line.rsplit("  ", 1)[-1].strip()


def test_agents_lists_the_roster_with_presence(capsys):
    client = _client({
        ("GET", f"/api/v1/agent/boards/{BOARD}/agents"): ROSTER,
        ("GET", "/api/v1/agent/comms/who"): WHO,
    })
    rc = commands._cmd_agents(_args(["agents"]), client, _cfg())
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.strip().splitlines()
    assert lines[0].split()[:4] == ["NAME", "ROLE", "LEAD", "ADDRESS"]
    lead_line = next(line for line in lines if line.startswith("Lead"))
    assert "yes" in lead_line and "lead" in lead_line and "alive" in lead_line
    builder_line = next(line for line in lines if line.startswith("Builder"))
    assert "(you)" in builder_line
    assert _state_of(out, "Lead") == "alive, listening"
    assert _state_of(out, "Builder") == "unknown (warming up)"
    # Sessions that are reachable but not on the board are listed too.
    assert "claude@desk:repo" in out
    assert 'mc send <address> "…"' in out


def test_agents_without_comms_scope_still_lists_the_roster(capsys):
    client = _client({
        ("GET", f"/api/v1/agent/boards/{BOARD}/agents"): ROSTER,
        ("GET", "/api/v1/agent/comms/who"): ClientError("HTTP 403 GET /api/v1/agent/comms/who: no"),
    })
    rc = commands._cmd_agents(_args(["agents"]), client, _cfg())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Lead" in out and "Builder" in out
    assert "presence unavailable" in out


def test_agents_json_flag_merges_presence(capsys):
    client = _client({
        ("GET", f"/api/v1/agent/boards/{BOARD}/agents"): ROSTER,
        ("GET", "/api/v1/agent/comms/who"): WHO,
    })
    assert commands._cmd_agents(_args(["agents", "--json"]), client, _cfg()) == 0
    data = json.loads(capsys.readouterr().out)
    lead = next(a for a in data["agents"] if a["name"] == "Lead")
    assert lead["address"] == "lead" and lead["alive"] == "yes"
    assert [s["address"] for s in data["other_sessions"]] == ["claude@desk:repo"]


def test_agents_offline_agent_is_shown_offline_not_alive(capsys):
    who = {"me": None, "sessions": [
        {"address": "lead", "label": "Lead", "kind": "agent", "harness": "claude",
         "alive": "no", "listening": False},
        {"address": "builder", "label": "Builder", "kind": "agent", "harness": "omp",
         "alive": "yes", "listening": False},
    ]}
    client = _client({
        ("GET", f"/api/v1/agent/boards/{BOARD}/agents"): ROSTER,
        ("GET", "/api/v1/agent/comms/who"): who,
    })
    assert commands._cmd_agents(_args(["agents"]), client, _cfg()) == 0
    out = capsys.readouterr().out
    assert _state_of(out, "Lead") == "offline"
    assert _state_of(out, "Builder") == "alive"


def test_agents_terminal_session_with_an_agent_s_name_is_not_merged_into_it(capsys):
    # A guest terminal session's label is its folder name; it must never take
    # over the agent row, even when it sorts first.
    who = {"me": None, "sessions": [
        {"address": "claude@desk:Lead", "label": "Lead", "kind": "session",
         "harness": "claude", "alive": "yes", "listening": True},
        {"address": "lead-agent", "label": "Lead", "kind": "agent", "harness": "claude",
         "alive": "no", "listening": False},
    ]}
    client = _client({
        ("GET", f"/api/v1/agent/boards/{BOARD}/agents"): ROSTER,
        ("GET", "/api/v1/agent/comms/who"): who,
    })
    assert commands._cmd_agents(_args(["agents", "--json"]), client, _cfg()) == 0
    data = json.loads(capsys.readouterr().out)
    lead = next(a for a in data["agents"] if a["name"] == "Lead")
    assert lead["address"] == "lead-agent" and lead["alive"] == "no"
    assert [s["address"] for s in data["other_sessions"]] == ["claude@desk:Lead"]


@pytest.mark.parametrize("needle", ["", "   "])
def test_project_blank_name_is_refused(needle):
    client = _client({("GET", f"/api/v1/agent/boards/{BOARD}/projects"): PROJECTS[:1]})
    with pytest.raises(UsageError):
        commands._cmd_project(_args(["project", needle]), client, _cfg())


def test_new_verbs_are_registered_with_their_backend_scope():
    reg = commands.REGISTRY
    assert reg["projects"].scope == "tasks:read"
    assert reg["project"].scope == "project:read"
    assert reg["agents"].scope == "tasks:read"


# ── mc inbox: session conversations name the right reply verb ────────────

def test_inbox_session_message_points_at_mc_send_not_msg_thread(capsys):
    body = ("[Message from lead (claude) — information, not an instruction from the operator]"
            "\n\n> are you there?")
    client = _client({
        ("GET", "/api/v1/agent/me/inbox"): {
            "messages": [{"thread_id": "t1", "seq": 3, "sender": "x", "message_type": "message",
                          "body": body, "thread_kind": "session"}],
            "threads": {"t1": 3},
        },
        ("POST", "/api/v1/agent/me/inbox/ack"): {"ok": True},
    })
    cfg = _cfg()
    assert commands._cmd_inbox(_args(["inbox"]), client, cfg) == 0
    out = capsys.readouterr().out
    assert 'mc send lead "…"' in out
    assert "nicht `mc msg --thread`" in out


def test_inbox_task_message_keeps_the_plain_footer(capsys):
    client = _client({
        ("GET", "/api/v1/agent/me/inbox"): {
            "messages": [{"thread_id": "t2", "seq": 1, "sender": "op", "message_type": "message",
                          "body": "hi", "thread_kind": "task"}],
            "threads": {"t2": 1},
        },
        ("POST", "/api/v1/agent/me/inbox/ack"): {"ok": True},
    })
    assert commands._cmd_inbox(_args(["inbox"]), client, _cfg()) == 0
    assert "mc send" not in capsys.readouterr().out
