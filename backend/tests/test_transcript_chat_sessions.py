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


def test_find_active_session_live_flag_boundary_true_at_59s(tmp_path, monkeypatch):
    f = tmp_path / "session-boundary.jsonl"
    f.write_text('{"type":"user"}\n')
    fixed_mtime = 1_800_000_000.0
    os.utime(f, (fixed_mtime, fixed_mtime))
    monkeypatch.setattr(tc.time, "time", lambda: fixed_mtime + 59)

    _, meta = tc.find_active_session(tmp_path)

    assert meta["live"] is True


def test_find_active_session_live_flag_boundary_false_at_61s(tmp_path, monkeypatch):
    f = tmp_path / "session-boundary.jsonl"
    f.write_text('{"type":"user"}\n')
    fixed_mtime = 1_800_000_000.0
    os.utime(f, (fixed_mtime, fixed_mtime))
    monkeypatch.setattr(tc.time, "time", lambda: fixed_mtime + 61)

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


# ── resolve_aliveness ─────────────────────────────────────────────────────────
#
# Fixes the old live-only semantics: mtime<60s was the ONLY signal, so an
# idle-but-still-running CLI read as "ended" everywhere (operator-visible
# bug). "live" itself (find_active_session/read_history) is untouched for
# backward compat; aliveness is the new, richer "active"|"idle"|"ended"
# classification layered on top.


async def test_resolve_aliveness_active_when_recent_mtime(tmp_path):
    f = tmp_path / "session.jsonl"
    _touch(f, mtime_offset_seconds=5)
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    assert await tc.resolve_aliveness(agent, f) == "active"


async def test_resolve_aliveness_ended_on_rollover(tmp_path):
    """A newer session file now exists in the same directory — this one was
    superseded, regardless of what the process check would say."""
    old = tmp_path / "session-old.jsonl"
    new = tmp_path / "session-new.jsonl"
    _touch(old, mtime_offset_seconds=300)
    _touch(new, mtime_offset_seconds=1)
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    assert await tc.resolve_aliveness(agent, old) == "ended"


async def test_resolve_aliveness_idle_when_docker_process_alive(tmp_path, monkeypatch):
    f = tmp_path / "session.jsonl"
    _touch(f, mtime_offset_seconds=300)
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    async def _fake_process_alive(a):
        return True

    monkeypatch.setattr(tc, "process_alive", _fake_process_alive)

    assert await tc.resolve_aliveness(agent, f) == "idle"


async def test_resolve_aliveness_ended_when_docker_process_dead(tmp_path, monkeypatch):
    f = tmp_path / "session.jsonl"
    _touch(f, mtime_offset_seconds=300)
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    async def _fake_process_alive(a):
        return False

    monkeypatch.setattr(tc, "process_alive", _fake_process_alive)

    assert await tc.resolve_aliveness(agent, f) == "ended"


async def test_resolve_aliveness_idle_fallback_within_max_age_for_host_agent(tmp_path, monkeypatch):
    """Boss/host: process_alive has no channel (returns None) — falls back
    to the transcript-age heuristic. Recent enough (well under 12h) -> idle."""
    f = tmp_path / "session.jsonl"
    _touch(f, mtime_offset_seconds=300)
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert await tc.resolve_aliveness(agent, f) == "idle"


async def test_resolve_aliveness_ended_fallback_beyond_max_age(tmp_path):
    """Stale well beyond the 12h fallback window -> ended, whether that's
    because there's no process channel (Boss/host) or the docker check
    itself couldn't determine an answer."""
    f = tmp_path / "session.jsonl"
    _touch(f, mtime_offset_seconds=13 * 3600)
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert await tc.resolve_aliveness(agent, f) == "ended"


async def test_resolve_aliveness_idle_fallback_when_docker_process_check_unknown(tmp_path, monkeypatch):
    """process_alive returning None (check itself failed/timed out, not a
    confident "dead") falls back to the same age heuristic as host — NOT a
    confident "ended" just because the probe was inconclusive."""
    f = tmp_path / "session.jsonl"
    _touch(f, mtime_offset_seconds=300)
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge")

    async def _fake_process_alive(a):
        return None

    monkeypatch.setattr(tc, "process_alive", _fake_process_alive)

    assert await tc.resolve_aliveness(agent, f) == "idle"


async def test_resolve_aliveness_ended_when_file_missing(tmp_path):
    """A session file that's disappeared entirely (mtime stat fails) has no
    live-window evidence at all — must not crash, resolves via the same
    fallback chain (no newer file either -> falls through to process/age,
    both of which report "gone" absent any mtime)."""
    f = tmp_path / "does-not-exist.jsonl"
    agent = SimpleNamespace(slug="boss", agent_runtime="host")

    assert await tc.resolve_aliveness(agent, f) == "ended"


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


def test_transcript_allowed_non_boss_host_agent_fails_closed(tmp_path):
    # Fix-round 1 (review finding): a non-Boss host agent (e.g. Hermes) must
    # NEVER fall through to the Boss cwd/branch heuristic, even against a
    # readable transcript that would pass that heuristic.
    f = tmp_path / "s.jsonl"
    _write_jsonl(
        f,
        [{"type": "user", "cwd": "/Users/x/.mc/checkouts/mission-control", "gitBranch": "main"}],
    )
    agent = SimpleNamespace(slug="hermes", agent_runtime="host")

    assert tc.transcript_allowed(agent, f) is False
