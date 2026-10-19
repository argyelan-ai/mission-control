"""The agent chat shows the CHAT DAEMON's session, never a task session.

Operator finding 19.10.2026 ("the chat is buggy, as if it switches between two
sessions"): an ACP agent runs its chat session and a task session at the same
time. Both write transcript files in the same sessions tree within seconds of
each other, and ``find_active_session`` picked "the newest file" — so the
chat history, the live stream and the aliveness badge jumped between the two.

The chat daemon names its session in ``acp-chat-state.json`` (``transcript``,
docs/specs/chat-over-acp.md). That pointer decides. "Newest file" stays only
as the fallback when there is no pointer, and the fallback never returns a
task session (a ``taskId`` in a header of the session, or a worktree/task cwd).

Folder layout and header shapes copied from a live sessions tree (ids and
names made up).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from app.services import omp_chat

CHAT = "01a15411-0000-7000-8000-000000000001"
CHAT2 = "01a15600-0000-7000-8000-000000000003"
TASK = "01a1559a-0000-7000-8000-000000000002"
TASK_ID = "48468c92-0000-4000-8000-000000000009"

CONTAINER_SESSIONS = "/home/agent/.omp/profiles/mc-agent/agent/sessions"


def _omp():
    from app.services.transcript_adapters import adapter_for

    return adapter_for(SimpleNamespace(harness="omp"))


def _msg(i: int, ts: str) -> dict:
    return {"type": "message", "id": f"m{i:07d}", "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": f"line {i}"}]}}


def _native(folder: Path, stamp: str, sid: str, cwd: str, *entries_ts: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}Z_{sid}.jsonl"
    lines = [
        {"type": "title", "v": 1, "title": "", "updatedAt": "2026-10-19T12:10:19.485Z"},
        {"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-19T12:10:19.485Z",
         "cwd": cwd},
    ]
    lines += [_msg(i, ts) for i, ts in enumerate(entries_ts)]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return path


def _sink(folder: Path, stamp: str, sid: str, *entries_ts: str, task_id: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}_{sid}.jsonl"
    header = {"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-19T12:10:19.500Z",
              "cwd": "", "bridge": "acp"}
    if task_id:
        header["taskId"] = task_id
    lines = [header] + [_msg(100 + i, ts) for i, ts in enumerate(entries_ts)]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return path


def _pointer(folder: Path, transcript: Path, session_id: str) -> Path:
    """The daemon's state mirror, with the CONTAINER path it writes."""
    state = folder / "acp-chat-state.json"
    state.write_text(json.dumps({
        "version": 1, "driver": "omp", "sessionId": session_id, "busy": False,
        "transcript": f"{CONTAINER_SESSIONS}/{folder.name}/{transcript.name}",
    }))
    return state


def _tree(tmp_path: Path):
    """Chat in /workspace, a task in a worktree — the live 19.10. layout.
    The TASK files are the newest by content and by mtime."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    wt = root / "--workspace-worktrees-bench-x--"
    chat_native = _native(ws, "2026-10-19T12-10-19-485", CHAT, "/workspace",
                          "2026-10-19T20:00:00.000Z")
    chat_sink = _sink(ws, "2026-10-19T12-10-19", CHAT, "2026-10-19T20:00:01.000Z")
    task_native = _native(wt, "2026-10-19T19-19-10-646", TASK, "/workspace/worktrees/bench-x",
                          "2026-10-19T22:11:00.000Z")
    task_sink = _sink(wt, "2026-10-19T19-19-11", TASK, "2026-10-19T22:11:01.000Z",
                      task_id=TASK_ID)
    old = 1_700_000_000
    for p in (chat_native, chat_sink):
        os.utime(p, (old, old))
    for p in (task_native, task_sink):
        os.utime(p, (old + 100, old + 100))
    return root, ws, chat_sink, task_sink


# ── the pointer decides ─────────────────────────────────────────────────────


def test_chat_stays_on_the_daemon_session_while_a_task_writes_newer(tmp_path):
    root, ws, chat_sink, _ = _tree(tmp_path)
    _pointer(ws, chat_sink, CHAT)
    active = omp_chat.find_active_session(root)
    assert active is not None
    assert active[0] == chat_sink
    assert active[1]["sessionId"] == chat_sink.stem


def test_pointer_wins_over_a_newer_session_that_carries_no_task_marker(tmp_path):
    """The pointer is the authority, not the task markers: a parallel session
    without ``taskId`` (an older bridge image, a head run in /workspace) that
    is written newer must not take over the chat either."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    chat = _sink(ws, "2026-10-19T12-10-19", CHAT, "2026-10-19T20:00:00.000Z")
    other = _native(ws, "2026-10-19T21-00-00-000", TASK, "/workspace", "2026-10-19T22:00:00.000Z")
    os.utime(chat, (1_700_000_000, 1_700_000_000))
    os.utime(other, (1_700_000_500, 1_700_000_500))
    _pointer(ws, chat, CHAT)
    assert omp_chat.find_active_session(root)[0] == chat


def test_pointer_to_omps_own_file_still_lands_on_the_bridge_file(tmp_path):
    root, ws, chat_sink, _ = _tree(tmp_path)
    native = next(ws.glob(f"*Z_{CHAT}.jsonl"))
    _pointer(ws, native, CHAT)
    assert omp_chat.find_active_session(root)[0] == chat_sink


def test_task_in_the_same_folder_as_the_chat_does_not_take_over(tmp_path):
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    chat = _sink(ws, "2026-10-19T12-10-19", CHAT, "2026-10-19T20:00:00.000Z")
    task = _sink(ws, "2026-10-19T18-15-28", TASK, "2026-10-19T22:00:00.000Z", task_id=TASK_ID)
    os.utime(chat, (1_700_000_000, 1_700_000_000))
    os.utime(task, (1_700_000_500, 1_700_000_500))
    _pointer(ws, chat, CHAT)
    assert omp_chat.find_active_session(root)[0] == chat


def test_new_session_moves_the_pointer_and_the_chat_follows(tmp_path):
    root, ws, chat_sink, _ = _tree(tmp_path)
    _pointer(ws, chat_sink, CHAT)
    assert omp_chat.find_active_session(root)[0] == chat_sink
    fresh = _sink(ws, "2026-10-19T22-30-00", CHAT2, "2026-10-19T22:30:00.000Z")
    _pointer(ws, fresh, CHAT2)
    assert omp_chat.find_active_session(root)[0] == fresh


def test_a_pointer_cannot_leave_the_sessions_tree(tmp_path):
    root, ws, chat_sink, task_sink = _tree(tmp_path)
    secret = tmp_path / "secret.jsonl"
    secret.write_text("{}\n")
    (ws / "acp-chat-state.json").write_text(json.dumps({
        "sessionId": "x", "transcript": "/../../secret.jsonl"}))
    found = omp_chat.find_active_session(root)
    assert found is not None and found[0] != secret
    # And it fell back to the chat, not to the newer task.
    assert found[0] == chat_sink


# ── fallback: no pointer ────────────────────────────────────────────────────


def test_without_a_pointer_the_fallback_never_returns_a_task_session(tmp_path):
    root, _, chat_sink, _ = _tree(tmp_path)
    assert omp_chat.find_active_session(root)[0] == chat_sink


def test_pointer_to_a_missing_file_falls_back_without_a_task(tmp_path):
    root, ws, chat_sink, _ = _tree(tmp_path)
    (ws / "acp-chat-state.json").write_text(json.dumps({
        "sessionId": "gone", "transcript": f"{CONTAINER_SESSIONS}/--workspace--/gone_x.jsonl"}))
    assert omp_chat.find_active_session(root)[0] == chat_sink


def test_a_tree_with_only_task_sessions_has_no_chat_session(tmp_path):
    root = tmp_path / "omp-sessions"
    _sink(root / "--workspace--", "2026-10-19T18-15-28", TASK, "2026-10-19T22:00:00.000Z",
          task_id=TASK_ID)
    _native(root / "--workspace-worktrees-y--", "2026-10-19T18-15-28-100", CHAT2,
            "/workspace/worktrees/y", "2026-10-19T22:00:00.000Z")
    assert omp_chat.find_active_session(root) is None


def test_omps_native_file_of_a_task_counts_as_task_via_its_sibling(tmp_path):
    """omp's own file of a task session carries no taskId — only the bridge
    sink does. The task must still be recognised through the sibling."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    chat = _native(ws, "2026-10-19T08-00-00-000", CHAT, "/workspace", "2026-10-19T08:00:00.000Z")
    task_native = _native(ws, "2026-10-19T18-15-28-176", TASK, "/workspace",
                          "2026-10-19T22:00:00.000Z")
    _sink(ws, "2026-10-19T18-15-28", TASK, task_id=TASK_ID)
    os.utime(chat, (1_700_000_000, 1_700_000_000))
    os.utime(task_native, (1_700_000_900, 1_700_000_900))
    assert omp_chat.find_active_session(root)[0] == chat


# ── live stream: the tailer follows the pointer ─────────────────────────────


async def test_tailer_stays_put_while_a_task_writes_and_switches_on_new(tmp_path, monkeypatch):
    import app.services.transcript_chat as tc
    from app.services import transcript_adapters

    root, ws, chat_sink, task_sink = _tree(tmp_path)
    _pointer(ws, chat_sink, CHAT)

    omp = _omp()
    monkeypatch.setattr(transcript_adapters, "adapter_for", lambda agent: omp)
    published: list[dict] = []

    async def _fake(channel, event_type, data):
        published.append(json.loads(json.dumps(data)))

    monkeypatch.setattr(tc.sse, "broadcast", _fake)
    m = tc.ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    m.STATE_PROBE_INTERVAL_SECONDS = 0.02
    await m.acquire("a", chat_sink)
    try:
        # The task keeps writing — newer content, newer mtime — for a while.
        for i in range(15):
            with task_sink.open("a") as fh:
                fh.write(json.dumps(_msg(9000 + i, f"2026-10-19T23:00:{i:02d}.000Z")) + "\n")
            await asyncio.sleep(0.03)
        assert not any(d.get("kind") == "session_changed" for d in published), published
        assert not any("line 90" in json.dumps(d) for d in published), "task lines leaked"

        # /new in the chat daemon: new transcript + pointer update.
        fresh = _sink(ws, "2026-10-19T23-30-00", CHAT2, "2026-10-19T23:30:00.000Z")
        _pointer(ws, fresh, CHAT2)
        for _ in range(100):
            if any(d.get("kind") == "session_changed" for d in published):
                break
            await asyncio.sleep(0.02)
    finally:
        await m.release("a")
    assert any(d.get("kind") == "session_changed" for d in published)
