"""Read endpoints for the task steps in the agent chat (spec A, PR-2).

Variant A puts a task's whole work into the agent chat, at its time. The
frontend (PR-3) needs three reads, all read-only and harness-neutral:

* ``GET /agents/{id}/chat/tasks`` — the task stubs of this chat (live ones
  always, the newest finished ones as cards), with an ETag.
* ``GET …/chat/tasks/{task_id}/steps`` — every attempt's session, slim
  (one short row per step), with a cursor so a poll only carries new rows.
* ``GET …/chat/tasks/{task_id}/steps/{row_id}`` — one step in full, on tap.

Folder layout and line shapes follow an omp agent's sessions tree; all ids,
titles and texts are made up.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import chat_task_steps, omp_chat
from app.services.chat_task_steps import comment_of, lead_of, page_rows, split_glued

SID_1 = "01a20000-0000-7000-8000-000000000001"
SID_2 = "01a20000-0000-7000-8000-000000000002"
SID_OTHER = "01a20000-0000-7000-8000-000000000003"
CHAT_SID = "01a20000-0000-7000-8000-000000000004"


# ── helpers ────────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _append(path: Path, lines: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _header(sid: str, ts: str, task_id: str | None) -> dict:
    head = {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": "", "bridge": "acp"}
    if task_id:
        head["taskId"] = task_id
    return head


class Lines:
    """Builds bridge-sink lines with increasing ids and timestamps."""

    def __init__(self, t0: datetime, start_id: int = 1):
        self.t = t0
        self.n = start_id

    def _id(self) -> str:
        val = f"acp{self.n:05x}"
        self.n += 1
        return val

    def _ts(self, step: float) -> str:
        self.t += timedelta(seconds=step)
        return _iso(self.t)

    def delivery(self, task_id: str, title: str, kind: str = "task") -> dict:
        return {"type": "message", "id": self._id(), "parentId": None, "timestamp": self._ts(0),
                "message": {"role": "user", "attribution": "mc",
                            "origin": {"kind": kind, "taskId": task_id, "title": title},
                            "content": [{"type": "text", "text":
                                         f"# Operating Card\nRules first.\n\n# New Task: {title}\nDo the thing."}]}}

    def thought(self, text: str, ms: int = 2000, step: float = 5) -> dict:
        return {"type": "message", "id": self._id(), "parentId": None, "timestamp": self._ts(step),
                "message": {"role": "assistant", "model": None,
                            "content": [{"type": "thinking", "thinking": text, "durationMs": ms}]}}

    def tool(self, command: str, *, result: str | None = "ok", error: bool = False,
             name: str = "bash", step: float = 2) -> list[dict]:
        call = f"call-{self.n:05x}"
        lines = [{"type": "message", "id": self._id(), "parentId": None, "timestamp": self._ts(step),
                  "message": {"role": "assistant", "model": None, "content": [
                      {"type": "toolCall", "id": call, "name": name,
                       "arguments": {"command": command, "i": f"$ {command}"}}]}}]
        if result is not None:
            lines.append(self.result(call, result, error=error))
        return lines

    def result(self, call: str, text: str, *, error: bool = False) -> dict:
        return {"type": "message", "id": self._id(), "parentId": None, "timestamp": self._ts(1),
                "message": {"role": "toolResult", "toolCallId": call, "toolName": "bash",
                            "content": [{"type": "text", "text": text}], "isError": error}}

    def text(self, text: str, step: float = 3) -> dict:
        return {"type": "message", "id": self._id(), "parentId": None, "timestamp": self._ts(step),
                "message": {"role": "assistant", "model": None,
                            "content": [{"type": "text", "text": text}], "stopReason": "stop"}}

    def permission(self, title: str) -> list[dict]:
        ts = self._ts(1)
        return [
            {"type": "custom_message", "customType": "acp-permission",
             "content": f"Freigabe-Anfrage (execute): {title} [Allow once | Always allow]",
             "display": True, "attribution": "agent", "id": self._id(), "parentId": None, "timestamp": ts},
            {"type": "custom_message", "customType": "acp-permission-decision",
             "content": f"Freigabe entschieden: allow_always — {title}",
             "display": True, "attribution": "agent", "id": self._id(), "parentId": None, "timestamp": ts},
        ]


def _preview(ts: str, text: str, *, started: str | None = None, chars: int | None = None) -> dict:
    line = {"type": "custom_message", "customType": "acp-preview", "content": text,
            "display": True, "attribution": "agent", "id": "acp0ffff", "parentId": None, "timestamp": ts}
    if started is not None:
        line["data"] = {"block": "thinking", "chars": chars or len(text), "startedAt": started}
    return line


@pytest.fixture(autouse=True)
def _fresh_caches():
    chat_task_steps.clear_caches()
    yield
    chat_task_steps.clear_caches()


@pytest.fixture
def acp_driver(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "omp_driver_default", "acp", raising=False)


async def _agent_and_board(make_agent, make_board, tmp_path, monkeypatch, *, harness="omp", name="agent-a"):
    board = await make_board(slug=f"b-{os.urandom(3).hex()}")
    agent = await make_agent(name=name, slug=name, harness=harness, agent_runtime="cli-bridge")
    root = tmp_path / "omp-sessions"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(omp_chat, "resolve_transcript_dir", lambda a: root)
    return board, agent, root


async def _add(*rows) -> None:
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for row in rows:
            s.add(row)
        await s.commit()


def _event(task_id, to_status: str, *, agent_id=None, from_status="inbox", at: datetime | None = None,
           reason: str | None = None):
    from app.models.task import TaskEvent

    return TaskEvent(task_id=task_id, from_status=from_status, to_status=to_status,
                     changed_by="agent", agent_id=agent_id, reason=reason,
                     created_at=(at or datetime.now(timezone.utc)).astimezone(timezone.utc))


def _utc(dt: datetime) -> datetime:
    """A DB time (aware UTC, like the models' own defaults)."""
    return dt.astimezone(timezone.utc)


def _ws(root: Path) -> Path:
    return root / "--workspace--"


def _attempt_file(root: Path, sid: str, stamp: str, lines: list[dict]) -> Path:
    return _write(_ws(root) / ".mc" / f"{stamp}_{sid}.jsonl", lines)


def _basic_attempt(task_id: str, t0: datetime) -> list[dict]:
    b = Lines(t0)
    lines = [_header(SID_1, _iso(t0), task_id), b.delivery(task_id, "Port the checklist")]
    lines += b.permission("mc ack")
    lines += b.tool("mc ack")
    lines.append(b.thought("Let me read the spec first. Then I plan the three steps."))
    lines += b.tool("cat spec.md", result="x" * 9000)
    lines += b.tool("cd /workspace && mc comment progress \"read the spec, starting\"")
    lines += b.tool("false", result="boom", error=True)
    lines.append(b.text("All done: the checklist is ported."))
    return lines


async def _steps(client: AsyncClient, agent, task, **params):
    return await client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps", params=params)


# ── pure helpers ───────────────────────────────────────────────────────────


def test_lead_of_takes_sentences_until_it_says_something():
    # A colon announces more: the lead goes on.
    assert lead_of("Now I see:\nthe config is wrong. Then more text.") == ("Now I see: the config is wrong.", True)
    # Too short (< 24 chars): the next sentence joins.
    assert lead_of("They ARE local. So the copy is not needed here.") == (
        "They ARE local. So the copy is not needed here.", False)
    # Markdown markers and inline emphasis go, whitespace collapses.
    assert lead_of("## **Plan:** run   `pytest` on the module first.") == (
        "Plan: run pytest on the module first.", False)
    # One long sentence is cut at the last space before 140 chars.
    lead, more = lead_of("word " * 60 + "end.")
    assert more is True
    assert lead.endswith("…") and len(lead) <= 141
    assert lead_of("") == ("", False)


def test_comment_of_detects_mc_comment_only_when_literal():
    def tool(cmd: str, name: str = "bash") -> dict:
        return {"kind": "tool", "name": name, "detail": {"command": cmd}}

    assert comment_of(tool('cd /workspace && mc comment progress "x y"')) == {"type": "progress", "text": "x y"}
    assert comment_of(tool("mc comment resolution 'Done; all green'")) == {
        "type": "resolution", "text": "Done; all green"}
    # Heredoc / substitution: the text is not what the command says literally.
    assert comment_of(tool("mc comment progress <<'EOF'\nhello\nEOF")) is None
    assert comment_of(tool('mc comment progress "$(cat notes.md)"')) is None
    # Broken quoting -> a plain tool row.
    assert comment_of(tool('mc comment progress "unterminated')) is None
    assert comment_of(tool("mc status")) is None
    assert comment_of(tool('mc comment progress "x"', name="edit")) is None


def _keep_thought(text: str) -> str:
    """The bridge's own cap for a stored thought (head + marker + tail)."""
    if len(text) <= 12000:
        return text
    return text[:6000] + "\n…\n" + text[-6000:]


def _thoughts_and_glued():
    t1 = "Let me start by ACKing the task, then check the environment.\n\nFirst call: `mc ack`."  # short
    t2 = "Now I read the spec and the files it names, one by one, before I change anything."
    t3_real = " ".join(f"part{i}" for i in range(2500))  # > 12,000 chars: stored capped
    t4_real = "This thought was stored differently from what was streamed, so only its start matches. " * 4
    t4 = t4_real[:250] + " (edited)"
    t5 = "Now the last step: run the tests and write the result comment for the task."
    tail = "ECONNREFUSED: Unable to connect"  # 31 chars
    glued = (t1 + "Reply piece one." + t2 + t3_real + "After capped." + t4_real + "lost"
             + t5 + tail)
    return [t1, t2, _keep_thought(t3_real), t4, t5], glued, tail


def test_split_glued_recovers_reply_pieces_and_the_tail():
    thoughts, glued, tail = _thoughts_and_glued()
    assert len(thoughts[2]) == 12003
    split = split_glued(glued, thoughts)
    assert split is not None
    # A capped thought's end is found by its kept tail; a thought that only
    # matches by its start cannot be bounded — the text after it is dropped.
    assert split.pieces == [(0, "Reply piece one."), (2, "After capped."), (-1, tail)]
    assert split.dropped == 1


def test_slim_events_splits_the_glued_line_of_every_turn():
    """Test 6b: a continue-nudge runs a NEW turn in the same session, and
    each turn wrote its own glued final line, starting with ITS first
    thought. Both are split; a permission line inside a turn does not
    start a new one."""
    def th(text, ts):
        return {"kind": "thinking", "text": text, "ts": ts}

    def reply(text, ts):
        return {"kind": "message", "role": "assistant", "text": text, "ts": ts}

    def deliver(kind, ts):
        return {"kind": "message", "role": "teammate", "text": "# Card\nDo it",
                "source": {"kind": kind, "title": "T"}, "ts": ts}

    permission = {"kind": "message", "role": "teammate", "ts": "t42",
                  "text": "Freigabe-Anfrage (execute): ls [Allow once]",
                  "source": {"kind": "system", "title": "acp-permission"}}
    one = ["Turn one thought A is long enough to say something.", "Turn one thought B with words here.",
           "Turn one thought C closing it."]
    two = ["Turn two thought X after the nudge arrives here.", "Turn two thought Y that continues work.",
           "Turn two thought Z finishing the job."]
    events = [deliver("task", "t0")] + [th(t, f"t1{i}") for i, t in enumerate(one)]
    events += [reply("".join(one) + "REPLY ONE", "t2"), deliver("nudge", "t3")]
    events += [th(two[0], "t40"), th(two[1], "t41"), permission, th(two[2], "t43")]
    events += [reply("".join(two) + "REPLY TWO", "t5")]
    texts = [(i, r) for i, r in chat_task_steps.slim_events(events, 1) if r["k"] == "text"]
    assert [r["text"] for _i, r in texts] == ["REPLY ONE", "REPLY TWO"]
    assert [r["id"] for _i, r in texts] == ["1:4.0", "1:10.0"]
    assert texts[1][1]["ts"] == "t5"  # the tail of turn two: the line's own ts
    # The detail view splits the same way (same per-turn thoughts).
    assert chat_task_steps._detail_of(events, 1, 10, 0)["text"] == "REPLY TWO"
    assert chat_task_steps._detail_of(events, 1, 10, None) is None  # the glued line itself


def test_clamp_values_caps_nested_strings_in_a_tool_detail():
    big = "y" * 50_000
    out = chat_task_steps._clamp_values({"path": "a.py", "edits": [{"new": big}], "n": 3})
    assert len(out["edits"][0]["new"]) == chat_task_steps._DETAIL_TEXT_MAX
    assert out["path"] == "a.py" and out["n"] == 3


def test_split_glued_leaves_a_normal_reply_alone():
    thoughts, _glued, _tail = _thoughts_and_glued()
    assert split_glued("All done: the checklist is ported.", thoughts) is None
    # Quoting ONE thought start is not a glued line (needs min(3, n) starts).
    assert split_glued(thoughts[0] + " — that was my plan, and it worked.", thoughts) is None
    assert split_glued("anything", []) is None


def test_page_rows_cursor_never_skips_and_never_repeats():
    a1 = [(i, {"id": f"1:{i}"}) for i in range(5)]
    a2 = [(i, {"id": f"2:{i}"}) for i in range(1500)]
    counts = [5, 1500]
    page = page_rows([a1, a2], counts, "v1.5", limit=1000)
    assert [r["id"] for r in page["rows"]][:2] == ["2:0", "2:1"]
    assert len(page["rows"]) == 1000 and page["hasMore"] is True
    assert page["cursor"] == "v1.5.1000"
    rest = page_rows([a1, a2], counts, page["cursor"], limit=1000)
    assert [r["id"] for r in rest["rows"]] == [f"2:{i}" for i in range(1000, 1500)]
    assert rest["hasMore"] is False and rest["cursor"] == "v1.5.1500"
    # Initial page: the newest rows, the older ones counted as omitted.
    first = page_rows([a1, a2], counts, None, limit=200)
    assert first["omitted"] == 1305 and first["rows"][-1]["id"] == "2:1499"
    assert first["cursor"] == "v1.5.1500" and first["reset"] is False
    # A cursor beyond the file / more attempts than exist / garbage -> reset.
    for bad in ("v1.6", "v1.5.1500.3", "nonsense", "v1.x"):
        assert page_rows([a1, a2], counts, bad, limit=10)["reset"] is True, bad


def test_page_rows_keeps_pieces_of_one_event_together():
    rows = [(0, {"id": "1:0"}), (1, {"id": "1:1.0"}), (1, {"id": "1:1.1"}), (2, {"id": "1:2"})]
    page = page_rows([rows], [3], "v1.0", limit=2)
    assert [r["id"] for r in page["rows"]] == ["1:0"]
    assert page["cursor"] == "v1.1" and page["hasMore"] is True
    nxt = page_rows([rows], [3], page["cursor"], limit=2)
    assert [r["id"] for r in nxt["rows"]] == ["1:1.0", "1:1.1"]


# ── discovery ──────────────────────────────────────────────────────────────


def test_find_task_sessions_all_attempts_deduped_safe(tmp_path):
    root = tmp_path / "omp-sessions"
    ws = _ws(root)
    tid = str(uuid.uuid4())
    t0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    second = _write(ws / ".mc" / f"2026-10-20T09-00-00_{SID_2}.jsonl", [_header(SID_2, _iso(t0 + timedelta(hours=1)), tid)])
    first = _write(ws / ".mc" / f"2026-10-20T08-00-00_{SID_1}.jsonl", [_header(SID_1, _iso(t0), tid)])
    # Same session in the old layout (beside .mc/): one attempt, the .mc/ copy kept.
    _write(ws / f"2026-10-20T08-00-00_{SID_1}.jsonl", [_header(SID_1, _iso(t0), tid)])
    # Another task, and the native file without a task id.
    _write(ws / ".mc" / f"2026-10-20T08-30-00_{SID_OTHER}.jsonl", [_header(SID_OTHER, _iso(t0), str(uuid.uuid4()))])
    _write(ws / f"2026-10-20T08-00-00-000Z_{SID_1}.jsonl", [{"type": "title"}, {"type": "session", "id": SID_1}])
    # A symlink to an attempt, and a folder symlinked to outside the tree.
    (ws / f"2026-10-20T10-00-00_{SID_OTHER}.jsonl").symlink_to(first)
    outside = tmp_path / "outside"
    _write(outside / f"2026-10-20T11-00-00_{SID_OTHER}.jsonl", [_header(SID_OTHER, _iso(t0), tid)])
    (root / "linked").symlink_to(outside, target_is_directory=True)

    found = omp_chat.find_task_sessions(root, tid, None)
    assert [f.path for f in found] == [first, second]
    # Each file carries the identity discovery's lstat saw (S3: the reader pins to it).
    assert [f.identity for f in found] == [
        (os.lstat(p).st_dev, os.lstat(p).st_ino) for p in (first, second)]
    # ``since`` skips files last written before it.
    for copy in (first, ws / f"2026-10-20T08-00-00_{SID_1}.jsonl", ws / f"2026-10-20T08-00-00-000Z_{SID_1}.jsonl"):
        os.utime(copy, (time.time() - 3600, time.time() - 3600))
    assert [f.path for f in omp_chat.find_task_sessions(root, tid, time.time() - 600)] == [second]


# ── steps ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_steps_slim_rows_kinds_caps_and_ids(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, title="Port the checklist", status="in_progress",
                           assigned_agent_id=agent.id, started_at=_utc(now - timedelta(minutes=30)),
                           created_at=_utc(now - timedelta(hours=2)))
    tid = str(task.id)
    _attempt_file(root, SID_1, "2026-10-20T08-00-00", _basic_attempt(tid, now - timedelta(minutes=30)))
    b2 = Lines(now - timedelta(minutes=10), start_id=1)  # ids restart per file (F3)
    _attempt_file(root, SID_2, "2026-10-20T09-00-00", [
        _header(SID_2, _iso(now - timedelta(minutes=10)), tid), b2.delivery(tid, "Port the checklist", kind="nudge"),
        b2.thought("Second attempt: continue where the first stopped."),
    ])

    resp = await _steps(auth_client, agent, task, limit=1000)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applies"] is True and body["hasSession"] is True and body["live"] is True
    assert [a["n"] for a in body["attempts"]] == [1, 2]
    rows = body["rows"]
    kinds = [r["k"] for r in rows]
    assert kinds == ["delivery", "system", "system", "tool", "think", "tool", "comment", "tool", "text",
                     "attempt", "think"], kinds
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("1:") for i in ids[:9]) and all(i.startswith("2:") for i in ids[9:])
    delivery, req, dec = rows[0], rows[1], rows[2]
    assert delivery["lead"] == "Port the checklist" and delivery["more"] is True
    assert req == {**req, "what": "mc ack", "request": True}
    assert dec == {**dec, "what": "mc ack", "decision": "allow_always"}
    think = rows[4]
    assert think["lead"] == "Let me read the spec first." and think["more"] is True and think["ms"] == 2000
    assert rows[5]["title"] == "$ cat spec.md" and rows[5]["st"] == "done"
    assert rows[6]["type"] == "progress" and rows[6]["text"] == "read the spec, starting"
    assert rows[7]["st"] == "error"
    assert rows[8]["text"] == "All done: the checklist is ported."
    assert rows[9]["n"] == 2
    forbidden = {"detail", "result", "diffs", "images", "locations", "usage", "todo", "text_full"}
    for row in rows:
        assert not forbidden & set(row), row
    assert body["cursor"].startswith("v1.") and body["hasMore"] is False and body["omitted"] == 0
    assert resp.headers["cache-control"] == "no-cache, private"


@pytest.mark.asyncio
async def test_steps_text_rows_are_capped(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="done", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    b = Lines(now - timedelta(minutes=30))
    _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), str(task.id)), b.delivery(str(task.id), "T"),
        b.text("y" * 5000), *b.tool("mc comment progress " + "'" + "z" * 900 + "'"),
    ])
    rows = (await _steps(auth_client, agent, task)).json()["rows"]
    text, comment = rows[1], rows[2]
    assert len(text["text"]) <= 1500 and text["more"] is True
    assert len(comment["text"]) <= 600 and comment["more"] is True


@pytest.mark.asyncio
async def test_steps_split_a_legacy_glued_final_line(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="done", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    thoughts, glued, tail = _thoughts_and_glued()
    b = Lines(now - timedelta(minutes=30))
    lines = [_header(SID_1, _iso(now - timedelta(minutes=30)), str(task.id)), b.delivery(str(task.id), "T")]
    for t in thoughts:
        lines.append(b.thought(t))
        lines += b.tool("ls")
    lines.append(b.text(glued))
    _attempt_file(root, SID_1, "2026-10-20T08-00-00", lines)

    body = (await _steps(auth_client, agent, task)).json()
    texts = [r for r in body["rows"] if r["k"] == "text"]
    assert [t["text"] for t in texts] == ["Reply piece one.", "After capped.", tail]
    assert all(t["id"].count(".") == 1 for t in texts)
    first_thought = next(r for r in body["rows"] if r["k"] == "think")
    assert texts[0]["ts"] == first_thought["ts"]  # placed right after its thought
    # The recovered piece in full.
    detail = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps/{texts[2]['id']}")
    assert detail.status_code == 200 and detail.json()["text"] == tail


@pytest.mark.asyncio
async def test_steps_cursor_delta_open_and_reset(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="in_progress", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    running = b.tool("pytest -q", result=None)
    call = running[0]["message"]["content"][0]["id"]
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T"), *running])
    first = (await _steps(auth_client, agent, task)).json()
    tool_row = first["rows"][-1]
    assert tool_row["st"] == "running"

    # 1,500 new steps (a long gap without polling), plus the running tool's result.
    _append(path, [b.result(call, "passed")] + [b.thought(f"Step {i} of the long run, thinking hard.") for i in range(1500)])
    page = (await _steps(auth_client, agent, task, after=first["cursor"], limit=1000, open=tool_row["id"])).json()
    assert len(page["rows"]) == 1000 and page["hasMore"] is True
    assert page["rows"][0]["lead"].startswith("Step 0 ")
    assert [u["id"] for u in page["updated"]] == [tool_row["id"]] and page["updated"][0]["st"] == "done"
    rest = (await _steps(auth_client, agent, task, after=page["cursor"], limit=1000)).json()
    assert len(rest["rows"]) == 500 and rest["hasMore"] is False
    assert rest["rows"][0]["lead"].startswith("Step 1000 ")
    quiet = (await _steps(auth_client, agent, task, after=rest["cursor"])).json()
    assert quiet["rows"] == [] and quiet["reset"] is False

    # The file shrank (rewritten): the cursor is beyond it -> reset + newest rows.
    _write(path, [_header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T")])
    again = (await _steps(auth_client, agent, task, after=rest["cursor"])).json()
    assert again["reset"] is True and [r["k"] for r in again["rows"]] == ["delivery"]
    # Bad ``open`` ids are refused.
    bad = await _steps(auth_client, agent, task, open="../x")
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_steps_etag_304_and_what_changes_it(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="in_progress", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T"), *b.tool("ls")])
    pfile = _write(path.parent / "previews" / f"2026-10-20T08-00-01_{SID_1}_abc123.jsonl", [
        _preview(_iso(now - timedelta(seconds=5)), "…thinking about it", started=_iso(now - timedelta(seconds=30)), chars=900),
    ])

    async def get(tag=None):
        headers = {"If-None-Match": tag} if tag else {}
        return await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps", headers=headers)

    r1 = await get()
    assert r1.status_code == 200
    live_row = r1.json()["liveRow"]
    assert live_row["block"] == "thinking" and live_row["chars"] == 900 and live_row["stale"] is False
    tag = r1.headers["etag"]
    assert (await get(tag)).status_code == 304

    _append(path, b.tool("pwd"))
    r2 = await get(tag)
    assert r2.status_code == 200
    tag = r2.headers["etag"]

    _append(pfile, [_preview(_iso(now), "…more", started=_iso(now - timedelta(seconds=30)), chars=950)])
    r3 = await get(tag)
    assert r3.status_code == 200 and r3.json()["liveRow"]["chars"] == 950
    tag = r3.headers["etag"]

    # After the task ended a preview change does not matter any more.
    from app.models.task import Task
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Task, task.id)
        row.status = "done"
        s.add(row)
        await s.commit()
    done = await get()
    assert done.json()["live"] is False and done.json()["liveRow"] is None
    _append(pfile, [_preview(_iso(now), "…late", started=_iso(now), chars=10)])
    assert (await get(done.headers["etag"])).status_code == 304


@pytest.mark.asyncio
async def test_steps_stale_flag_is_part_of_the_etag(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """N2: the client shows "no new steps for …" from ``stale`` — a 304
    that hides the flip would keep the old line forever."""
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="in_progress", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T")])
    pfile = _write(path.parent / "previews" / f"2026-10-20T08-00-01_{SID_1}_abc123.jsonl", [
        _preview(_iso(now - timedelta(minutes=9)), "…thinking", started=_iso(now - timedelta(minutes=9)), chars=10),
    ])
    nine = time.time() - 9 * 60
    os.utime(pfile, (nine, nine))
    first = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps")
    assert first.json()["liveRow"]["stale"] is False
    # Two minutes later nothing on disk changed — only the clock.
    real = chat_task_steps._now
    monkeypatch.setattr(chat_task_steps, "_now", lambda: real() + 120)
    second = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps",
                                   headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 200
    assert second.json()["liveRow"]["stale"] is True


@pytest.mark.asyncio
async def test_steps_preview_overtaken_by_the_transcript_is_not_live(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="in_progress", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=1))
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=1)), tid), b.delivery(tid, "T"), *b.tool("ls")])
    _write(path.parent / "previews" / f"2026-10-20T08-00-01_{SID_1}_abc123.jsonl", [
        _preview(_iso(now - timedelta(minutes=5)), "old answer text"),
    ])
    body = (await _steps(auth_client, agent, task)).json()
    assert body["liveRow"] is None


@pytest.mark.asyncio
async def test_steps_privacy_per_file_and_all_denied(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="done", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    first = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T")])
    _attempt_file(root, SID_2, "2026-10-20T09-00-00", [
        _header(SID_2, _iso(now - timedelta(minutes=20)), tid), b.delivery(tid, "T", kind="nudge"),
        b.thought("The second attempt thinks about the next step to take.")])

    real = omp_chat.transcript_allowed
    monkeypatch.setattr(omp_chat, "transcript_allowed", lambda a, p: p != first and real(a, p))
    body = (await _steps(auth_client, agent, task)).json()
    # The hidden attempt keeps its number: what is shown is attempt 2.
    assert [a["n"] for a in body["attempts"]] == [2]
    assert {r["k"] for r in body["rows"]} == {"attempt", "think"}
    assert all(r["id"].startswith("2:") for r in body["rows"])
    hidden = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps/1:0")
    assert hidden.status_code == 404

    monkeypatch.setattr(omp_chat, "transcript_allowed", lambda a, p: False)
    resp = await _steps(auth_client, agent, task)
    assert resp.status_code == 404 and resp.json() == {"reason": "no_transcript"}


@pytest.mark.asyncio
async def test_steps_symlink_swap_after_discovery_is_skipped(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="done", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T")])

    opened: list[int] = []
    real_open = os.open

    def spy(p, flags, *a, **kw):
        if str(p).endswith(".jsonl"):
            opened.append(flags)
        return real_open(p, flags, *a, **kw)

    monkeypatch.setattr(chat_task_steps.os, "open", spy)
    assert (await _steps(auth_client, agent, task)).status_code == 200
    assert opened and all(f & os.O_NOFOLLOW for f in opened)

    # Discovery is cached; the file is now swapped for a symlink to a foreign file.
    secret = _write(tmp_path / "foreign" / "secret.jsonl", [
        _header(SID_1, _iso(now), tid), b.text("private words of someone else")])
    path.unlink()
    path.symlink_to(secret)
    resp = await _steps(auth_client, agent, task)
    assert resp.status_code == 404 and resp.json() == {"reason": "no_transcript"}
    assert "private words" not in resp.text


@pytest.mark.asyncio
async def test_steps_parent_folder_swap_after_discovery_is_never_served(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """S3 (review MUST on #935): after discovery the agent's sandbox swaps
    the ``.mc/`` folder for a symlink to a folder holding a file of the
    SAME name. Neither the steps nor a row's detail may serve it, and the
    task's discovery is dropped so the next poll looks again."""
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="done", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T"), b.text("my own words")])
    first = (await _steps(auth_client, agent, task)).json()
    assert any(r.get("text") == "my own words" for r in first["rows"])

    planted = _write(tmp_path / "foreign" / path.name, [
        _header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T"),
        b.text("private words of someone else")])
    real_allowed = omp_chat.transcript_allowed

    def swap_right_after_the_check(a, p):
        """The window the review names: the per-file check passes, THEN
        the sandbox swaps the parent folder."""
        ok = real_allowed(a, p)
        if ok and not path.parent.is_symlink():
            path.parent.rename(path.parent.with_name(".mc-moved"))
            path.parent.symlink_to(planted.parent, target_is_directory=True)
        return ok

    monkeypatch.setattr(omp_chat, "transcript_allowed", swap_right_after_the_check)
    resp = await _steps(auth_client, agent, task)
    assert path.parent.is_symlink() and os.path.lexists(path)
    assert resp.status_code == 404 and "private words" not in resp.text
    assert (str(root), tid) not in chat_task_steps._discovery
    # The detail route, same window: undo the swap, discover again, then ask for one row.
    path.parent.unlink()
    path.parent.with_name(".mc-moved").rename(path.parent)
    monkeypatch.setattr(omp_chat, "transcript_allowed", real_allowed)
    assert (await _steps(auth_client, agent, task)).status_code == 200  # discovery cached again
    monkeypatch.setattr(omp_chat, "transcript_allowed", swap_right_after_the_check)
    detail = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps/1:2")
    assert path.parent.is_symlink()
    assert detail.status_code == 404 and "private words" not in detail.text
    # Re-discovered: the file behind the link resolves outside the tree, so it is not found at all.
    monkeypatch.setattr(omp_chat, "transcript_allowed", real_allowed)
    again = await _steps(auth_client, agent, task)
    assert "private words" not in again.text


@pytest.mark.asyncio
async def test_steps_file_replaced_after_discovery_is_skipped_then_rediscovered(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """The opened file's identity is compared with the one PINNED at
    discovery, not with a fresh lstat: a file replaced at the same name is
    not served on that poll; the next poll discovers (and pins) anew."""
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="done", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    b = Lines(now - timedelta(minutes=30))
    head = [_header(SID_1, _iso(now - timedelta(minutes=30)), tid), b.delivery(tid, "T")]
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", head + [b.text("first version")])
    assert (await _steps(auth_client, agent, task)).status_code == 200
    replacement = _write(path.with_name("tmp.jsonl"), head + [b.text("second version")])
    os.replace(replacement, path)
    resp = await _steps(auth_client, agent, task)
    assert resp.status_code == 404 and "second version" not in resp.text
    body = (await _steps(auth_client, agent, task)).json()
    assert any(r.get("text") == "second version" for r in body["rows"])


def test_open_pinned_refuses_a_symlink_and_a_file_swapped_while_opening(tmp_path, monkeypatch):
    """S3: the file read is the file discovery saw — a symlink is refused,
    and so is a file replaced at the same name (new inode) before the
    open. The compare is against the identity PINNED at discovery."""
    root = tmp_path / "root"
    path = _write(root / "ws" / "a.jsonl", [{"n": 1}])
    other = _write(root / "ws" / "b.jsonl", [{"n": 2}])
    pinned = (os.lstat(path).st_dev, os.lstat(path).st_ino)
    opened = chat_task_steps.open_pinned(root, path, pinned)
    assert opened is not None and json.loads(opened.fh.read()) == {"n": 1}
    opened.close()
    link = root / "ws" / "link.jsonl"
    link.symlink_to(path)
    assert chat_task_steps.open_pinned(root, link) is None
    # Outside ``root`` (lexically): refused before any open.
    assert chat_task_steps.open_pinned(root / "ws", root / "a.jsonl") is None

    real_open = os.open

    def racing_open(p, flags, *a, **kw):
        if str(p) == path.name:
            os.replace(other, path)
        return real_open(p, flags, *a, **kw)

    monkeypatch.setattr(chat_task_steps.os, "open", racing_open)
    assert chat_task_steps.open_pinned(root, path, pinned) is None


def test_open_pinned_never_hangs_on_a_fifo(tmp_path):
    """A file swapped for a FIFO (no writer) must be refused at once, not
    block the worker thread on ``open``."""
    import threading

    root = tmp_path / "root"
    (root / "ws").mkdir(parents=True)
    fifo = root / "ws" / "a.jsonl"
    os.mkfifo(fifo)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(chat_task_steps.open_pinned(root, fifo)), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and result == [None]


def test_open_pinned_refuses_a_parent_folder_swapped_for_a_symlink(tmp_path):
    """S3: ``O_NOFOLLOW`` guards only the LAST part of a path. A parent
    folder (``.mc/``) swapped for a symlink to a folder holding a file of
    the same name must never be read — neither through the walk nor
    because a fresh lstat would agree with the planted file."""
    root = tmp_path / "root"
    path = _write(root / "ws" / ".mc" / "a.jsonl", [{"mine": True}])
    pinned = (os.lstat(path).st_dev, os.lstat(path).st_ino)
    planted = _write(tmp_path / "elsewhere" / "a.jsonl", [{"secret": True}])
    (root / "ws" / ".mc").rename(root / "ws" / ".mc-old")
    (root / "ws" / ".mc").symlink_to(planted.parent, target_is_directory=True)
    assert chat_task_steps.open_pinned(root, path, pinned) is None
    assert chat_task_steps.open_pinned(root, path) is None  # the walk alone refuses it too


@pytest.mark.asyncio
async def test_steps_no_session_yet_and_gates(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    task = await make_task(board.id, status="inbox", assigned_agent_id=agent.id,
                           dispatched_at=_utc(datetime.now(timezone.utc)))
    body = (await _steps(auth_client, agent, task)).json()
    assert body["applies"] is True and body["hasSession"] is False and body["live"] is True
    assert body["attempts"] == [] and body["rows"] == [] and body["cursor"] == "v1"

    # A task neither assigned to this agent nor worked by it.
    other = await make_task(board.id, status="in_progress")
    resp = await _steps(auth_client, agent, other)
    assert resp.status_code == 404 and resp.json() == {"reason": "no_task"}
    missing = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{uuid.uuid4()}/steps")
    assert missing.status_code == 404 and missing.json() == {"reason": "no_task"}
    unknown = await auth_client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/tasks/{task.id}/steps")
    assert unknown.status_code == 404

    # A harness whose tasks run inside the chat: not applicable.
    claude = await make_agent(name="agent-b", slug="agent-b", harness="claude", agent_runtime="cli-bridge")
    ctask = await make_task(board.id, status="in_progress", assigned_agent_id=claude.id)
    assert (await _steps(auth_client, claude, ctask)).json() == {"applies": False}


@pytest.mark.asyncio
async def test_steps_require_a_user(client: AsyncClient):
    resp = await client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/tasks/{uuid.uuid4()}/steps")
    assert resp.status_code in (401, 403)
    resp = await client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/tasks")
    assert resp.status_code in (401, 403)


# ── detail ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_detail_full_row_and_live(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="in_progress", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", _basic_attempt(tid, now - timedelta(minutes=30)))
    _write(path.parent / "previews" / f"2026-10-20T08-00-01_{SID_1}_abc123.jsonl", [
        _preview(_iso(now), "x" * 3000 + "THE END", started=_iso(now - timedelta(seconds=30)), chars=3007),
    ])
    rows = (await _steps(auth_client, agent, task)).json()["rows"]
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["k"], r)

    async def detail(row_id: str):
        return await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps/{row_id}")

    think = (await detail(by_kind["think"]["id"])).json()
    assert think["k"] == "think" and think["text"].startswith("Let me read the spec first. Then")
    assert think["durationMs"] == 2000
    tool = (await detail(rows[5]["id"])).json()
    assert tool["k"] == "tool" and tool["result"].startswith("xxx") and tool["detail"]["command"] == "cat spec.md"
    delivery = (await detail(by_kind["delivery"]["id"])).json()
    assert "# New Task: Port the checklist" in delivery["text"] and delivery["truncated"] is False
    text = (await detail(by_kind["text"]["id"])).json()
    assert text["text"] == "All done: the checklist is ported."
    live = (await detail("live")).json()
    assert live["liveRow"]["block"] == "thinking" and live["liveRow"]["text"].endswith("THE END")
    assert len(live["liveRow"]["text"]) <= 2000

    for bad in ("1:2:3", "abc", "1:x", "123:1"):
        resp = await detail(bad)
        assert resp.status_code == 422, bad
    assert (await detail("1:999")).status_code == 404
    assert (await detail("7:1")).status_code == 404
    other = await make_task(board.id, status="in_progress")
    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks/{other.id}/steps/1:1")
    assert resp.status_code == 404 and resp.json() == {"reason": "no_task"}
    tagged = await detail(by_kind["think"]["id"])
    again = await auth_client.get(
        f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps/{by_kind['think']['id']}",
        headers={"If-None-Match": tagged.headers["etag"]})
    assert again.status_code == 304


@pytest.mark.asyncio
async def test_step_detail_live_does_not_reparse_the_attempt_while_only_the_preview_changes(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """Review SHOULD on #935: while a thought is open the preview changes on
    every 2 s poll. The live detail takes the transcript's newest time from
    the parse cache (keyed by the file's identity, size, mtime) — the
    attempt file (tens of MB in real tasks) is parsed once, not per poll."""
    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    task = await make_task(board.id, status="in_progress", assigned_agent_id=agent.id,
                           started_at=_utc(now - timedelta(minutes=30)))
    tid = str(task.id)
    path = _attempt_file(root, SID_1, "2026-10-20T08-00-00", _basic_attempt(tid, now - timedelta(minutes=30)))
    preview = _write(path.parent / "previews" / f"2026-10-20T08-00-01_{SID_1}_abc123.jsonl", [
        _preview(_iso(now), "thinking one", started=_iso(now - timedelta(seconds=30)))])
    parses: list[str] = []
    real_read = chat_task_steps._read_events

    def counting(opened, adapter):
        parses.append(opened.path.name)
        return real_read(opened, adapter)

    monkeypatch.setattr(chat_task_steps, "_read_events", counting)
    url = f"/api/v1/agents/{agent.id}/chat/tasks/{task.id}/steps/live"
    first = (await auth_client.get(url)).json()
    assert first["liveRow"]["text"] == "thinking one"
    for k in range(3):
        _append(preview, [_preview(_iso(now + timedelta(seconds=2 * (k + 1))), f"thinking {k + 2}",
                                   started=_iso(now - timedelta(seconds=30)))])
        assert (await auth_client.get(url)).json()["liveRow"]["text"] == f"thinking {k + 2}"
    assert parses == [path.name]
    # The transcript grows: parsed again, once.
    _append(path, [Lines(now + timedelta(seconds=1)).text("more")])
    await auth_client.get(url)
    assert parses == [path.name, path.name]


# ── stubs ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_stubs_live_cards_handover_and_outcomes(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    await _stubs_scenario(auth_client, make_agent, make_board, make_task, tmp_path, monkeypatch)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_stubs_on_postgres(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """The stubs query (outer join on a grouped subquery, coalesce over
    timestamptz columns, IN-subquery) on the real database engine."""
    await _stubs_scenario(auth_client, make_agent, make_board, make_task, tmp_path, monkeypatch)


async def _stubs_scenario(auth_client, make_agent, make_board, make_task, tmp_path, monkeypatch):
    from app.models.deliverable import TaskDeliverable
    from app.models.task import TaskComment
    from app.models.thread import Message, Thread

    board, agent, root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    other_agent = await make_agent(name="agent-c", slug="agent-c", harness="omp", agent_runtime="cli-bridge")
    now = datetime.now(timezone.utc)

    # A live task started long ago (older than 20 finished ones).
    old_live = await make_task(board.id, title="Old but running", status="in_progress",
                               assigned_agent_id=agent.id, started_at=_utc(now - timedelta(days=30)))
    # 21 finished tasks, newest first by start.
    done = []
    for i in range(21):
        done.append(await make_task(board.id, title=f"Done {i}", status="done", assigned_agent_id=agent.id,
                                    started_at=_utc(now - timedelta(hours=i + 1)),
                                    completed_at=_utc(now - timedelta(hours=i + 1) + timedelta(minutes=30))))
    await _add(
        TaskComment(task_id=done[0].id, author_type="agent", comment_type="resolution",
                    content="**Ported** the list.\n\nAll `12` tests green.",
                    created_at=_utc(now - timedelta(minutes=40))),
        TaskComment(task_id=done[0].id, author_type="agent", comment_type="resolution",
                    content="Older resolution", created_at=_utc(now - timedelta(minutes=50))),
        TaskDeliverable(task_id=done[0].id, deliverable_type="file", title="a"),
        TaskDeliverable(task_id=done[0].id, deliverable_type="file", title="b"),
    )
    # Delivered, not acked yet: live.
    delivered = await make_task(board.id, title="Just delivered", status="inbox", assigned_agent_id=agent.id,
                                dispatched_at=_utc(now - timedelta(seconds=20)))
    # Queued (inbox, not dispatched) and held ones.
    await make_task(board.id, title="Queued", status="inbox", assigned_agent_id=agent.id)
    await make_task(board.id, title="Held", status="inbox", assigned_agent_id=agent.id, run_control="manual_hold")
    # Worked here, now in progress at another agent: a card, never live.
    handed = await make_task(board.id, title="Handed over", status="in_progress",
                             assigned_agent_id=other_agent.id, started_at=_utc(now - timedelta(minutes=5)))
    await _add(_event(handed.id, "in_progress", agent_id=agent.id, at=now - timedelta(minutes=90)),
               _event(handed.id, "review", agent_id=agent.id, from_status="in_progress",
                      at=now - timedelta(minutes=80)))
    # Parked: started_at / dispatched_at reset, the in_progress event keeps its place.
    parked = await make_task(board.id, title="Parked", status="inbox", assigned_agent_id=agent.id)
    await _add(_event(parked.id, "in_progress", agent_id=agent.id, at=now - timedelta(minutes=3)),
               _event(parked.id, "inbox", agent_id=agent.id, from_status="in_progress",
                      at=now - timedelta(minutes=2)))
    # Waiting on an open question; failed with a blocker.
    thread = Thread(kind="task")
    await _add(thread)
    waiting = await make_task(board.id, title="Asks", status="waiting", assigned_agent_id=agent.id,
                              started_at=_utc(now - timedelta(minutes=10)), thread_id=thread.id)
    await _add(Message(thread_id=thread.id, seq=1, sender_type="agent", message_type="question",
                       body="Which branch should I use?", question_meta={"awaiting": True, "blocking": True}))
    failed = await make_task(board.id, title="Broke", status="failed", assigned_agent_id=agent.id,
                             started_at=_utc(now - timedelta(minutes=7)))
    await _add(TaskComment(task_id=failed.id, author_type="agent", comment_type="blocker",
                           content="The model is unreachable."),
               _event(failed.id, "failed", agent_id=agent.id, from_status="in_progress",
                      at=now - timedelta(minutes=6)))
    # Someone else's task: never listed.
    await make_task(board.id, title="Foreign", status="done", assigned_agent_id=other_agent.id,
                    started_at=_utc(now - timedelta(minutes=1)))

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Queued: "Queued" and the parked one (back in the inbox, not delivered);
    # never the held one or the delivered one (that one is live).
    assert body["applies"] is True and body["queued"] == 2 and body["hasMore"] is True
    tasks = {t["title"]: t for t in body["tasks"]}
    assert "Foreign" not in tasks and "Queued" not in tasks and "Held" not in tasks
    live = {t["title"] for t in body["tasks"] if t["live"]}
    assert live == {"Old but running", "Just delivered", "Asks"}
    assert len([t for t in body["tasks"] if not t["live"]]) == 20
    assert tasks["Handed over"]["live"] is False and tasks["Handed over"]["assignee"] == "agent-c"
    assert tasks["Parked"]["startKey"] is not None and tasks["Parked"]["startedAt"] is None
    assert tasks["Done 0"]["outcome"] == "Ported the list. All 12 tests green."
    assert tasks["Done 0"]["deliverables"] == 2 and tasks["Done 0"]["endedAt"] is not None
    assert tasks["Done 1"]["outcome"] is None and tasks["Done 1"]["deliverables"] == 0
    assert tasks["Asks"]["outcome"] == "Which branch should I use?"
    assert tasks["Broke"]["outcome"] == "The model is unreachable."
    assert tasks["Broke"]["endedAt"] is not None
    assert tasks["Old but running"]["endedAt"] is None
    assert tasks["Just delivered"]["deliveredAt"] is not None
    # Done 20 is the 21st finished one -> cut by the limit of 20 non-live tasks.
    assert "Done 20" not in tasks
    assert resp.headers["cache-control"] == "no-cache, private"
    again = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks",
                                  headers={"If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304
    small = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks", params={"limit": 2})).json()
    assert len([t for t in small["tasks"] if not t["live"]]) == 2
    assert (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks", params={"limit": 0})).status_code == 422


@pytest.mark.asyncio
async def test_task_stubs_a_live_task_beyond_the_cap_is_never_a_card(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """More live tasks than the defensive cap: the extra one is left out,
    never shown as a finished-looking card with ``live: false``."""
    board, agent, _root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    cap = chat_task_steps._LIVE_CAP
    for k in range(cap + 2):
        await make_task(board.id, title=f"Running {k}", status="in_progress", assigned_agent_id=agent.id,
                        started_at=_utc(now - timedelta(minutes=k + 1)))
    await make_task(board.id, title="Finished", status="done", assigned_agent_id=agent.id,
                    started_at=_utc(now - timedelta(hours=2)))
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks")).json()
    assert len([t for t in body["tasks"] if t["live"]]) == cap
    assert [t["title"] for t in body["tasks"] if not t["live"]] == ["Finished"]


@pytest.mark.asyncio
async def test_task_stubs_not_applicable_for_a_chat_harness(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    board, _agent, _root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    claude = await make_agent(name="agent-b", slug="agent-b", harness="claude", agent_runtime="cli-bridge")
    await make_task(board.id, status="in_progress", assigned_agent_id=claude.id)
    body = (await auth_client.get(f"/api/v1/agents/{claude.id}/chat/tasks")).json()
    assert body == {"applies": False}
    assert (await auth_client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/tasks")).status_code == 404


@pytest.mark.asyncio
async def test_task_stubs_not_applicable_under_the_native_driver(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "omp_driver_default", "native", raising=False)
    board, agent, _root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    assert (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks")).json() == {"applies": False}


def test_route_order_static_before_parameterised():
    from app.routers.agent_chat import router

    paths = [r.path for r in router.routes]
    tasks = paths.index("/api/v1/agents/{agent_id}/chat/tasks")
    steps = paths.index("/api/v1/agents/{agent_id}/chat/tasks/{task_id}/steps")
    detail = paths.index("/api/v1/agents/{agent_id}/chat/tasks/{task_id}/steps/{row_id}")
    assert tasks < steps < detail


@pytest.mark.asyncio
async def test_task_stubs_name_the_card_a_review_run_works(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """Round-4 review r1 S2: a review handoff gives the `review` card to the
    reviewer, and its review run works it — the chat probe says
    `task_working`. The card is not ``live`` (it is no section with a run of
    its own state), so the status line cut that state to "ready". The stub
    says so instead: ``reviewRun`` — and only for the reviewer's own card."""
    board, agent, _root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    reviewing = await make_task(board.id, title="Reviewing", status="review", assigned_agent_id=agent.id,
                                dispatch_intent="review_handoff", started_at=_utc(now - timedelta(minutes=9)),
                                dispatched_at=_utc(now - timedelta(minutes=1)))
    await make_task(board.id, title="Own done", status="done", assigned_agent_id=agent.id,
                    started_at=_utc(now - timedelta(hours=1)))
    await make_task(board.id, title="Own review", status="review", assigned_agent_id=agent.id,
                    started_at=_utc(now - timedelta(hours=2)))
    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks")).json()
    tasks = {t["title"]: t for t in body["tasks"]}
    assert tasks["Reviewing"]["reviewRun"] is True
    assert tasks["Reviewing"]["live"] is False
    assert tasks["Own done"]["reviewRun"] is False
    assert tasks["Own review"]["reviewRun"] is False
    assert str(reviewing.id) == tasks["Reviewing"]["taskId"]


@pytest.mark.asyncio
async def test_task_stubs_name_the_current_run_after_a_rework(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """Round-7 re-check t1 NICE-3: after "Request changes" the end line said
    "took 9 min" — from the first start to the end of the second run, the
    wait for the review included. The stub names the current run
    (``runStartedAt`` = the newest step into ``in_progress``, ``runs`` = how
    many there were); ``startKey`` keeps the first start, it places the task
    in the chat."""
    board, agent, _root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    first = now - timedelta(minutes=10)
    second = now - timedelta(minutes=2)
    task = await make_task(board.id, title="Reworked", status="review", assigned_agent_id=agent.id,
                           started_at=_utc(first))
    await _add(
        _event(task.id, "in_progress", agent_id=agent.id, at=first),
        _event(task.id, "review", agent_id=agent.id, from_status="in_progress", at=first + timedelta(seconds=88)),
        _event(task.id, "inbox", from_status="review", at=second - timedelta(seconds=2)),
        _event(task.id, "in_progress", agent_id=agent.id, at=second),
        _event(task.id, "review", agent_id=agent.id, from_status="in_progress", at=second + timedelta(seconds=48)),
    )
    single = await make_task(board.id, title="One run", status="done", assigned_agent_id=agent.id,
                             started_at=_utc(now - timedelta(hours=1)),
                             completed_at=_utc(now - timedelta(minutes=50)))
    await _add(_event(single.id, "in_progress", agent_id=agent.id, at=now - timedelta(hours=1)))

    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks")).json()
    tasks = {t["title"]: t for t in body["tasks"]}
    reworked = tasks["Reworked"]
    assert reworked["runs"] == 2
    assert datetime.fromisoformat(reworked["runStartedAt"]) == second
    assert datetime.fromisoformat(reworked["startKey"]) == first
    assert datetime.fromisoformat(reworked["endedAt"]) == second + timedelta(seconds=48)
    assert tasks["One run"]["runs"] == 1


@pytest.mark.asyncio
async def test_task_stubs_count_only_real_run_starts(
    auth_client: AsyncClient, make_agent, make_board, make_task, tmp_path, monkeypatch, acp_driver
):
    """Round-8 review MUST-2: every step into ``in_progress`` counted as a
    run. 20 min of work, a question, 1 min more → "Dauer 1 min (Lauf 2)".
    Only ``inbox → in_progress`` (or a phase rewrite) starts a run; an answered
    question, a Telegram resume and the provisional ``review → in_progress``
    of a drag rejection (which then goes via inbox) do not."""
    board, agent, _root = await _agent_and_board(make_agent, make_board, tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    # Work, a question, the answer, more work, review: ONE run.
    start = now - timedelta(minutes=30)
    asked = start + timedelta(minutes=20)
    answered = asked + timedelta(minutes=8)
    asked_task = await make_task(board.id, title="Asked", status="review", assigned_agent_id=agent.id,
                                 started_at=_utc(start))
    await _add(
        _event(asked_task.id, "in_progress", agent_id=agent.id, at=start),
        _event(asked_task.id, "blocked", from_status="in_progress", at=asked, reason="clarification_question"),
        _event(asked_task.id, "in_progress", from_status="blocked", at=answered, reason="clarification_answered"),
        _event(asked_task.id, "review", agent_id=agent.id, from_status="in_progress",
               at=answered + timedelta(minutes=1)),
    )

    # A drag rejection: review → in_progress (provisional) → inbox → in_progress = run 2, not 3.
    first = now - timedelta(minutes=12)
    reject = now - timedelta(minutes=4)
    dragged = await make_task(board.id, title="Dragged", status="in_progress", assigned_agent_id=agent.id,
                              started_at=_utc(first))
    await _add(
        _event(dragged.id, "in_progress", agent_id=agent.id, at=first),
        _event(dragged.id, "review", agent_id=agent.id, from_status="in_progress", at=first + timedelta(minutes=3)),
        _event(dragged.id, "in_progress", from_status="review", at=reject),
        _event(dragged.id, "inbox", from_status="in_progress", at=reject + timedelta(seconds=1)),
        _event(dragged.id, "in_progress", agent_id=agent.id, at=reject + timedelta(seconds=5)),
    )

    # A done phase sent back for a rewrite starts a run without an inbox step.
    p_first = now - timedelta(minutes=40)
    rewrite = now - timedelta(minutes=6)
    rewritten = await make_task(board.id, title="Rewritten", status="in_progress", assigned_agent_id=agent.id,
                                started_at=_utc(p_first))
    await _add(
        _event(rewritten.id, "in_progress", agent_id=agent.id, at=p_first),
        _event(rewritten.id, "done", agent_id=agent.id, from_status="in_progress", at=p_first + timedelta(minutes=5)),
        _event(rewritten.id, "in_progress", agent_id=agent.id, from_status="done", at=rewrite,
               reason="phase_rewrite_request"),
    )

    body = (await auth_client.get(f"/api/v1/agents/{agent.id}/chat/tasks")).json()
    tasks = {t["title"]: t for t in body["tasks"]}
    assert tasks["Asked"]["runs"] == 1
    assert datetime.fromisoformat(tasks["Asked"]["runStartedAt"]) == start
    assert tasks["Dragged"]["runs"] == 2
    assert datetime.fromisoformat(tasks["Dragged"]["runStartedAt"]) == reject + timedelta(seconds=5)
    assert tasks["Rewritten"]["runs"] == 2
    assert datetime.fromisoformat(tasks["Rewritten"]["runStartedAt"]) == rewrite
