"""Task A4 — live transcript tailer + `/agents/{id}/chat/history|stream` router.

Two layers:

1. Router tests (history endpoint only — the SSE endpoint's response never
   terminates on its own, so it isn't exercised end-to-end here, matching
   how every other `*/stream` endpoint in this suite is (not) tested):
   - 200 with a fixture transcript (``resolve_transcript_dir`` monkeypatched
     to a tmp dir with a real ``.jsonl``)
   - 404 ``{"reason": "no_transcript"}`` for a host agent with no transcript
     dir at all (Hermes — not in the Boss allowlist, so
     ``resolve_transcript_dir`` returns None with zero monkeypatching)
   - 401 without a token

2. ``ChatTailerManager`` unit tests: refcounted acquire/release, new-bytes
   publishing (with a partial trailing line held back), session rollover to
   a newer ``.jsonl``, and a disappeared file not crashing the poll loop.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════════════════
# Router: /agents/{id}/chat/history
# ══════════════════════════════════════════════════════════════════════════


def _user_line(text: str, msg_uuid: str = "u1", ts: str = "2026-08-13T00:00:00Z") -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": msg_uuid,
            "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


async def test_history_200_for_agent_with_fixture_transcript(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    tdir = tmp_path / "rex-transcripts"
    tdir.mkdir()
    (tdir / "sess1.jsonl").write_text(_user_line("hello from fixture") + "\n")

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "resolve_transcript_dir", lambda a: tdir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session"]["sessionId"] == "sess1"
    assert len(body["events"]) == 1
    assert body["events"][0]["kind"] == "message"
    assert body["events"][0]["text"] == "hello from fixture"


async def test_history_404_no_transcript_for_host_agent_without_dir(auth_client: AsyncClient, make_agent):
    # "hermes" is a host-runtime agent but not in the Boss allowlist —
    # resolve_transcript_dir() returns None for it with no monkeypatching.
    agent = await make_agent(name="Hermes", agent_runtime="host", slug="hermes")

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


async def test_history_404_no_transcript_when_dir_has_no_sessions(auth_client: AsyncClient, make_agent, tmp_path, monkeypatch):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    import app.routers.agent_chat as agent_chat_mod

    monkeypatch.setattr(agent_chat_mod, "resolve_transcript_dir", lambda a: empty_dir)

    resp = await auth_client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 404
    assert resp.json() == {"reason": "no_transcript"}


async def test_history_requires_auth(client: AsyncClient, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge")

    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/history")

    assert resp.status_code == 401


async def test_history_404_for_unknown_agent(auth_client: AsyncClient):
    resp = await auth_client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/history")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# ChatTailerManager
# ══════════════════════════════════════════════════════════════════════════


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return predicate()


@pytest.fixture
def fake_broadcast(monkeypatch):
    """Patches ``transcript_chat.sse.broadcast`` and returns the list it
    appends (channel, event_type, data) tuples to."""
    import app.services.transcript_chat as transcript_chat

    published: list[tuple[str, str, dict]] = []

    async def _fake(channel: str, event_type: str, data: dict) -> None:
        published.append((channel, event_type, data))

    monkeypatch.setattr(transcript_chat.sse, "broadcast", _fake)
    return published


@pytest.fixture
def manager():
    from app.services.transcript_chat import ChatTailerManager

    m = ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    return m


async def test_tailer_publishes_appended_line(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        session_file.write_text(_user_line("hello", msg_uuid="u1") + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    channel, event_type, data = fake_broadcast[0]
    assert channel == "mc:agent:agent-1:chat"
    assert event_type == "chat_event"
    assert data["kind"] == "message"
    assert data["text"] == "hello"


async def test_tailer_holds_back_partial_trailing_line(manager, fake_broadcast, tmp_path):
    """A write that lands mid-line must not be parsed until the newline
    arrives — the tailer buffers the remainder across polls."""
    session_file = tmp_path / "sess1.jsonl"
    full_line = _user_line("complete once flushed", msg_uuid="u1")
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    try:
        # Write only the first half of the line (no trailing newline).
        session_file.write_text(full_line[: len(full_line) // 2])
        await asyncio.sleep(0.1)
        assert fake_broadcast == []  # nothing published yet — line incomplete

        # Now complete it.
        session_file.write_text(full_line + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    assert fake_broadcast[0][2]["text"] == "complete once flushed"


async def test_tailer_session_changed_on_newer_jsonl(manager, fake_broadcast, tmp_path):
    old_session = tmp_path / "sess-old.jsonl"
    old_session.write_text(_user_line("in the old session", msg_uuid="u-old") + "\n")

    await manager.acquire("agent-1", old_session)
    try:
        # A newer file lands in the same directory — simulate a fresh
        # Claude Code session starting.
        await asyncio.sleep(0.05)
        new_session = tmp_path / "sess-new.jsonl"
        new_session.write_text("")
        # Ensure a strictly newer mtime than old_session.
        import os
        os.utime(new_session, None)

        assert await _wait_until(
            lambda: any(d.get("kind") == "session_changed" for _, _, d in fake_broadcast)
        )

        # Events appended to the new file are now published too.
        fake_broadcast.clear()
        new_session.write_text(_user_line("in the new session", msg_uuid="u-new") + "\n")
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
    finally:
        await manager.release("agent-1")

    assert fake_broadcast[-1][2]["text"] == "in the new session"


async def test_tailer_survives_disappearing_file(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text(_user_line("before delete", msg_uuid="u1") + "\n")

    await manager.acquire("agent-1", session_file)
    try:
        assert await _wait_until(lambda: len(fake_broadcast) > 0)
        session_file.unlink()
        # Give the loop a few ticks to poll a missing file — must not raise
        # or kill the task.
        await asyncio.sleep(0.15)
        assert not manager._tasks["agent-1"].done()
    finally:
        await manager.release("agent-1")


async def test_tailer_refcount_shares_one_task(manager, fake_broadcast, tmp_path):
    session_file = tmp_path / "sess1.jsonl"
    session_file.write_text("")

    await manager.acquire("agent-1", session_file)
    task_after_first = manager._tasks["agent-1"]
    await manager.acquire("agent-1", session_file)
    assert manager._tasks["agent-1"] is task_after_first  # no second task spawned

    await manager.release("agent-1")
    assert manager._tasks["agent-1"] is task_after_first  # still running — one ref left

    await manager.release("agent-1")
    assert "agent-1" not in manager._tasks  # last release cancels + drops it
