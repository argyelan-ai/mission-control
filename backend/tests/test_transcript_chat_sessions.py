"""Tests for session resolution + the Boss privacy filter in transcript_chat.

Fixture JSONL lines are trimmed, redacted copies of the real Claude Code
schema (structure kept, content neutralized — no personal data, no real
paths/usernames).
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from app.services import transcript_chat as tc


# ── encode_cwd ──────────────────────────────────────────────────────────────


def test_encode_cwd_matches_claude_code_convention():
    assert (
        tc.encode_cwd("/Users/x/.mc/checkouts/mission-control")
        == "-Users-x--mc-checkouts-mission-control"
    )


def test_encode_cwd_container_home():
    # Claude Code's own project-dir encoding for the container agent's HOME —
    # every non-alphanumeric char (including the leading slash) becomes '-'.
    assert tc.encode_cwd("/home/agent") == "-home-agent"


# ── resolve_transcript_dir ───────────────────────────────────────────────────


def test_resolve_transcript_dir_cli_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_host_home", lambda: tmp_path)
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    result = tc.resolve_transcript_dir(agent)

    assert result == tmp_path / ".mc" / "agents" / "rex" / "claude-config" / "projects" / "-home-agent"


def test_resolve_transcript_dir_boss(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_host_home", lambda: tmp_path)
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    result = tc.resolve_transcript_dir(agent)

    checkout = str(tmp_path / ".mc" / "checkouts" / "mission-control")
    expected = tmp_path / ".claude" / "projects" / tc.encode_cwd(checkout)
    assert result == expected


def test_resolve_transcript_dir_boss_host_slug_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_host_home", lambda: tmp_path)
    agent = SimpleNamespace(slug="boss-host", agent_runtime="host")

    result = tc.resolve_transcript_dir(agent)

    checkout = str(tmp_path / ".mc" / "checkouts" / "mission-control")
    expected = tmp_path / ".claude" / "projects" / tc.encode_cwd(checkout)
    assert result == expected


def test_resolve_transcript_dir_none_for_hermes(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_host_home", lambda: tmp_path)
    agent = SimpleNamespace(slug="hermes", agent_runtime="host")

    assert tc.resolve_transcript_dir(agent) is None


def test_resolve_transcript_dir_none_for_manual(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_host_home", lambda: tmp_path)
    agent = SimpleNamespace(slug="onkel", agent_runtime="manual")

    assert tc.resolve_transcript_dir(agent) is None


def test_resolve_transcript_dir_none_when_no_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_host_home", lambda: tmp_path)
    agent = SimpleNamespace(slug=None, agent_runtime="cli-bridge")

    assert tc.resolve_transcript_dir(agent) is None


# ── find_active_session ──────────────────────────────────────────────────────


def _touch(path, mtime_offset_seconds):
    path.write_text('{"type":"user"}\n')
    ts = time.time() - mtime_offset_seconds
    os.utime(path, (ts, ts))


def test_find_active_session_newest_wins(tmp_path):
    old = tmp_path / "session-old.jsonl"
    new = tmp_path / "session-new.jsonl"
    _touch(old, mtime_offset_seconds=500)
    _touch(new, mtime_offset_seconds=10)

    result = tc.find_active_session(tmp_path)

    assert result is not None
    path, meta = result
    assert path == new
    assert meta["sessionId"] == "session-new"


def test_find_active_session_live_flag_recent(tmp_path):
    recent = tmp_path / "session-recent.jsonl"
    _touch(recent, mtime_offset_seconds=5)

    _, meta = tc.find_active_session(tmp_path)

    assert meta["live"] is True


def test_find_active_session_live_flag_stale(tmp_path):
    stale = tmp_path / "session-stale.jsonl"
    _touch(stale, mtime_offset_seconds=300)

    _, meta = tc.find_active_session(tmp_path)

    assert meta["live"] is False


def test_find_active_session_ignores_subdirectories(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    nested = sub / "session-nested.jsonl"
    _touch(nested, mtime_offset_seconds=1)

    top = tmp_path / "session-top.jsonl"
    _touch(top, mtime_offset_seconds=500)

    result = tc.find_active_session(tmp_path)

    assert result is not None
    path, meta = result
    assert path == top
    assert meta["sessionId"] == "session-top"


def test_find_active_session_empty_dir(tmp_path):
    assert tc.find_active_session(tmp_path) is None


def test_find_active_session_missing_dir(tmp_path):
    assert tc.find_active_session(tmp_path / "does-not-exist") is None


def test_find_active_session_meta_has_iso_mtime(tmp_path):
    f = tmp_path / "session-x.jsonl"
    _touch(f, mtime_offset_seconds=5)

    _, meta = tc.find_active_session(tmp_path)

    assert isinstance(meta["mtime"], str)
    assert "T" in meta["mtime"]  # ISO 8601


# ── transcript_allowed ───────────────────────────────────────────────────────


def _write_jsonl(path, lines):
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_transcript_allowed_docker_agent_always_true(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [{"type": "user", "cwd": "/Users/x/private-project", "gitBranch": "main"}])
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    assert tc.transcript_allowed(agent, f) is True


def test_transcript_allowed_boss_private_cwd_denied(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [{"type": "user", "cwd": "/Users/x/some-other-private-repo", "gitBranch": "main"}],
    )
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert tc.transcript_allowed(agent, f) is False


def test_transcript_allowed_boss_mc_cwd_granted(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [{"type": "user", "cwd": "/Users/x/.mc/checkouts/mission-control", "gitBranch": "main"}],
    )
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert tc.transcript_allowed(agent, f) is True


def test_transcript_allowed_boss_task_branch_granted(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [{"type": "user", "cwd": "/Users/x/some-other-private-repo", "gitBranch": "task/x"}],
    )
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert tc.transcript_allowed(agent, f) is True


def test_transcript_allowed_boss_looks_past_first_line(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [{"type": "summary"}] * 5 + [
        {"type": "user", "cwd": "/Users/x/.mc/checkouts/mission-control", "gitBranch": "main"}
    ]
    _write_jsonl(f, lines)
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert tc.transcript_allowed(agent, f) is True


def test_transcript_allowed_boss_unreadable_file_denied(tmp_path):
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert tc.transcript_allowed(agent, tmp_path / "does-not-exist.jsonl") is False
