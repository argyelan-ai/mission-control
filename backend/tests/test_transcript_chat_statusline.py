"""Tests for read_statusline_state / _claude_config_root — the reader half
of the CLI-statusline-as-context-truth feature. docker/shared/statusline-mc.sh
is the writer (a shell script, not unit-testable here); these tests cover
what the backend does with what that script leaves on disk: fresh state
wins, stale/missing/malformed all fall back to None (-> "estimate" at the
call sites in read_history / ChatTailerManager, covered in
test_transcript_chat_history.py and test_agent_chat_router.py).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.services.transcript_chat import _claude_config_root, read_statusline_state


def _write_state(state_dir: Path, session_id: str, payload: dict, *, age_seconds: float = 0.0) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / f"{session_id}.json"
    f.write_text(json.dumps(payload))
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(f, (stamp, stamp))
    return f


_VALID_PAYLOAD = {
    "session_id": "sess1",
    "context_window": {
        "context_window_size": 1_000_000,
        "used_percentage": 55.25,
        "current_usage": {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 10,
        },
    },
}


def test_read_statusline_state_fresh_returns_pct_and_tokens(tmp_path):
    _write_state(tmp_path / "statusline-state", "sess1", _VALID_PAYLOAD)

    result = read_statusline_state(tmp_path, "sess1")

    assert result == {
        "usedPct": 55.25,
        "usedTokens": 1050,  # 100+40+900+10
        "contextWindowSize": 1_000_000,
        "components": {
            "input": 100,
            "cacheRead": 900,
            "cacheCreation": 10,
            "output": 40,
        },
    }


def test_read_statusline_state_stale_returns_none(tmp_path):
    _write_state(tmp_path / "statusline-state", "sess1", _VALID_PAYLOAD, age_seconds=300)

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_just_under_freshness_window_is_fresh(tmp_path):
    _write_state(tmp_path / "statusline-state", "sess1", _VALID_PAYLOAD, age_seconds=100)

    assert read_statusline_state(tmp_path, "sess1") is not None


def test_read_statusline_state_missing_file_returns_none(tmp_path):
    assert read_statusline_state(tmp_path, "no-such-session") is None


def test_read_statusline_state_missing_state_dir_returns_none(tmp_path):
    # tmp_path itself exists but statusline-state/ was never created — the
    # common case for every agent that hasn't sent a claude-code prompt yet,
    # and for Boss (whose ~/.claude isn't managed by this codebase at all).
    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_malformed_json_returns_none(tmp_path):
    state_dir = tmp_path / "statusline-state"
    state_dir.mkdir()
    (state_dir / "sess1.json").write_text("not json")

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_missing_context_window_key_returns_none(tmp_path):
    _write_state(tmp_path / "statusline-state", "sess1", {"session_id": "sess1"})

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_missing_current_usage_key_returns_none(tmp_path):
    _write_state(
        tmp_path / "statusline-state",
        "sess1",
        {"context_window": {"used_percentage": 10.0}},
    )

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_missing_context_window_size_key_returns_none(tmp_path):
    """context_window_size is required just like used_percentage/current_usage
    — a state file predating this field (or a broken write) must fall back
    to the static estimate rather than surface a half-parsed result."""
    _write_state(
        tmp_path / "statusline-state",
        "sess1",
        {
            "context_window": {
                "used_percentage": 10.0,
                "current_usage": {"input_tokens": 1},
            }
        },
    )

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_non_dict_json_returns_none(tmp_path):
    state_dir = tmp_path / "statusline-state"
    state_dir.mkdir()
    (state_dir / "sess1.json").write_text(json.dumps(["not", "a", "dict"]))

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_non_numeric_used_percentage_returns_none(tmp_path):
    _write_state(
        tmp_path / "statusline-state",
        "sess1",
        {
            "context_window": {
                "used_percentage": "not-a-number",
                "current_usage": {"input_tokens": 1},
            }
        },
    )

    assert read_statusline_state(tmp_path, "sess1") is None


def test_read_statusline_state_missing_usage_fields_default_to_zero(tmp_path):
    _write_state(
        tmp_path / "statusline-state",
        "sess1",
        {
            "context_window": {
                "context_window_size": 200_000,
                "used_percentage": 1.0,
                "current_usage": {"input_tokens": 5},
            }
        },
    )

    result = read_statusline_state(tmp_path, "sess1")

    assert result == {
        "usedPct": 1.0,
        "usedTokens": 5,
        "contextWindowSize": 200_000,
        "components": {"input": 5, "cacheRead": 0, "cacheCreation": 0, "output": 0},
    }


def test_claude_config_root_three_levels_above_session_file():
    session_path = Path("/x/claude-config/projects/-home-agent/sess1.jsonl")

    assert _claude_config_root(session_path) == Path("/x/claude-config")


def test_read_statusline_state_components_sum_to_used_tokens(tmp_path):
    """The breakdown view derives "Frei" as window minus the sum, so the parts
    must add up to the total the same reader reports."""
    _write_state(tmp_path / "statusline-state", "sess1", _VALID_PAYLOAD)

    result = read_statusline_state(tmp_path, "sess1")

    assert sum(result["components"].values()) == result["usedTokens"]
