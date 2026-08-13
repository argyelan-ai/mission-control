"""Tests for the pure transcript-line -> chat-event parser.

Fixture lines are trimmed, redacted copies of the real Claude Code JSONL
schema (structure kept, content neutralized — no personal data).
"""
from app.services.transcript_chat import build_tool_title, parse_transcript_line

# ── Fixture lines ──────────────────────────────────────────────────────────

USER_LINE = '{"type":"user","uuid":"u1","timestamp":"2026-08-13T10:00:00Z","isSidechain":false,"message":{"role":"user","content":[{"type":"text","text":"fix the bug"}]}}'

CMD_LINE = '{"type":"user","uuid":"u2","timestamp":"2026-08-13T10:00:01Z","isSidechain":false,"message":{"role":"user","content":[{"type":"text","text":"/model sonnet"}]}}'

ASSIST_LINE = '{"type":"assistant","uuid":"a1","timestamp":"2026-08-13T10:00:02Z","isSidechain":false,"message":{"role":"assistant","model":"claude-sonnet-4-6","id":"msg_x","usage":{"input_tokens":100,"cache_read_input_tokens":900,"output_tokens":50},"content":[{"type":"thinking","thinking":"hmm"},{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/app/main.py"}},{"type":"text","text":"done"}]}}'

TOOL_RESULT_LINE = '{"type":"user","uuid":"u3","timestamp":"2026-08-13T10:00:03Z","isSidechain":false,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"file contents here"}]}}'

SIDECHAIN_LINE = '{"type":"user","uuid":"u4","timestamp":"2026-08-13T10:00:04Z","isSidechain":true,"message":{"role":"user","content":[{"type":"text","text":"sub-agent prompt"}]}}'

USAGE_WITH_EFFORT_LINE = '{"type":"assistant","uuid":"a2","timestamp":"2026-08-13T10:00:05Z","isSidechain":false,"effort":"high","message":{"role":"assistant","model":"claude-opus-5","id":"msg_y","usage":{"input_tokens":10,"output_tokens":5},"content":[{"type":"text","text":"ok"}]}}'


# ── type == "user" ──────────────────────────────────────────────────────────


def test_user_text():
    evs = parse_transcript_line(USER_LINE)
    assert evs == [
        {
            "kind": "message",
            "uuid": "u1",
            "ts": "2026-08-13T10:00:00Z",
            "role": "user",
            "text": "fix the bug",
            "model": None,
            "sidechain": False,
        }
    ]


def test_slash_command():
    evs = parse_transcript_line(CMD_LINE)
    assert len(evs) == 1
    assert evs[0]["kind"] == "command"
    assert evs[0]["command"] == "/model sonnet"
    assert evs[0]["uuid"] == "u2"
    assert evs[0]["ts"] == "2026-08-13T10:00:01Z"


def test_slash_with_newline_is_message_not_command():
    line = (
        '{"type":"user","uuid":"u5","timestamp":"2026-08-13T10:00:06Z",'
        '"isSidechain":false,"message":{"role":"user","content":'
        '[{"type":"text","text":"/not-a-command\\nmore text"}]}}'
    )
    evs = parse_transcript_line(line)
    assert evs == [
        {
            "kind": "message",
            "uuid": "u5",
            "ts": "2026-08-13T10:00:06Z",
            "role": "user",
            "text": "/not-a-command\nmore text",
            "model": None,
            "sidechain": False,
        }
    ]


def test_tool_result_emits_internal_event():
    evs = parse_transcript_line(TOOL_RESULT_LINE)
    assert evs == [
        {
            "kind": "_tool_result",
            "tool_use_id": "toolu_1",
            "content": "file contents here",
        }
    ]


def test_sidechain_flag_set():
    evs = parse_transcript_line(SIDECHAIN_LINE)
    assert len(evs) == 1
    assert evs[0]["sidechain"] is True


# ── type == "assistant" ─────────────────────────────────────────────────────


def test_assistant_line_emits_thinking_tool_text_usage():
    evs = parse_transcript_line(ASSIST_LINE)
    kinds = [e["kind"] for e in evs]
    assert kinds == ["thinking", "tool", "message", "usage"]


def test_assistant_thinking_event_shape():
    evs = parse_transcript_line(ASSIST_LINE)
    thinking = evs[0]
    assert thinking == {
        "kind": "thinking",
        "uuid": "a1",
        "ts": "2026-08-13T10:00:02Z",
        "text": "hmm",
        "sidechain": False,
    }


def test_assistant_tool_use_event_shape():
    evs = parse_transcript_line(ASSIST_LINE)
    tool = evs[1]
    assert tool["kind"] == "tool"
    assert tool["uuid"] == "a1"
    assert tool["ts"] == "2026-08-13T10:00:02Z"
    assert tool["name"] == "Read"
    assert tool["title"] == "Read main.py"
    assert tool["detail"] == {"file_path": "/app/main.py"}
    assert tool["result"] is None
    assert tool["status"] == "done"
    assert tool["stats"] is None
    assert tool["sidechain"] is False


def test_assistant_text_event_shape():
    evs = parse_transcript_line(ASSIST_LINE)
    message = evs[2]
    assert message == {
        "kind": "message",
        "uuid": "a1",
        "ts": "2026-08-13T10:00:02Z",
        "role": "assistant",
        "text": "done",
        "model": "claude-sonnet-4-6",
        "sidechain": False,
    }


def test_assistant_usage_event_shape():
    evs = parse_transcript_line(ASSIST_LINE)
    usage = evs[3]
    assert usage == {
        "kind": "usage",
        "uuid": "a1",
        "ts": "2026-08-13T10:00:02Z",
        "inputTokens": 1000,  # 100 + 900 cache_read + 0 cache_creation
        "outputTokens": 50,
        "model": "claude-sonnet-4-6",
        "effort": None,
    }


def test_usage_effort_from_top_level_entry():
    evs = parse_transcript_line(USAGE_WITH_EFFORT_LINE)
    usage = [e for e in evs if e["kind"] == "usage"][0]
    assert usage["effort"] == "high"
    assert usage["inputTokens"] == 10
    assert usage["outputTokens"] == 5
    assert usage["model"] == "claude-opus-5"


def test_tool_use_detail_truncated_over_2000_chars():
    long_value = "x" * 3000
    line = (
        '{"type":"assistant","uuid":"a3","timestamp":"2026-08-13T10:00:07Z",'
        '"isSidechain":false,"message":{"role":"assistant","model":"claude-sonnet-4-6",'
        '"id":"msg_z","content":[{"type":"tool_use","id":"toolu_2","name":"Bash",'
        f'"input":{{"command":"{long_value}"}}}}]}}}}'
    )
    evs = parse_transcript_line(line)
    tool = evs[0]
    assert tool["kind"] == "tool"
    assert len(tool["detail"]["command"]) <= 2001  # truncated + ellipsis
    assert tool["detail"]["command"].endswith("…")


# ── unknown / malformed ─────────────────────────────────────────────────────


def test_unknown_and_garbage():
    assert parse_transcript_line('{"type":"mode","mode":"plan"}') == []
    assert parse_transcript_line('{"type":"file-history-snapshot"}') == []
    assert parse_transcript_line('{"type":"queue-operation"}') == []
    assert parse_transcript_line("not json") == []


def test_missing_fields_returns_empty_list():
    assert parse_transcript_line('{"type":"user"}') == []
    assert parse_transcript_line('{"type":"assistant"}') == []
    assert parse_transcript_line("") == []


# ── build_tool_title ────────────────────────────────────────────────────────


def test_build_tool_title_read():
    assert build_tool_title("Read", {"file_path": "/x/y.py"}) == "Read y.py"


def test_build_tool_title_write():
    assert build_tool_title("Write", {"file_path": "/x/y.py"}) == "Write y.py"


def test_build_tool_title_notebook_edit():
    assert build_tool_title("NotebookEdit", {"file_path": "/x/nb.ipynb"}) == "Read nb.ipynb"


def test_build_tool_title_edit():
    assert build_tool_title("Edit", {"file_path": "/a/b/c.ts"}) == "Edit c.ts"


def test_build_tool_title_bash():
    assert build_tool_title("Bash", {"command": "ls -la"}) == "$ ls -la"


def test_build_tool_title_grep():
    assert build_tool_title("Grep", {"pattern": "TODO"}) == 'Search "TODO"'


def test_build_tool_title_glob():
    assert build_tool_title("Glob", {"pattern": "**/*.py"}) == 'Search "**/*.py"'


def test_build_tool_title_websearch():
    assert build_tool_title("WebSearch", {"query": "claude sdk"}) == 'Web "claude sdk"'


def test_build_tool_title_webfetch():
    assert build_tool_title("WebFetch", {"url": "https://example.com/page"}) == "Fetch example.com"


def test_build_tool_title_task():
    assert build_tool_title("Task", {"description": "research foo"}) == "Agent: research foo"


def test_build_tool_title_agent():
    assert build_tool_title("Agent", {"description": "audit bar"}) == "Agent: audit bar"


def test_build_tool_title_fallback():
    assert build_tool_title("SomeCustomTool", {"x": 1}) == "SomeCustomTool"


def test_build_tool_title_truncates_to_80_chars():
    title = build_tool_title("Bash", {"command": "x" * 200})
    assert len(title) == 80
    assert title.endswith("…")
