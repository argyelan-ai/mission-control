"""A further chat is recorded LIVE, not only by the 10-minute sweep.

Live finding 23.11.2026: a second chat with an omp agent got its answer at
13:02:18, but its ``chat_events`` stayed empty until the import sweep at
13:10:16 — the composer showed "unconfirmed" for minutes. The chat tailer's
live sink follows only the agent's DEFAULT session, and a further chat's
view (``useRecordChat``) never opens the agent's stream at all.

Contract:

* a message sent into a further chat (``POST /chat/input`` with ``chatId``)
  keeps that chat's own session file live in the record: what the agent
  writes there is in ``chat_events`` within seconds (measured here with the
  production poll interval);
* the same for a further Claude chat, whose file lives under
  ``claude-config/chats/<chat id>/`` — and the sweep imports that file too
  (before, nothing ever read it);
* flag off: nothing is followed.

Fixture lines are synthetic copies of the omp / Claude Code JSONL schema.
Slugs are made up.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import ChatEvent
from app.services import chat_record, chat_record_follow
from tests.conftest import test_engine
from tests.test_acp_chat_many_chats import wire  # noqa: F401 — fixture used by name
from tests.test_chat_record import (  # noqa: F401 — fixtures used by name
    _assistant_text,
    _import_all,
    _test_factory,
    _user,
    _write,
    _append,
    flag_on,
    host_home,
)

pytestmark = pytest.mark.asyncio

#: Upper bound for "within seconds": two production polls plus slack.
LIVE_WITHIN_S = 2 * chat_record_follow.POLL_SECONDS + 1.0


@pytest.fixture
def follower(monkeypatch):
    """The process-wide follower on the test database, stopped afterwards."""
    f = chat_record_follow.ChatFollower(session_factory=_test_factory)
    monkeypatch.setattr(chat_record_follow, "FOLLOWER", f)
    return f  # tests that start a follow stop it themselves


@pytest.fixture
def claude_home(host_home, monkeypatch):  # noqa: F811
    from app.services import claude_chat_runner

    monkeypatch.setattr(claude_chat_runner, "_host_home", lambda: host_home)
    return host_home


async def _texts(chat_id) -> list[str]:
    async with AsyncSession(test_engine) as s:
        rows = (await s.exec(
            select(ChatEvent).where(ChatEvent.chat_id == chat_id, ChatEvent.kind == "message")
            .order_by(ChatEvent.ts, ChatEvent.seq)
        )).all()
    return [r.payload.get("text") for r in rows]


async def _wait_written(f, at_least: int, timeout: float) -> float:
    """Seconds until the follower wrote ``at_least`` rows. Polls the
    follower, never the DB: the SQLite lane shares one connection."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if f.written >= at_least:
            return time.monotonic() - t0
        await asyncio.sleep(0.05)
    return float("inf")


def _omp_line(mid, role, text, ts):
    return {"type": "message", "id": mid, "parentId": None, "timestamp": ts,
            "message": {"role": role, "content": [{"type": "text", "text": text}]}}


async def test_further_omp_chat_answer_is_in_the_record_within_seconds(
    auth_client, make_agent, wire, host_home, flag_on, follower,  # noqa: F811
):
    agent = await make_agent(name="Omp Live", slug="omp-live", agent_runtime="cli-bridge", harness="omp")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await s.commit()
    # The operator sends; the daemon opens the chat's session and pins it.
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": "hello second chat", "chatId": str(chat.id)})
    assert resp.status_code == 204, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(type(chat), chat.id)
        await chat_record.pin_native_session(s, row, "s-live")
        await s.commit()
    copy = (host_home / ".mc" / "agents" / "omp-live" / "omp-sessions" / "--workspace--"
            / ".mc" / "2026-11-23T13-01-40-000Z_s-live.jsonl")
    head = {"type": "session", "version": 3, "id": "s-live", "timestamp": "2026-11-23T13:01:40.000Z",
            "cwd": "/workspace", "bridge": "acp"}
    _write(copy, [head, _omp_line("m1", "user", "hello second chat", "2026-11-23T13:01:41.000Z")])
    try:
        assert await _wait_written(follower, 1, 10) < 10, "the operator's message never reached the record"
        # The agent answers: measured from the line on disk to the row.
        _append(copy, [_omp_line("m2", "assistant", "Hi from chat two.", "2026-11-23T13:02:18.000Z")])
        took = await _wait_written(follower, 2, 10)
    finally:
        await follower.stop()
    print(f"\nfurther omp chat: answer -> chat_events row in {took:.2f} s")
    assert took < LIVE_WITHIN_S, f"answer reached the record after {took:.2f} s"
    assert await _texts(chat.id) == ["hello second chat", "Hi from chat two."]
    # Nothing leaked into the agent's default chat.
    default = await chat_record.ensure_agent_chat(AsyncSession(test_engine), agent)
    assert await _texts(default.id) == []


async def test_further_claude_chat_is_followed_live(make_agent, claude_home, flag_on, follower):  # noqa: F811
    from app.services import acp_chat_chats, claude_chat_runner

    agent = await make_agent(name="Claude Live", slug="claude-live", agent_runtime="cli-bridge",
                             harness="claude")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await s.commit()
        assert await acp_chat_chats.resolve_chat(s, _Admin(), agent, chat.id) == str(chat.id)
    path = claude_chat_runner.transcript_path("claude-live", str(chat.id), chat.native_session_id)
    _write(path, [_user("u1", "2026-11-23T13:01:41.000Z", "hello claude chat")])
    try:
        assert await _wait_written(follower, 1, 10) < 10
        _append(path, [_assistant_text("a1", "2026-11-23T13:01:50.000Z", "Hi from Claude chat two.")])
        took = await _wait_written(follower, 2, 10)
    finally:
        await follower.stop()
    print(f"\nfurther Claude chat: answer -> chat_events row in {took:.2f} s")
    assert took < LIVE_WITHIN_S
    assert await _texts(chat.id) == ["hello claude chat", "Hi from Claude chat two."]


async def test_sweep_imports_a_further_claude_chats_own_file(make_agent, claude_home, flag_on):  # noqa: F811
    """The file lives under ``claude-config/chats/<chat id>/`` — outside the
    folder the agent-level reader globs. Before, nothing ever read it."""
    from app.services import claude_chat_runner

    agent = await make_agent(name="Claude Sweep", slug="claude-sweep", agent_runtime="cli-bridge",
                             harness="claude")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await s.commit()
    path = claude_chat_runner.transcript_path("claude-sweep", str(chat.id), chat.native_session_id)
    _write(path, [_user("u1", "2026-11-23T13:01:41.000Z", "hi"),
                  _assistant_text("a1", "2026-11-23T13:01:44.000Z", "hello")])
    await _import_all()
    assert await _texts(chat.id) == ["hi", "hello"]
    await _import_all()  # idempotent
    assert await _texts(chat.id) == ["hi", "hello"]


async def test_nothing_is_followed_while_the_flag_is_off(make_agent, monkeypatch, follower):
    monkeypatch.setattr(settings, "chat_record_enabled", False, raising=False)
    agent = await make_agent(name="Omp Off", slug="omp-off", agent_runtime="cli-bridge", harness="omp")
    follower.follow(agent, "00000000-0000-4000-8000-000000000001")
    assert not follower.following("00000000-0000-4000-8000-000000000001")


async def test_no_database_round_trip_before_the_chat_has_files(make_agent, host_home, flag_on):  # noqa: F811
    """A follow whose transcript folder does not exist never opens a DB
    session (send-route tests elsewhere must not reach a real database)."""
    opened = []

    def _factory():
        opened.append(1)
        return _test_factory()

    f = chat_record_follow.ChatFollower(session_factory=_factory)
    agent = await make_agent(name="Omp Dry", slug="omp-dry", agent_runtime="cli-bridge", harness="omp")
    f.follow(agent, "00000000-0000-4000-8000-000000000002")
    await asyncio.sleep(chat_record_follow.POLL_SECONDS * 1.5)
    assert f.following("00000000-0000-4000-8000-000000000002")
    await f.stop()
    assert opened == []


class _Admin:
    id = None
    role = "admin"


# ── critic round (23.11.2026) ──────────────────────────────────────────────


async def test_viewing_a_further_chat_rearms_its_follow(
    auth_client, make_agent, host_home, flag_on, follower,  # noqa: F811
):
    """The follow lives in memory and is started by a send. After a restart
    or deploy mid-turn, or after 15 quiet minutes (a long tool run), it is
    gone — and the chat fell back to the 10-minute sweep, the reported
    symptom. The chat's view polls its history every few seconds: that poll
    keeps the follow alive. The agent's own chat is never followed here
    (the tailer's live sink writes it)."""
    agent = await make_agent(name="Omp View", slug="omp-view", agent_runtime="cli-bridge", harness="omp")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await chat_record.pin_native_session(s, chat, "s-view")
        await s.commit()
        default = await chat_record.ensure_agent_chat(s, agent)
        await s.commit()
    assert not follower.following(chat.id)  # nothing sent since the "restart"
    try:
        resp = await auth_client.get(f"/api/v1/chat-record/chats/{chat.id}/history")
        assert resp.status_code == 200, resp.text
        assert follower.following(chat.id), "viewing a further chat must re-arm its live follow"
        resp = await auth_client.get(f"/api/v1/chat-record/chats/{default.id}/history")
        assert resp.status_code == 200, resp.text
        assert not follower.following(default.id)
        # The re-armed follow really writes: the agent answers after the restart.
        copy = (host_home / ".mc" / "agents" / "omp-view" / "omp-sessions" / "--workspace--"
                / ".mc" / "2026-11-23T13-01-40-000Z_s-view.jsonl")
        head = {"type": "session", "version": 3, "id": "s-view", "timestamp": "2026-11-23T13:01:40.000Z",
                "cwd": "/workspace", "bridge": "acp"}
        _write(copy, [head, _omp_line("m1", "user", "still there?", "2026-11-23T13:01:41.000Z"),
                      _omp_line("m2", "assistant", "Yes.", "2026-11-23T13:02:18.000Z")])
        assert await _wait_written(follower, 2, 10) < 10
    finally:
        await follower.stop()
    assert await _texts(chat.id) == ["still there?", "Yes."]


async def test_viewing_does_not_follow_a_chat_whose_harness_the_agent_cannot_run(
    auth_client, make_agent, host_home, flag_on, follower,  # noqa: F811
):
    # An omp chat on an agent that does not run omp over ACP in a container.
    agent = await make_agent(name="Omp Host", slug="omp-host", agent_runtime="host", harness="omp")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await s.commit()
    resp = await auth_client.get(f"/api/v1/chat-record/chats/{chat.id}/history")
    assert resp.status_code == 200, resp.text
    assert not follower.following(chat.id)


async def test_a_failed_live_import_is_tried_again_without_the_file_growing(
    make_agent, host_home, flag_on, follower, monkeypatch,  # noqa: F811
):
    """The last write of a turn does not grow the file again: a failed import
    (DB hiccup, lock) must not wait for the sweep."""
    from app.services import chat_record_import

    real = chat_record_import.import_session_file
    calls = []

    async def _flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database hiccup")
        return await real(*a, **kw)

    monkeypatch.setattr(chat_record_import, "import_session_file", _flaky)
    agent = await make_agent(name="Omp Retry", slug="omp-retry", agent_runtime="cli-bridge", harness="omp")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await chat_record.pin_native_session(s, chat, "s-retry")
        await s.commit()
    copy = (host_home / ".mc" / "agents" / "omp-retry" / "omp-sessions" / "--workspace--"
            / ".mc" / "2026-11-23T13-01-40-000Z_s-retry.jsonl")
    head = {"type": "session", "version": 3, "id": "s-retry", "timestamp": "2026-11-23T13:01:40.000Z",
            "cwd": "/workspace", "bridge": "acp"}
    _write(copy, [head, _omp_line("m1", "user", "q", "2026-11-23T13:01:41.000Z"),
                  _omp_line("m2", "assistant", "a", "2026-11-23T13:02:18.000Z")])
    follower.follow(agent, chat.id)
    try:
        took = await _wait_written(follower, 2, 6)
    finally:
        await follower.stop()
    assert follower.errors == 1
    assert took < 6, f"failed import never retried (calls={len(calls)})"
    assert await _texts(chat.id) == ["q", "a"]


def test_omp_further_chat_files_are_found_without_scanning_the_whole_folder(tmp_path, monkeypatch):
    """One lookup per followed chat per few seconds: only the chat's own
    files (by native id), never a stat + header read of every session."""
    from app.services import chat_record_import, omp_chat
    from app.services.transcript_adapters import adapter_for

    root = tmp_path / "omp-sessions"
    folder = root / "--workspace--"
    own = folder / "2026-11-23T13-02-04_s-mine.jsonl"
    copy = folder / ".mc" / "2026-11-23T13-02-04-123Z_s-mine.jsonl"
    other = folder / "2026-11-23T12-00-00_s-other.jsonl"
    _write(own, [{"type": "session", "id": "s-mine", "cwd": "/workspace"}])
    _write(copy, [{"type": "session", "id": "s-mine", "cwd": "/workspace", "bridge": "acp"}])
    _write(other, [{"type": "session", "id": "s-other", "cwd": "/workspace"}])

    class _Agent:
        slug = "x"
        harness = "omp"
        agent_runtime = "cli-bridge"

    import dataclasses

    adapter = dataclasses.replace(adapter_for(_Agent()), resolve_transcript_dir=lambda agent: root)

    def _no_full_scan(*a, **kw):
        raise AssertionError("full-folder scan")

    monkeypatch.setattr(chat_record_import, "session_files", _no_full_scan)
    looked_at = []
    real_header = omp_chat._session_header
    monkeypatch.setattr(omp_chat, "_session_header",
                        lambda p: looked_at.append(p.name) or real_header(p))
    assert chat_record_import.further_chat_files(_Agent(), adapter, "c", "s-mine") == [copy]
    assert other.name not in looked_at
    assert chat_record_import.further_chat_files(_Agent(), adapter, "c", "s-none") == []


async def test_sweep_reads_further_claude_chats_even_without_the_default_folder(
    make_agent, claude_home, flag_on,  # noqa: F811
):
    import dataclasses

    from app.services import chat_record_import, claude_chat_runner
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Claude NoDir", slug="claude-nodir", agent_runtime="cli-bridge",
                             harness="claude")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, None, agent, title="Chat 2")
        await s.commit()
    path = claude_chat_runner.transcript_path("claude-nodir", str(chat.id), chat.native_session_id)
    _write(path, [_user("u1", "2026-11-23T13:01:41.000Z", "hi"),
                  _assistant_text("a1", "2026-11-23T13:01:44.000Z", "hello")])
    adapter = dataclasses.replace(adapter_for(agent), resolve_transcript_dir=lambda a: None)
    stats = chat_record_import.ImportStats()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await chat_record_import.import_agent(s, agent, stats, adapter=adapter)
    assert await _texts(chat.id) == ["hi", "hello"]


def test_the_api_shutdown_ends_every_follow():
    """The follower lives in the API process, which runs without the
    background services (``ENABLE_BACKGROUND_SERVICES=false`` there) — so its
    stop belongs in the API lifespan, not in ``stop_background_services``."""
    import inspect

    from app import main

    src = inspect.getsource(main.lifespan)
    assert "chat_record_follow" in src and "FOLLOWER.stop()" in src.split("yield", 1)[1]


async def test_stop_ends_running_follows(make_agent, host_home, flag_on, follower):  # noqa: F811
    agent = await make_agent(name="Omp Stop", slug="omp-stop", agent_runtime="cli-bridge", harness="omp")
    follower.follow(agent, "00000000-0000-4000-8000-000000000003")
    assert follower.following("00000000-0000-4000-8000-000000000003")
    await follower.stop()
    assert not follower.following("00000000-0000-4000-8000-000000000003")
