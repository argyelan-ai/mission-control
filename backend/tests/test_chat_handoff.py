"""The handoff package of a chat switch (ADR-105, plan S3 §2.0, rules F3–F5).

``build_handoff`` is pure, so these tests feed it event dicts of the exact
shapes ``transcript_chat.py`` (Claude Code) and ``omp_chat.py`` (omp)
produce and that the record stores in ``chat_events.payload``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import chat_handoff
from app.services.chat_handoff import Pair, budget_for_window, build_handoff

GOLDEN = Path(__file__).parent / "fixtures" / "chat_handoff" / "golden_package.md"

OLD = Pair(harness="claude", model="opus", session_id="s-old")
NEW = Pair(harness="omp", runtime_id="r-1", model="qwen-38b", runtime_label="Local box")


class _Chat:
    title = "Refactor the loader"
    workspace_path = "/work/demo"


def _msg(n: int, role: str, text: str, **extra) -> dict:
    return {"kind": "message", "uuid": f"m{n}", "ts": f"2026-11-19T10:{n // 60:02d}:{n % 60:02d}.000Z",
            "role": role, "text": text, "model": None, "sidechain": False, **extra}


def _build(events, budget=8_000, **kw):
    return build_handoff(events, chat=_Chat(), old_pair=OLD, new_pair=NEW, budget_tokens=budget,
                         today="2026-11-19", **kw)


# ── F3 · budget ─────────────────────────────────────────────────────────────


def test_truncates_oldest_first():
    """200 messages × 1 500 chars, window 32 000 → budget 8 000 tokens: the
    package fits, the oldest messages are the ones that drop out, header and
    recap stay whole."""
    events = [{"kind": "tool", "uuid": "t0", "name": "Edit", "detail": {"file_path": "/work/demo/a.py"}}]
    events += [_msg(i, "user" if i % 2 else "assistant", f"<{i:03d}>" + "x" * 1_495) for i in range(200)]
    budget = budget_for_window(32_000)
    assert budget == 8_000

    pkg = _build(events, budget=budget)

    assert pkg.est_tokens <= budget
    assert len(pkg.text) <= budget * chat_handoff.CHARS_PER_TOKEN
    assert pkg.truncated is True
    assert pkg.messages_total == 200
    assert 0 < pkg.messages_included < 200
    # The newest is in, the oldest is out, and the kept ones are contiguous.
    assert "<199>" in pkg.text and "<000>" not in pkg.text
    first_kept = 200 - pkg.messages_included
    assert f"<{first_kept:03d}>" in pkg.text and f"<{first_kept - 1:03d}>" not in pkg.text
    # Reading order: oldest kept first.
    assert pkg.text.index(f"<{first_kept:03d}>") < pkg.text.index("<199>")
    assert chat_handoff.RULES_LINE in pkg.text
    assert "- Files changed: a.py" in pkg.text
    assert "- Before: claude · opus" in pkg.text and "- Now: omp · Local box · qwen-38b" in pkg.text


def test_nothing_truncated_when_it_fits():
    pkg = _build([_msg(1, "user", "hi"), _msg(2, "assistant", "hello")])
    assert pkg.truncated is False
    assert (pkg.messages_included, pkg.messages_total) == (2, 2)


def test_one_message_is_clipped_to_max_msg_chars():
    pkg = _build([_msg(1, "assistant", "y" * 10_000)], budget=32_000)
    assert "y" * chat_handoff.MAX_MSG_CHARS not in pkg.text
    assert "y" * (chat_handoff.MAX_MSG_CHARS - 1) + "…" in pkg.text


# ── F4 · floor and cap ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("window", "budget"),
    [(8_000, 8_000), (1_000_000, 32_000), (64_000, 16_000), (None, 8_000), (0, 8_000)],
)
def test_budget_floor_and_cap(window, budget):
    assert budget_for_window(window) == budget


def test_floor_is_8000_from_m1():
    """M1: a 4k package lost ~25 points against 8k. A 16k window (25 % =
    4 000) and a 32k window both get the 8 000 floor; above it the share
    counts again."""
    assert chat_handoff.BUDGET_FLOOR_TOKENS == 8_000
    assert budget_for_window(16_000) == 8_000
    assert budget_for_window(32_000) == 8_000
    assert budget_for_window(40_000) == 10_000
    assert budget_for_window(200_000) == 32_000  # cap: unmeasured, kept


def test_token_estimate_is_3_3_chars_per_token():
    """M1: 4 chars/token made packages ~20 % larger than estimated. A full
    8 000-token package now holds at most 26 400 characters, not 32 000."""
    assert chat_handoff.est_tokens("x" * 33) == 10
    assert chat_handoff.est_tokens("x" * 26_400) == 8_000
    assert chat_handoff.est_tokens("x" * 26_401) == 8_001
    events = [_msg(i, "user" if i % 2 else "assistant", "x" * 1_500) for i in range(100)]
    pkg = _build(events, budget=8_000)
    assert len(pkg.text) <= 26_400
    assert pkg.est_tokens <= 8_000
    assert len(pkg.text) > 26_400 - chat_handoff.MAX_MSG_CHARS - 100  # the room is used


def test_max_msg_chars_stays_2000():
    """M1: keep 2 000 while the floor is at least 8 000 (600 only for a 4k
    floor)."""
    assert chat_handoff.MAX_MSG_CHARS == 2_000
    assert chat_handoff.BUDGET_FLOOR_TOKENS >= 8_000


def test_window_prefers_the_runtime_row_then_the_model_name():
    class _Rt:
        max_context_len = 131_072

    assert chat_handoff.window_for(_Rt(), "opus") == 131_072
    assert chat_handoff.window_for(None, None) is None


# ── F5 · recap fidelity ─────────────────────────────────────────────────────


def _edit_events():
    return [
        {"kind": "tool", "uuid": "c1", "name": "Edit", "toolUseId": "tu1",
         "detail": {"file_path": "/work/demo/src/a.py"}},
        {"kind": "tool", "uuid": "o1", "name": "write", "toolUseId": "tu2", "detail": {"path": "docs/b.md"}},
        {"kind": "tool", "uuid": "o2", "name": "read", "toolUseId": "tu3", "detail": {"path": "c.txt"}},
    ]


def test_recap_lists_changed_files():
    pkg = _build(_edit_events())
    assert pkg.files == ["a.py", "b.md"]
    assert "- Files changed: a.py, b.md" in pkg.text
    assert "- Tools used: 3" in pkg.text
    # Sabotage of the input: without the Edit event a.py is gone.
    pkg2 = _build(_edit_events()[1:])
    assert "a.py" not in pkg2.text and pkg2.files == ["b.md"]


def test_recap_open_todos_and_open_question():
    events = [
        {"kind": "todo", "uuid": "td1", "source": "TodoWrite",
         "items": [{"content": "old", "status": "pending"}]},
        {"kind": "todo", "uuid": "td2", "source": "TodoWrite", "items": [
            {"content": "write tests", "status": "in_progress"},
            {"content": "ship", "status": "pending"},
            {"content": "plan", "status": "completed"},
        ]},
        {"kind": "elicitation", "uuid": "e1", "elicitationId": "q1", "message": "answered one?", "schema": {}},
        {"kind": "elicitation_result", "uuid": "e2", "elicitationId": "q1", "status": "accept"},
        {"kind": "elicitation", "uuid": "e3", "elicitationId": "q2", "message": "Which branch?", "schema": {}},
    ]
    pkg = _build(events)
    assert "- Open todos: [in_progress] write tests; [pending] ship" in pkg.text
    assert "old" not in pkg.text and "plan" not in pkg.text
    assert pkg.open_question == "Which branch?"
    assert "- Open question: Which branch?" in pkg.text


def test_recap_keeps_the_chat_goal_when_the_tail_drops_it():
    """M1's cheap step instead of layer 3: the first operator message is in
    the recap even after it fell out of the tail; clipped to MAX_MSG_CHARS,
    masked before the clip, one line."""
    token = "sk-ant-" + "a" * 40
    goal = f"GOAL-START key {token}\nsecond line " + "g" * 5_000
    events = [_msg(0, "assistant", "hello first"), _msg(1, "user", goal)]
    events += [_msg(i, "user" if i % 2 else "assistant", f"<{i:03d}>" + "x" * 1_495) for i in range(2, 200)]
    pkg = _build(events)
    assert pkg.truncated is True
    tail = pkg.text.split("### Last messages", 1)[1]
    assert "GOAL-START" not in tail
    line = next(ln for ln in pkg.text.splitlines() if ln.startswith("- Chat goal (first operator message): "))
    assert "GOAL-START key" in line and "second line" in line
    assert token[:20] not in pkg.text
    assert line.endswith("…")
    assert len(line) - len("- Chat goal (first operator message): ") == chat_handoff.MAX_MSG_CHARS
    # No operator message, no goal line.
    assert "Chat goal" not in _build([_msg(1, "assistant", "only me")]).text


def test_no_open_question_when_answered():
    events = [
        {"kind": "elicitation", "uuid": "e1", "elicitationId": "q1", "message": "ok?", "schema": {}},
        {"kind": "elicitation_result", "uuid": "e2", "elicitationId": "q1", "status": "accept"},
    ]
    assert "- Open question: none" in _build(events).text


def test_thinking_sidechain_and_tool_results_stay_out():
    events = [
        {"kind": "thinking", "uuid": "th", "text": "SECRET-THOUGHT", "sidechain": False},
        _msg(1, "assistant", "helper said SIDE", sidechain=True),
        {"kind": "tool", "uuid": "t", "name": "Bash", "detail": {"command": "ls"}, "result": "BIG-RESULT"},
        _msg(2, "teammate", "TEAMMATE-NOTE"),
        _msg(3, "user", "visible"),
    ]
    text = _build(events).text
    for hidden in ("SECRET-THOUGHT", "SIDE", "BIG-RESULT", "TEAMMATE-NOTE"):
        assert hidden not in text
    assert "visible" in text


def test_secrets_are_masked_before_the_clip():
    token = "sk-ant-" + "a" * 40
    pkg = _build([_msg(1, "assistant", "x" * (chat_handoff.MAX_MSG_CHARS - 20) + f" key {token}")])
    assert token[:20] not in pkg.text


def test_deterministic():
    events = _golden_events()
    assert _build(events).text == _build(events).text


# ── Golden: 40 real-shaped events, both harness shapes ──────────────────────


def _golden_events() -> list[dict]:
    """40 events: a Claude Code stretch (Edit/Write/Bash, TodoWrite, usage,
    thinking) followed by an omp stretch (write/edit with the bracket path,
    an open elicitation), as the record stores them."""
    ev: list[dict] = []
    n = 0

    def add(e):
        nonlocal n
        n += 1
        ev.append({"uuid": f"g{n}", "ts": f"2026-11-19T09:{n:02d}:00.000Z", **e})

    add({"kind": "message", "role": "user", "text": "Rename the config loader to settings_loader.", "sidechain": False})
    add({"kind": "thinking", "text": "I should look at loader.py first.", "sidechain": False})
    add({"kind": "tool", "name": "Read", "toolUseId": "t1", "title": "Read loader.py",
         "detail": {"file_path": "/work/demo/app/loader.py"}, "result": "def load(): ...", "status": "done"})
    add({"kind": "message", "role": "assistant", "text": "Looking at app/loader.py.", "sidechain": False})
    add({"kind": "tool", "name": "Bash", "toolUseId": "t2", "title": "git mv",
         "detail": {"command": "git mv app/loader.py app/settings_loader.py"}, "result": "", "status": "done"})
    add({"kind": "tool", "name": "Edit", "toolUseId": "t3", "title": "Edit settings_loader.py",
         "detail": {"file_path": "/work/demo/app/settings_loader.py"}, "result": "ok", "status": "done"})
    add({"kind": "tool", "name": "Edit", "toolUseId": "t4", "title": "Edit main.py",
         "detail": {"file_path": "/work/demo/app/main.py"}, "result": "ok", "status": "done"})
    add({"kind": "todo", "source": "TodoWrite", "items": [
        {"content": "Rename loader", "status": "completed"},
        {"content": "Fix imports", "status": "in_progress"},
        {"content": "Run the tests", "status": "pending"},
    ]})
    add({"kind": "usage", "inputTokens": 12000, "outputTokens": 300, "model": "claude-opus-4-1",
         "contextWindow": 200000})
    add({"kind": "message", "role": "assistant", "text": "Renamed loader.py to settings_loader.py and fixed main.py.",
         "sidechain": False})
    for i in range(10):
        add({"kind": "message", "role": "user" if i % 2 == 0 else "assistant",
             "text": f"Claude turn {i}: checking module {i}.", "sidechain": False})
    add({"kind": "tool", "name": "Write", "toolUseId": "t5", "title": "Write notes",
         "detail": {"file_path": "/work/demo/NOTES.md"}, "result": "ok", "status": "done"})
    add({"kind": "message", "role": "user", "text": "Now continue on the local model.", "sidechain": False})
    # omp stretch.
    add({"kind": "message", "role": "user", "text": "Add a test for settings_loader.", "sidechain": False})
    add({"kind": "thinking", "text": "omp reasoning noise", "sidechain": False})
    add({"kind": "tool", "name": "write", "toolUseId": "o1", "title": "write",
         "detail": {"path": "tests/test_settings_loader.py"}, "result": "written", "status": "done"})
    add({"kind": "tool", "name": "edit", "toolUseId": "o2", "title": "edit",
         "detail": {"input": "[app/settings_loader.py#ab12]\n- old\n+ new"}, "result": "ok", "status": "done"})
    add({"kind": "tool", "name": "bash", "toolUseId": "o3", "title": "pytest",
         "detail": {"command": "pytest -q"}, "result": "3 passed", "status": "done"})
    add({"kind": "message", "role": "assistant", "text": "Added tests/test_settings_loader.py; 3 passed.",
         "sidechain": False})
    add({"kind": "usage", "inputTokens": 9000, "outputTokens": 200, "model": "qwen-38b", "contextWindow": 131072})
    for i in range(10):
        add({"kind": "message", "role": "user" if i % 2 == 0 else "assistant",
             "text": f"omp turn {i}: step {i} done.", "sidechain": False})
    add({"kind": "elicitation", "elicitationId": "q-1", "message": "Push the branch now?",
         "schema": {"properties": {"ok": {"type": "boolean"}}}, "planApproval": False})
    assert len(ev) == 40
    return ev


def test_golden_package_for_both_harness_shapes():
    pkg = _build(_golden_events())
    expected = GOLDEN.read_text(encoding="utf-8")
    assert pkg.text == expected, "golden changed — review the diff, then refresh the fixture on purpose"
    assert pkg.files == ["settings_loader.py", "main.py", "NOTES.md", "test_settings_loader.py"]
    assert pkg.tool_count == 8
    assert pkg.open_question == "Push the branch now?"
    assert (pkg.messages_included, pkg.messages_total) == (26, 26)
