"""Helpers (subagents) in the chat stream — what the Claude Code parser has to
hand the frontend so it can say "4 helpers · 3 working" truthfully.

Why this exists: a delegated job in today's Claude Code is almost always a
BACKGROUND spawn. Its tool result arrives within a second and only says
"launched" (``toolUseResult.status == "async_launched"``, or
``"teammate_spawned"`` for a team member). The chat used to read "has a
result" as "finished" — every helper showed as done the moment it started.
The completion arrives later as a ``<task-notification>`` (status + its own
``<duration_ms>``). Fixture lines keep the real JSONL structure with neutral
content.
"""
import json
from pathlib import Path

from app.services.transcript_adapters import adapter_for
from app.services.transcript_chat import (
    _merge_tool_result,
    clean_helpers,
    parse_transcript_line,
    read_history,
)

SPAWN = (
    '{"type":"assistant","uuid":"a1","timestamp":"2026-11-13T10:26:40Z","isSidechain":false,'
    '"message":{"role":"assistant","model":"claude-opus-5-5","content":[{"type":"tool_use",'
    '"id":"toolu_A","name":"Agent","input":{"description":"Read the docs","prompt":"read",'
    '"run_in_background":true}}]}}'
)


def _result_line(tool_use_id: str, tool_use_result: dict, *, uuid: str = "u1",
                 ts: str = "2026-11-13T10:26:41Z", is_error: bool = False) -> str:
    return json.dumps({
        "type": "user", "uuid": uuid, "timestamp": ts, "isSidechain": False,
        "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": "Async agent launched successfully.\nagent_id: x"}],
            "is_error": is_error,
        }]},
        "toolUseResult": tool_use_result,
    })


ASYNC_RESULT = _result_line("toolu_A", {
    "isAsync": True, "status": "async_launched", "agentId": "ab717b9bd5f79182d",
    "description": "Read the docs",
})

TEAMMATE_RESULT = _result_line("toolu_A", {
    "status": "teammate_spawned", "agentId": "acfd-729ae2a6", "name": "cfd",
    "teammate_id": "cfd@session-1",
})

SYNC_RESULT = _result_line("toolu_A", {
    "status": "completed", "agentId": "a42", "totalDurationMs": 88915,
    "content": [{"type": "text", "text": "report"}],
})

NOTIFICATION = json.dumps({
    "type": "user", "uuid": "n1", "timestamp": "2026-11-13T10:28:10Z", "isSidechain": False,
    "message": {"role": "user", "content": (
        "<task-notification>\n<task-id>ab717b9bd5f79182d</task-id>\n"
        "<tool-use-id>toolu_A</tool-use-id>\n<output-file>/tmp/x.output</output-file>\n"
        "<status>completed</status>\n<summary>Agent \"Read the docs\" finished</summary>\n"
        "<result>long report</result>\n<usage><subagent_tokens>87712</subagent_tokens>"
        "<tool_uses>11</tool_uses><duration_ms>88915</duration_ms></usage>\n</task-notification>"
    )},
})


def _tool_result(line: str) -> dict:
    (ev,) = [e for e in parse_transcript_line(line) if e["kind"] == "_tool_result"]
    return ev


def test_background_spawn_is_working_not_done():
    ev = _tool_result(ASYNC_RESULT)
    assert ev["helpers"] == [{"id": "ab717b9bd5f79182d", "name": None, "agent": None,
                              "state": "working", "durationMs": None}]


def test_teammate_spawn_is_working_and_keeps_its_name():
    ev = _tool_result(TEAMMATE_RESULT)
    assert ev["helpers"][0]["state"] == "working"
    assert ev["helpers"][0]["name"] == "cfd"


def test_foreground_spawn_is_done_with_its_measured_duration():
    ev = _tool_result(SYNC_RESULT)
    assert ev["helpers"][0]["state"] == "done"
    assert ev["helpers"][0]["durationMs"] == 88915


def test_failed_spawn_is_failed():
    ev = _tool_result(_result_line("toolu_A", {"status": "completed", "agentId": "a1"}, is_error=True))
    assert ev["helpers"][0]["state"] == "failed"


def test_ordinary_tool_result_has_no_helpers():
    line = _result_line("toolu_B", {"stdout": "ok", "stderr": "", "interrupted": False})
    assert "helpers" not in _tool_result(line)


def test_unknown_launch_status_says_nothing_instead_of_guessing():
    line = _result_line("toolu_A", {"status": "something_new", "agentId": "a1"})
    assert "helpers" not in _tool_result(line)


def test_two_results_in_one_line_are_never_given_the_lines_helper():
    """``toolUseResult`` belongs to the LINE. With two results in it nobody
    can say which one it describes — so neither gets it."""
    d = json.loads(ASYNC_RESULT)
    block = d["message"]["content"][0]
    d["message"]["content"] = [block, {**block, "tool_use_id": "toolu_Z"}]
    evs = [e for e in parse_transcript_line(json.dumps(d)) if e["kind"] == "_tool_result"]
    assert len(evs) == 2
    assert all("helpers" not in e for e in evs)


def test_merge_puts_helpers_on_the_card():
    tool_ev = {"kind": "tool", "toolUseId": "toolu_A", "status": "done", "result": None}
    _merge_tool_result(tool_ev, _tool_result(ASYNC_RESULT))
    assert tool_ev["helpers"][0]["state"] == "working"


def test_notification_carries_the_measured_duration():
    (ev,) = parse_transcript_line(NOTIFICATION)
    assert ev["kind"] == "notification"
    assert ev["status"] == "completed"
    assert ev["taskId"] == "ab717b9bd5f79182d"
    assert ev["durationMs"] == 88915


def test_notification_without_usage_has_no_duration():
    line = NOTIFICATION.replace(
        "<usage><subagent_tokens>87712</subagent_tokens><tool_uses>11</tool_uses>"
        "<duration_ms>88915</duration_ms></usage>", "")
    (ev,) = parse_transcript_line(line)
    assert ev["durationMs"] is None


def test_clean_helpers_fails_closed():
    assert clean_helpers(None) is None
    assert clean_helpers("x") is None
    assert clean_helpers([{"id": "a", "state": "exploded"}]) is None
    assert clean_helpers([{"id": 3, "state": "done", "durationMs": -5, "name": "n" * 500}]) == [
        {"id": None, "name": "n" * 200, "agent": None, "state": "done", "durationMs": None}
    ]


def test_history_end_to_end_background_helper(tmp_path: Path):
    """The whole path the chat loads: spawn, launch result, completion."""
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([SPAWN, ASYNC_RESULT, NOTIFICATION]) + "\n")
    out = read_history(path, adapter=adapter_for(None))
    (tool,) = [e for e in out["events"] if e["kind"] == "tool"]
    assert tool["helpers"][0]["state"] == "working"
    assert tool["helpers"][0]["id"] == "ab717b9bd5f79182d"
    (note,) = [e for e in out["events"] if e["kind"] == "notification"]
    assert note["toolUseId"] == "toolu_A"
    assert note["durationMs"] == 88915
