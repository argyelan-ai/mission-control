"""Tests for read_history — streams a transcript JSONL file into one page
of chat events, merging tool_result content onto its tool event, deduping
repeated lines, and paging backward via before_uuid.

Fixture lines are synthetic copies of the real Claude Code JSONL schema
(structure kept, content neutralized — no personal data, no real paths).
"""
from __future__ import annotations

import json
import os
import time

from app.services.transcript_chat import read_history

# ── Fixture builders ─────────────────────────────────────────────────────────


def _write_jsonl(path, lines):
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _user(uuid, ts, text):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_text(uuid, ts, text):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "id": f"msg_{uuid}",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_tool(uuid, ts, text, tool_id, tool_name, tool_input):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "id": f"msg_{uuid}",
            "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input},
            ],
        },
    }


def _assistant_multi_tool(uuid, ts, tools):
    """``tools`` is a list of (tool_use_id, name, input) tuples — used to
    fixture a single assistant turn issuing several parallel tool calls."""
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "id": f"msg_{uuid}",
            "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": inp}
                for tid, name, inp in tools
            ],
        },
    }


def _assistant_usage(uuid, ts, input_tokens=100, output_tokens=50):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "id": f"msg_{uuid}",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "content": [{"type": "text", "text": "usage carrier"}],
        },
    }


def _tool_result(uuid, ts, tool_use_id, content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {"role": "user", "content": [block]},
    }


def _multi_tool_result(uuid, ts, results):
    """``results`` is a list of (tool_use_id, content) tuples, one
    tool_result content block per entry, in the given order."""
    blocks = [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        for tool_use_id, content in results
    ]
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {"role": "user", "content": blocks},
    }


def _history_lines():
    """8 physical lines: a tool_use -> tool_result pair split across two
    entries, a verbatim-repeated line (dedup case), and plain messages —
    matches the brief's Step-1 fixture shape."""
    return [
        _user("h1", "2026-08-13T10:00:00Z", "start task"),
        _assistant_tool(
            "h2", "2026-08-13T10:00:01Z", "on it", "rt1", "Read", {"file_path": "/app/a.py"}
        ),
        _tool_result("h3", "2026-08-13T10:00:02Z", "rt1", "file contents"),
        _assistant_text("h4", "2026-08-13T10:00:03Z", "done reading"),
        _user("h5", "2026-08-13T10:00:04Z", "thanks"),
        _user("h1", "2026-08-13T10:00:00Z", "start task"),  # repeated line (resume artifact)
        _assistant_text("h6", "2026-08-13T10:00:05Z", "no problem"),
        _user("h7", "2026-08-13T10:00:06Z", "bye"),
    ]


_EXPECTED_KIND_UUIDS = [
    ("message", "h1"),
    ("message", "h2"),
    ("tool", "h2"),
    ("message", "h4"),
    ("message", "h5"),
    ("message", "h6"),
    ("message", "h7"),
]


# ── merge + dedup + session metadata ─────────────────────────────────────────


def test_read_history_merges_tool_result_and_dedups(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=200)
    events = result["events"]

    # 8 lines in, 1 exact repeat deduped, 1 tool_result merged (not a
    # standalone event) -> 7 events.
    assert [(e["kind"], e["uuid"]) for e in events] == _EXPECTED_KIND_UUIDS

    tool_event = events[2]
    assert tool_event["toolUseId"] == "rt1"
    assert tool_event["result"] == "file contents"
    assert tool_event["status"] == "done"

    assert result["hasMore"] is False
    assert result["session"]["sessionId"] == "session"
    assert result["session"]["startedAt"] == "2026-08-13T10:00:00Z"


def test_read_history_internal_tool_result_never_in_output(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=200)

    assert all(e["kind"] != "_tool_result" for e in result["events"])


def test_read_history_tool_result_error_flips_status(tmp_path):
    f = tmp_path / "session.jsonl"
    lines = [
        _assistant_tool(
            "e1", "2026-08-13T10:00:00Z", "trying", "et1", "Bash", {"command": "false"}
        ),
        _tool_result("e2", "2026-08-13T10:00:01Z", "et1", "command failed", is_error=True),
    ]
    _write_jsonl(f, lines)

    result = read_history(f, limit=200)
    tool_event = [e for e in result["events"] if e["kind"] == "tool"][0]

    assert tool_event["status"] == "error"
    assert tool_event["result"] == "command failed"


def test_read_history_result_truncated_to_4000_chars(tmp_path):
    f = tmp_path / "session.jsonl"
    long_content = "x" * 5000
    lines = [
        _assistant_tool("t1", "2026-08-13T10:00:00Z", "reading", "tt1", "Read", {"file_path": "/a"}),
        _tool_result("t2", "2026-08-13T10:00:01Z", "tt1", long_content),
    ]
    _write_jsonl(f, lines)

    result = read_history(f, limit=200)
    tool_event = [e for e in result["events"] if e["kind"] == "tool"][0]

    assert len(tool_event["result"]) == 4000


def test_read_history_unmatched_tool_result_is_dropped_silently(tmp_path):
    f = tmp_path / "session.jsonl"
    lines = [_tool_result("o1", "2026-08-13T10:00:00Z", "no-such-tool", "orphan result")]
    _write_jsonl(f, lines)

    result = read_history(f, limit=200)

    assert result["events"] == []


# ── parallel tool_use / tool_result matching by id ───────────────────────────


def test_read_history_parallel_tool_results_match_by_toolUseId(tmp_path):
    f = tmp_path / "session.jsonl"
    lines = [
        _assistant_multi_tool(
            "p1",
            "2026-08-13T10:00:00Z",
            [
                ("pt1", "Read", {"file_path": "/app/a.py"}),
                ("pt2", "Read", {"file_path": "/app/b.py"}),
            ],
        ),
        # Results arrive in a later entry, deliberately out of id order.
        _multi_tool_result(
            "p2", "2026-08-13T10:00:01Z", [("pt2", "b contents"), ("pt1", "a contents")]
        ),
    ]
    _write_jsonl(f, lines)

    result = read_history(f, limit=200)
    tools = {e["toolUseId"]: e for e in result["events"] if e["kind"] == "tool"}

    assert tools.keys() == {"pt1", "pt2"}
    assert tools["pt1"]["result"] == "a contents"
    assert tools["pt2"]["result"] == "b contents"


# ── stats (Edit/Write line counts) ───────────────────────────────────────────


def test_read_history_edit_stats_computed_from_old_new_string(tmp_path):
    f = tmp_path / "session.jsonl"
    line = _assistant_tool(
        "s1",
        "2026-08-13T10:00:00Z",
        "editing",
        "st1",
        "Edit",
        {"file_path": "/app/a.py", "old_string": "line1\nline2", "new_string": "line1\nline2\nline3"},
    )
    _write_jsonl(f, [line])

    result = read_history(f, limit=200)
    tool_event = [e for e in result["events"] if e["kind"] == "tool"][0]

    assert tool_event["stats"] == {"additions": 3, "deletions": 2}


def test_read_history_no_stats_for_non_edit_write_tools(tmp_path):
    f = tmp_path / "session.jsonl"
    line = _assistant_tool(
        "s2", "2026-08-13T10:00:00Z", "reading", "st2", "Read", {"file_path": "/app/a.py"}
    )
    _write_jsonl(f, [line])

    result = read_history(f, limit=200)
    tool_event = [e for e in result["events"] if e["kind"] == "tool"][0]

    assert tool_event["stats"] is None


def test_read_history_no_stats_when_write_has_no_old_new_string(tmp_path):
    f = tmp_path / "session.jsonl"
    line = _assistant_tool(
        "s3",
        "2026-08-13T10:00:00Z",
        "writing",
        "st3",
        "Write",
        {"file_path": "/app/new.py", "content": "print('hi')"},
    )
    _write_jsonl(f, [line])

    result = read_history(f, limit=200)
    tool_event = [e for e in result["events"] if e["kind"] == "tool"][0]

    assert tool_event["stats"] is None


# ── paging (before_uuid, hasMore) ────────────────────────────────────────────


def test_read_history_default_limit_returns_newest_and_sets_hasMore(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=3)

    assert [(e["kind"], e["uuid"]) for e in result["events"]] == [
        ("message", "h5"),
        ("message", "h6"),
        ("message", "h7"),
    ]
    assert result["hasMore"] is True


def test_read_history_before_uuid_pages_backward(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=2, before_uuid="h6")

    assert [(e["kind"], e["uuid"]) for e in result["events"]] == [
        ("message", "h4"),
        ("message", "h5"),
    ]
    assert result["hasMore"] is True


def test_read_history_before_uuid_excludes_whole_entry_not_just_first_event(tmp_path):
    # before_uuid="h2" must exclude BOTH events derived from that entry
    # (the message and the tool use), not cut mid-entry.
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=200, before_uuid="h2")

    assert [(e["kind"], e["uuid"]) for e in result["events"]] == [("message", "h1")]
    assert result["hasMore"] is False


def test_read_history_before_uuid_reaching_start_sets_hasMore_false(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=200, before_uuid="h4")

    assert [(e["kind"], e["uuid"]) for e in result["events"]] == [
        ("message", "h1"),
        ("message", "h2"),
        ("tool", "h2"),
    ]
    assert result["hasMore"] is False


def test_read_history_before_uuid_unknown_returns_empty_page(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())

    result = read_history(f, limit=200, before_uuid="does-not-exist")

    assert result["events"] == []
    assert result["hasMore"] is False


# ── session metadata (live flag, missing file) ───────────────────────────────


def test_read_history_live_flag_recent(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())
    now = time.time()
    os.utime(f, (now, now))

    result = read_history(f, limit=200)

    assert result["session"]["live"] is True


def test_read_history_live_flag_stale(tmp_path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(f, _history_lines())
    old = time.time() - 300
    os.utime(f, (old, old))

    result = read_history(f, limit=200)

    assert result["session"]["live"] is False


def test_read_history_missing_file_returns_empty_session(tmp_path):
    result = read_history(tmp_path / "does-not-exist.jsonl", limit=200)

    assert result == {
        "events": [],
        "session": {"sessionId": "does-not-exist", "live": False, "startedAt": None},
        "hasMore": False,
    }


def test_read_history_malformed_and_blank_lines_skipped(tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text(
        "\n".join(
            [
                json.dumps(_user("m1", "2026-08-13T10:00:00Z", "hi")),
                "",
                "not json",
                json.dumps(["not", "a", "dict"]),
                json.dumps(_user("m2", "2026-08-13T10:00:01Z", "bye")),
            ]
        )
        + "\n"
    )

    result = read_history(f, limit=200)

    assert [(e["kind"], e["uuid"]) for e in result["events"]] == [
        ("message", "m1"),
        ("message", "m2"),
    ]


def test_read_history_surfaces_string_content_user_turn(tmp_path):
    """Fix round 5: real interactive user turns write message.content as a
    plain string, not a list of blocks — read_history must surface them
    exactly like a normal text-block message (existing merge/dedup logic
    untouched, since the normalization happens inside parse_transcript_line)."""
    f = tmp_path / "session.jsonl"
    string_content_line = {
        "type": "user",
        "uuid": "m3",
        "timestamp": "2026-08-13T10:00:02Z",
        "isSidechain": False,
        "message": {"role": "user", "content": "typed straight into the TUI"},
    }
    _write_jsonl(f, [_user("m1", "2026-08-13T10:00:00Z", "hi"), string_content_line])

    result = read_history(f, limit=200)

    assert [(e["kind"], e["uuid"], e.get("text")) for e in result["events"]] == [
        ("message", "m1", "hi"),
        ("message", "m3", "typed straight into the TUI"),
    ]


# ── usage events: statusline-state source stamping ──────────────────────────
#
# resolve_transcript_dir shapes an agent's transcript dir as
# <claude-config>/projects/<encoded-cwd>/, and find_active_session returns a
# .jsonl file directly inside it — so a session file's path is
# <claude-config>/projects/<encoded-cwd>/<session>.jsonl. These fixtures
# replicate that same nesting (3 levels below <claude-config>) so
# _claude_config_root's "three up from the file" derivation resolves to a
# real, writable statusline-state/ dir, exactly like on disk.


def _session_file(tmp_path):
    """Builds <tmp_path>/claude-config/projects/-encoded-/session.jsonl and
    returns (session_file, claude_config_root)."""
    tdir = tmp_path / "claude-config" / "projects" / "-home-agent"
    tdir.mkdir(parents=True)
    return tdir / "session.jsonl", tmp_path / "claude-config"


def test_read_history_usage_event_source_cli_when_statusline_fresh(tmp_path):
    f, claude_config_root = _session_file(tmp_path)
    _write_jsonl(f, [_assistant_usage("u1", "2026-08-13T10:00:00Z")])

    state_dir = claude_config_root / "statusline-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session.json").write_text(
        json.dumps(
            {
                "context_window": {
                    "context_window_size": 1_000_000,
                    "used_percentage": 37.5,
                    "current_usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            }
        )
    )

    result = read_history(f, limit=200)
    usage = next(e for e in result["events"] if e["kind"] == "usage")

    assert usage["usedPct"] == 37.5
    assert usage["source"] == "cli"
    # contextWindow_size straight from the CLI's own state OVERRIDES the
    # settings.context_windows estimate parse_transcript_line stamped
    # earlier (model "claude-sonnet-4-6" -> 200_000) — ground truth wins.
    assert usage["contextWindow"] == 1_000_000


def test_read_history_usage_event_source_estimate_when_no_statusline_state(tmp_path):
    f, _claude_config_root = _session_file(tmp_path)
    _write_jsonl(f, [_assistant_usage("u1", "2026-08-13T10:00:00Z")])

    result = read_history(f, limit=200)
    usage = next(e for e in result["events"] if e["kind"] == "usage")

    assert usage["usedPct"] is None
    assert usage["source"] == "estimate"
    # contextWindow (the static estimate) is untouched — resolve_context_window
    # still ran inside parse_transcript_line.
    assert usage["contextWindow"] == 200_000


def test_read_history_usage_event_source_estimate_when_statusline_stale(tmp_path):
    import os
    import time

    f, claude_config_root = _session_file(tmp_path)
    _write_jsonl(f, [_assistant_usage("u1", "2026-08-13T10:00:00Z")])

    state_dir = claude_config_root / "statusline-state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "session.json"
    state_file.write_text(
        json.dumps(
            {"context_window": {"used_percentage": 90.0, "current_usage": {}}}
        )
    )
    # Backdate mtime well past the 120s freshness window.
    stale = time.time() - 300
    os.utime(state_file, (stale, stale))

    result = read_history(f, limit=200)
    usage = next(e for e in result["events"] if e["kind"] == "usage")

    assert usage["source"] == "estimate"
    assert usage["usedPct"] is None
