"""Harness-neutral todo snapshot event (chat progress panel).

Fixtures under ``tests/fixtures/todo/`` keep the real shapes (a Claude Code
JSONL ``TodoWrite`` line with its tool_result, an ACP ``plan`` update as omp
sends it) with neutral content.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import chat_todos
from app.services.omp_chat import OmpLineParser
from app.services.transcript_adapters import adapter_for
from app.services.transcript_chat import parse_transcript_line, read_history

FIXTURES = Path(__file__).parent / "fixtures" / "todo"
CLAUDE = adapter_for(None)


class _OmpAgent:
    harness = "omp"


OMP = adapter_for(_OmpAgent())


def _claude_lines() -> list[str]:
    return (FIXTURES / "claude_todowrite.jsonl").read_text().splitlines()


def _acp_plan() -> dict:
    return json.loads((FIXTURES / "acp_plan_update.json").read_text())


def _acp_plan_line(entry_id: str = "acp00001", ts: str = "2026-10-12T09:20:00.000Z", entries=None) -> str:
    """The transcript line the omp bridge writes for an ACP plan update
    (docker/omp-bridge/acp_chat_events.py ``_plan``)."""
    return json.dumps(
        {
            "type": "custom",
            "customType": "acp-plan",
            "data": {"entries": entries if entries is not None else _acp_plan()["update"]["entries"]},
            "id": entry_id,
            "parentId": None,
            "timestamp": ts,
        }
    )


# ── normalisation ───────────────────────────────────────────────────────────


def test_normalize_maps_status_spellings_and_keeps_priority():
    items = chat_todos.normalize_items(
        [
            {"content": "a", "status": "completed", "priority": "HIGH"},
            {"step": "b", "status": "in_progress"},
            {"title": "c", "status": "done"},
            {"text": "d", "status": "weird"},
            {"content": "  ", "status": "pending"},
            "e",
            42,
        ]
    )
    assert items == [
        {"content": "a", "status": "completed", "priority": "high"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "completed"},
        {"content": "d", "status": "pending"},
        {"content": "e", "status": "pending"},
    ]


def test_normalize_rejects_non_lists_but_keeps_an_empty_snapshot():
    assert chat_todos.normalize_items(None) is None
    assert chat_todos.normalize_items({"content": "x"}) is None
    assert chat_todos.normalize_items([]) == []


def test_normalize_caps_length_and_text():
    items = chat_todos.normalize_items([{"content": "x" * 1000}] * 80)
    assert len(items) == chat_todos.MAX_ITEMS
    assert len(items[0]["content"]) == chat_todos.MAX_TEXT_LEN + 1


def test_todo_tool_names_are_harness_neutral():
    for name in ("TodoWrite", "todo_write", "todo-write", "update_plan", "updatePlan"):
        assert chat_todos.is_todo_tool(name), name
    for name in ("todo", "Write", "plan", None, 3):
        assert not chat_todos.is_todo_tool(name), name


def test_codex_update_plan_reads_the_plan_key():
    ev = chat_todos.todo_event_from_tool(
        "update_plan",
        {"explanation": "x", "plan": [{"step": "one", "status": "in_progress"}]},
        uuid="u1",
        ts="2026-10-12T09:00:00.000Z",
    )
    assert ev["items"] == [{"content": "one", "status": "in_progress"}]
    assert ev["source"] == "update_plan"


# ── Claude Code transcript ──────────────────────────────────────────────────


def test_claude_todowrite_line_emits_a_todo_snapshot_next_to_the_tool_card():
    events = parse_transcript_line(_claude_lines()[0])
    kinds = [e["kind"] for e in events]
    assert "tool" in kinds, "the TodoWrite card itself must stay"
    todo = next(e for e in events if e["kind"] == "todo")
    assert todo == {
        "kind": "todo",
        "uuid": "7c1e2d3f-8a9b-4c5d-9e0f-1a2b3c4d5e6f",
        "ts": "2026-10-12T09:14:03.120Z",
        "source": "TodoWrite",
        "items": [
            {"content": "Read the adapter contract", "status": "completed"},
            {"content": "Write the parser test", "status": "in_progress"},
            {"content": "Wire the progress panel", "status": "pending"},
        ],
    }


def test_claude_subagent_todowrite_never_becomes_the_session_list():
    d = json.loads(_claude_lines()[0])
    d["isSidechain"] = True
    events = parse_transcript_line(json.dumps(d))
    assert [e for e in events if e["kind"] == "todo"] == []


# ── omp / ACP transcript ────────────────────────────────────────────────────


def test_omp_acp_plan_line_emits_a_todo_snapshot():
    events = OmpLineParser()(_acp_plan_line())
    assert events == [
        {
            "kind": "todo",
            "uuid": "acp00001",
            "ts": "2026-10-12T09:20:00.000Z",
            "source": "acp_plan",
            "items": [
                {"content": "Inspect the failing test", "status": "completed", "priority": "high"},
                {"content": "Patch the parser", "status": "in_progress", "priority": "medium"},
                {"content": "Run the suite", "status": "pending", "priority": "low"},
            ],
        }
    ]


def test_omp_tool_call_named_todo_write_emits_a_snapshot_but_native_todo_ops_do_not():
    def line(name, args):
        return json.dumps(
            {
                "type": "message",
                "id": "ab12cd34",
                "timestamp": "2026-10-12T09:21:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "call_1", "name": name, "arguments": args}],
                },
            }
        )

    parser = OmpLineParser()
    full = parser(line("todo_write", {"todos": [{"content": "x", "status": "pending"}]}))
    assert [e["kind"] for e in full] == ["tool", "todo"]
    # omp's native `todo` tool sends DELTAS (op=add/update) — no snapshot.
    delta = parser(line("todo", {"op": "add", "content": "x"}))
    assert [e["kind"] for e in delta] == ["tool"]


# ── history: latest snapshot wins, never in the timeline page ───────────────


def test_read_history_returns_latest_todo_snapshot_outside_the_page(tmp_path):
    lines = _claude_lines()
    later = json.loads(lines[0])
    later["uuid"] = "9e3a4b5c-0d1e-4f2a-8b3c-4d5e6f708192"
    later["timestamp"] = "2026-10-12T09:30:00.000Z"
    later["message"]["content"][0]["id"] = "toolu_01TodoWriteB"
    later["message"]["content"][0]["input"]["todos"][1]["status"] = "completed"
    filler = [
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"fill-{i}",
                "timestamp": f"2026-10-12T09:4{i}:00.000Z",
                "isSidechain": False,
                "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": f"t{i}"}]},
            }
        )
        for i in range(5)
    ]
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join([*lines, json.dumps(later), *filler]) + "\n")

    result = read_history(f, CLAUDE, limit=2)

    assert all(e["kind"] != "todo" for e in result["events"]), "todo is a slot, not a timeline row"
    assert result["todos"]["uuid"] == "9e3a4b5c-0d1e-4f2a-8b3c-4d5e6f708192"
    assert [i["status"] for i in result["todos"]["items"]] == ["completed", "completed", "pending"]


def test_read_history_without_todos_says_none(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_claude_lines()[1] + "\n")
    assert read_history(f, CLAUDE)["todos"] is None


def test_read_history_omp_acp_plan(tmp_path):
    f = tmp_path / "2026-10-12T09-20-00-000Z_01a08150-b7ac-71d0-b036-e8536d952cc3.jsonl"
    f.write_text(
        "\n".join(
            [
                _acp_plan_line("acp00001"),
                _acp_plan_line(
                    "acp00002",
                    "2026-10-12T09:25:00.000Z",
                    [{"content": "Patch the parser", "status": "completed"}],
                ),
            ]
        )
        + "\n"
    )
    result = read_history(f, OMP)
    assert result["todos"]["uuid"] == "acp00002"
    assert result["todos"]["items"] == [{"content": "Patch the parser", "status": "completed"}]
