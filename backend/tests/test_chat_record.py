"""MC's own chat record (ADR-103): importer, live sink, read API, flag.

Fixture lines are synthetic copies of the Claude Code / omp JSONL schema
(structure kept, content neutral), like tests/test_transcript_chat_history.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import Chat, ChatEvent, ChatSession
from app.services import chat_record, chat_record_import
from tests.conftest import test_engine

# ── fixtures ────────────────────────────────────────────────────────────────


def _user(uid, ts, text):
    return {
        "type": "user", "uuid": uid, "timestamp": ts, "isSidechain": False,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_tool(uid, ts, text, tool_id, name, tool_input):
    return {
        "type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": False,
        "message": {
            "role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
            "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input},
            ],
        },
    }


def _tool_result(uid, ts, tool_id, content):
    return {
        "type": "user", "uuid": uid, "timestamp": ts, "isSidechain": False,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False},
        ]},
    }


def _assistant_text(uid, ts, text):
    return {
        "type": "assistant", "uuid": uid, "timestamp": ts, "isSidechain": False,
        "message": {"role": "assistant", "model": "claude-sonnet-4-6", "id": f"msg_{uid}",
                    "content": [{"type": "text", "text": text}]},
    }


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def _append(path: Path, lines: list[dict]) -> None:
    with path.open("a") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _session_one() -> list[dict]:
    return [
        _user("u1", "2026-11-01T10:00:00.000Z", "please rename the config loader"),
        _assistant_tool("a1", "2026-11-01T10:00:05.000Z", "Looking at it.", "tool-1", "Bash", {"command": "ls"}),
        _tool_result("r1", "2026-11-01T10:00:06.000Z", "tool-1", "config.py\nloader.py"),
        _assistant_text("a2", "2026-11-01T10:00:09.000Z", "Renamed loader.py to settings_loader.py."),
    ]


def _session_two() -> list[dict]:
    return [
        _user("u2", "2026-11-02T09:00:00.000Z", "what about the tests"),
        _assistant_text("a3", "2026-11-02T09:00:04.000Z", "The tests are green."),
    ]


@pytest.fixture
def host_home(tmp_path, monkeypatch):
    from app.services import omp_chat, transcript_chat

    monkeypatch.setattr(transcript_chat, "_host_home", lambda: tmp_path)
    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


def _claude_dir(home: Path, slug: str) -> Path:
    return home / ".mc" / "agents" / slug / "claude-config" / "projects" / "-home-agent"


async def _counts(chat_id=None) -> dict:
    async with AsyncSession(test_engine) as s:
        q = select(func.count()).select_from(ChatEvent)
        if chat_id is not None:
            q = q.where(ChatEvent.chat_id == chat_id)
        events = (await s.exec(q)).one()
        chats = (await s.exec(select(func.count()).select_from(Chat))).one()
        sessions = (await s.exec(select(func.count()).select_from(ChatSession))).one()
    return {"events": events, "chats": chats, "sessions": sessions}


async def _events(chat_id) -> list[ChatEvent]:
    async with AsyncSession(test_engine) as s:
        return list((await s.exec(
            select(ChatEvent).where(ChatEvent.chat_id == chat_id).order_by(ChatEvent.ts, ChatEvent.seq)
        )).all())


async def _import_all() -> chat_record_import.ImportStats:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await chat_record_import.import_all(s)


def _digest(paths) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


# ── importer ────────────────────────────────────────────────────────────────


async def test_import_builds_one_chat_per_agent_with_all_sessions(host_home, flag_on, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha")
    tdir = _claude_dir(host_home, "alpha")
    one, two = tdir / "s-one.jsonl", tdir / "s-two.jsonl"
    _write(one, _session_one())
    _write(two, _session_two())
    import os
    os.utime(one, (1_700_000_000, 1_700_000_000))
    os.utime(two, (1_700_000_100, 1_700_000_100))
    before = _digest([one, two])

    stats = await _import_all()

    assert stats.files_imported == 2 and stats.errors == []
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    assert chat.title == "Alpha"
    evs = await _events(chat.id)
    kinds = [e.kind for e in evs]
    # Session one: user msg, assistant text, tool (with merged result), text;
    # session two: two messages — in time order across both sessions.
    assert kinds == ["message", "message", "tool", "message", "message", "message"]
    tool = next(e for e in evs if e.kind == "tool")
    assert tool.payload["result"] == "config.py\nloader.py"
    assert tool.source_key == "tool:tool-1"
    assert len({e.session_id for e in evs}) == 2
    # Files stay untouched.
    assert _digest([one, two]) == before


async def test_import_is_idempotent_and_picks_up_growth(host_home, flag_on, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "s-one.jsonl"
    _write(path, _session_one())

    first = await _import_all()
    again = await _import_all()
    assert first.events == 4
    assert again.files_skipped_unchanged == 1 and again.events == 0
    assert (await _counts())["events"] == 4

    _append(path, _session_two())
    grown = await _import_all()
    assert grown.files_imported == 1
    assert (await _counts())["events"] == 6
    assert (await _counts())["chats"] == 1 and (await _counts())["sessions"] == 1


async def test_import_skips_files_the_privacy_gate_rejects(host_home, flag_on, make_agent, monkeypatch):
    await make_agent(name="Alpha", slug="alpha")
    _write(_claude_dir(host_home, "alpha") / "s.jsonl", _session_one())
    import dataclasses

    from app.services import transcript_adapters

    real = transcript_adapters.adapter_for

    def _gated(agent):
        return dataclasses.replace(real(agent), transcript_allowed=lambda agent, path: False)

    monkeypatch.setattr(transcript_adapters, "adapter_for", _gated)
    stats = await _import_all()
    assert stats.files_skipped_gate == 1
    assert (await _counts())["events"] == 0


async def test_import_refuses_while_flag_is_off(host_home, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    _write(_claude_dir(host_home, "alpha") / "s.jsonl", _session_one())
    with pytest.raises(RuntimeError):
        await _import_all()
    assert (await _counts())["events"] == 0


async def test_import_omp_skips_task_sessions(host_home, flag_on, make_agent):
    await make_agent(name="Omp", slug="omp-a", harness="omp")
    root = host_home / ".mc" / "agents" / "omp-a" / "omp-sessions"
    head = {"type": "session", "version": 3, "id": "s-chat", "timestamp": "2026-11-01T10:00:00.000Z", "cwd": "/workspace"}
    user = {"type": "message", "id": "m1", "parentId": None, "timestamp": "2026-11-01T10:00:01.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "hello omp"}]}}
    _write(root / "--workspace--" / "2026-11-01T10-00-00-000_s-chat.jsonl", [head, user])
    task_head = dict(head, id="s-task", cwd="/workspace/worktrees/x")
    task_user = dict(user, id="m2", message={"role": "user", "content": [{"type": "text", "text": "task work"}]})
    _write(root / "--workspace-worktrees-x--" / "2026-11-01T11-00-00-000_s-task.jsonl", [task_head, task_user])

    stats = await _import_all()
    assert stats.files_imported == 1, stats
    async with AsyncSession(test_engine) as s:
        texts = [e.search_text for e in (await s.exec(select(ChatEvent))).all()]
    assert texts == ["hello omp"]


# ── keys: importer and live sink agree ──────────────────────────────────────


def test_key_counter_is_stable_per_entry():
    c = chat_record.KeyCounter()
    a = {"kind": "message", "uuid": "x"}
    assert c.key(a) == "x:message:0"
    assert c.key(a) == "x:message:1"
    assert c.key({"kind": "tool", "uuid": "x", "toolUseId": "t"}) == "tool:t"
    assert chat_record.KeyCounter.update_key({"kind": "command", "uuid": "y"}) == "y:command:0"
    assert not chat_record.is_recordable({"kind": "tool_output", "uuid": None, "toolUseId": "t"})


def test_payload_cap_cuts_and_flags_big_results():
    big = "x" * (chat_record.MAX_RESULT_CHARS + 10)
    out = chat_record.capped_payload({"kind": "tool", "result": big})
    assert len(out["result"]) == chat_record.MAX_RESULT_CHARS and out["resultTruncated"] is True


def _test_factory():
    return AsyncSession(test_engine, expire_on_commit=False)


async def test_live_recorder_upserts_republished_card_and_matches_import(host_home, flag_on, make_agent):
    """The tailer publishes a tool card first without result, then again with
    it; a later import of the same file must not double anything."""
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "live.jsonl"
    _write(path, [])
    adapter = adapter_for(agent)
    rec = chat_record.LiveRecorder(agent, adapter, None, session_factory=_test_factory)
    tool = {"kind": "tool", "uuid": "a1", "ts": "2026-11-01T10:00:05.000Z", "name": "Bash",
            "title": "ls", "detail": {}, "toolUseId": "tool-1", "result": None, "status": "done",
            "sidechain": False}
    msg = {"kind": "message", "uuid": "a1", "ts": "2026-11-01T10:00:05.000Z", "role": "assistant",
           "text": "Looking at it.", "model": None, "sidechain": False}
    rec.record(path, msg)
    rec.record(path, tool)
    rec.record(path, dict(tool, result="config.py\nloader.py"), republish=True)
    rec.record(path, {"kind": "tool_output", "uuid": None, "toolUseId": "tool-1", "text": "…"})
    await rec.wait_idle()
    rec.close()
    assert rec.errors == 0
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    evs = await _events(chat.id)
    assert [e.kind for e in evs] == ["message", "tool"]
    assert evs[1].payload["result"] == "config.py\nloader.py"

    # The file now holds the same entries — importing it adds nothing new.
    _write(path, _session_one()[:3])
    await _import_all()
    evs = await _events(chat.id)
    assert [e.source_key for e in evs] == ["u1:message:0", "a1:message:0", "tool:tool-1"]


async def test_tailer_writes_into_the_record_while_the_chat_runs(host_home, flag_on, make_agent, monkeypatch):
    import app.database
    import app.services.runtime_window as rw
    import app.services.transcript_chat as tc

    agent = await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "live.jsonl"
    _write(path, _session_one()[:1])  # written before anyone watched

    async def _none(*a, **k):
        return None

    async def _broadcast(*a, **k):
        return None

    monkeypatch.setattr(app.database, "async_session_maker", _test_factory)
    monkeypatch.setattr(rw, "agent_runtime_window", _none)
    monkeypatch.setattr(rw, "current_agent_runtime_window", _none)
    monkeypatch.setattr(tc, "capture_pane", _none)
    monkeypatch.setattr(tc.sse, "broadcast", _broadcast)
    monkeypatch.setattr(tc.ChatTailerManager, "_start_preview", _none)

    async def _state(self, *a, **k):
        return {}

    monkeypatch.setattr(tc.ChatTailerManager, "_compute_pane_state", _state)
    recorders: list = []
    real_for = chat_record.live_recorder_for

    def _capture(*a, **k):
        rec = real_for(*a, **k)
        recorders.append(rec)
        return rec

    monkeypatch.setattr(chat_record, "live_recorder_for", _capture)
    m = tc.ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    await m.acquire(str(agent.id), path, agent)
    try:
        # Lines written AFTER the backfill read the file can only reach the
        # record through the live stream.
        for _ in range(200):
            if recorders and recorders[0] is not None and recorders[0].backfilled.is_set():
                break
            await asyncio.sleep(0.02)
        assert recorders[0].backfilled.is_set()
        _append(path, _session_one()[1:])
        # Poll the recorder, never the DB: the SQLite test lane shares ONE
        # connection, a second session would roll back the writer's work.
        for _ in range(200):
            if recorders and recorders[0].written >= 3:
                break
            await asyncio.sleep(0.02)
    finally:
        await m.release(str(agent.id))
    assert recorders and recorders[0] is not None
    await recorders[0].wait_idle()
    assert recorders[0].errors == 0
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    evs = await _events(chat.id)
    # The first line came from the backfill, the rest from the live stream.
    assert [e.source_key for e in evs] == ["u1:message:0", "a1:message:0", "tool:tool-1", "a2:message:0"]
    assert evs[2].payload["result"] == "config.py\nloader.py"


async def test_tailer_writes_nothing_while_flag_is_off(host_home, make_agent):
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    assert chat_record.live_recorder_for(agent, adapter_for(agent), None) is None


# ── read API ────────────────────────────────────────────────────────────────


async def _seed_two_agents(host_home, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    await make_agent(name="Beta", slug="beta")
    _write(_claude_dir(host_home, "alpha") / "a.jsonl", _session_one())
    _write(_claude_dir(host_home, "beta") / "b.jsonl", _session_two())
    await _import_all()


async def test_api_is_404_while_flag_is_off(auth_client):
    for url in ("/api/v1/chat-record/chats", "/api/v1/chat-record/search?q=loader",
                f"/api/v1/chat-record/chats/{uuid.uuid4()}/history"):
        r = await auth_client.get(url)
        assert r.status_code == 404, url
        assert r.json()["detail"] == {"reason": "chat_record_disabled"}


async def test_api_lists_chats_reads_history_and_pages(host_home, flag_on, make_agent, auth_client):
    await _seed_two_agents(host_home, make_agent)
    r = await auth_client.get("/api/v1/chat-record/chats")
    assert r.status_code == 200
    chats = r.json()["chats"]
    assert [c["title"] for c in chats] == ["Beta", "Alpha"]  # newest activity first
    rex = next(c for c in chats if c["title"] == "Alpha")
    assert rex["events"] == 4 and rex["sessions"] == 1

    r = await auth_client.get(f"/api/v1/chat-record/chats/{rex['id']}/history?limit=2")
    page = r.json()
    assert page["hasMore"] is True
    assert [e["event"]["kind"] for e in page["events"]] == ["tool", "message"]
    r = await auth_client.get(
        f"/api/v1/chat-record/chats/{rex['id']}/history?limit=2&before={page['events'][0]['id']}"
    )
    older = r.json()
    assert [e["event"]["text"] for e in older["events"]] == [
        "please rename the config loader", "Looking at it."
    ]
    assert older["hasMore"] is False


async def test_api_search_finds_across_chats(host_home, flag_on, make_agent, auth_client):
    await _seed_two_agents(host_home, make_agent)
    r = await auth_client.get("/api/v1/chat-record/search?q=tests")
    hits = r.json()["hits"]
    assert {h["chatTitle"] for h in hits} == {"Beta"}
    r = await auth_client.get("/api/v1/chat-record/search?q=loader")
    assert {h["chatTitle"] for h in r.json()["hits"]} == {"Alpha"}


async def test_api_hides_chats_owned_by_someone_else(host_home, flag_on, make_agent, client):
    from app.auth import create_access_token
    from app.models.user import User

    await _seed_two_agents(host_home, make_agent)
    other, viewer = uuid.uuid4(), uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=other, email="o@mc.local", name="O", role="operator", is_active=True))
        s.add(User(id=viewer, email="v@mc.local", name="V", role="operator", is_active=True))
        await s.commit()
        chat = (await s.exec(select(Chat).where(Chat.title == "Alpha"))).one()
        chat.owner_user_id = other
        s.add(chat)
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(viewer), 'operator')}"
    titles = [c["title"] for c in (await client.get("/api/v1/chat-record/chats")).json()["chats"]]
    assert titles == ["Beta"]
    assert (await client.get(f"/api/v1/chat-record/chats/{chat.id}/history")).status_code == 404
    assert (await client.get("/api/v1/chat-record/search?q=loader")).json()["hits"] == []


# ── Postgres lane: real transactions, real full-text index ─────────────────


@pytest.mark.postgres
async def test_pg_two_writers_at_once_never_double_or_collide(host_home, flag_on, make_agent):
    """Importer and live sink write the same events in two real transactions
    at the same time: every key once, every seq unique."""
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "s.jsonl"
    lines = []
    for i in range(200):
        lines.append(_user(f"u{i}", f"2026-11-01T10:{i // 60:02d}:{i % 60:02d}.000Z", f"question number {i}"))
    _write(path, lines)
    adapter = adapter_for(agent)

    async def _import():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await chat_record_import.import_session_file(s, agent, adapter, path, force=True)
            await s.commit()

    await asyncio.gather(_import(), _import(), _import())
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
        n = (await s.exec(select(func.count()).select_from(ChatEvent))).one()
        seqs = (await s.exec(select(func.count(func.distinct(ChatEvent.seq))))).one()
    assert n == 200 and seqs == 200
    assert (await _counts())["chats"] == 1


@pytest.mark.postgres
async def test_pg_search_uses_full_text(host_home, flag_on, make_agent):
    from sqlalchemy import text

    await _seed_two_agents(host_home, make_agent)
    async with AsyncSession(test_engine) as s:
        idx = (await s.exec(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chat_events_search'"
        ))).first()
        assert idx is not None and "to_tsvector" in idx[0]
        hits = await chat_record.search(s, type("U", (), {"role": "admin"})(), "rename config")
    assert [h["chatTitle"] for h in hits] == ["Alpha"]
    assert "rename the config" in hits[0]["snippet"]


async def test_nul_characters_never_reach_the_database(host_home, flag_on, make_agent):
    """Real transcripts carry U+0000 in tool output; Postgres text and JSONB
    refuse it (found on the measurement run, 2026-11-18). The record drops it."""
    agent = await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "nul.jsonl"
    lines = _session_one()[:3]
    lines[2] = _tool_result("r1", "2026-11-01T10:00:06.000Z", "tool-1", "bin\u0000ary")
    lines[0] = _user("u1", "2026-11-01T10:00:00.000Z", "nul\u0000here")
    _write(path, lines)
    stats = await _import_all()
    assert stats.errors == [] and stats.events == 3
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    evs = await _events(chat.id)
    assert evs[0].payload["text"] == "nulhere" and evs[0].search_text == "nulhere"
    assert next(e for e in evs if e.kind == "tool").payload["result"] == "binary"


def test_nul_strip_keeps_a_literal_backslash_u_text_intact():
    out = chat_record._strip_nul({"text": "see \\u0000 and real\u0000nul", "items": ["a\u0000"]})
    assert out == {"text": "see \\u0000 and realnul", "items": ["a"]}


# ── critic round 1 (2026-11-18) ────────────────────────────────────────────


def _dup_tool_session() -> list[dict]:
    """A long or compacted Claude Code session repeats a tool_use id under a
    NEW entry uuid (10 of 832 real files). Both turn into ``tool:tool-1``."""
    return [
        *_session_one(),
        _assistant_tool("a1b", "2026-11-01T10:00:20.000Z", "Again.", "tool-1", "Bash", {"command": "ls"}),
    ]


async def _assert_dup_tool_file_imports(host_home, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha")
    _write(_claude_dir(host_home, "alpha") / "dup.jsonl", _dup_tool_session())
    stats = await _import_all()
    assert stats.errors == [] and stats.files_imported == 1, stats
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    evs = await _events(chat.id)
    assert [e.source_key for e in evs].count("tool:tool-1") == 1
    # The card keeps its result — the later repeat without one never wipes it.
    assert next(e for e in evs if e.kind == "tool").payload["result"] == "config.py\nloader.py"


async def test_duplicate_tool_id_in_one_file_imports(host_home, flag_on, make_agent):
    await _assert_dup_tool_file_imports(host_home, make_agent)


@pytest.mark.postgres
async def test_pg_duplicate_tool_id_in_one_file_imports(host_home, flag_on, make_agent):
    """Postgres refuses two rows with the same conflict key in ONE
    ``INSERT .. ON CONFLICT DO UPDATE`` (CardinalityViolation); SQLite does
    not — this is the lane that catches it."""
    await _assert_dup_tool_file_imports(host_home, make_agent)


async def test_failing_file_is_marked_and_retried_only_after_it_changes(
    host_home, flag_on, make_agent, auth_client, monkeypatch
):
    await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "bad.jsonl"
    _write(path, _session_one())
    real = chat_record.upsert_events

    async def _boom(*a, **k):
        raise RuntimeError("write refused")

    monkeypatch.setattr(chat_record, "upsert_events", _boom)
    first = await _import_all()
    assert first.errors == ["alpha/bad.jsonl: RuntimeError"]
    async with AsyncSession(test_engine) as s:
        row = (await s.exec(select(ChatSession))).one()
    assert row.last_error == "RuntimeError" and row.failed_bytes == path.stat().st_size

    # Unchanged file: not read again, but still counted as failed.
    again = await _import_all()
    assert again.errors == [] and again.files_failed_unchanged == 1

    # Visible in the read API, not only in a log line.
    chats = (await auth_client.get("/api/v1/chat-record/chats")).json()["chats"]
    assert chats[0]["failedSessions"] == 1
    hist = (await auth_client.get(f"/api/v1/chat-record/chats/{chats[0]['id']}/history")).json()
    assert hist["sessions"][0]["error"] == "RuntimeError"

    # The file grows -> tried again; success clears the mark.
    monkeypatch.setattr(chat_record, "upsert_events", real)
    _append(path, _session_two())
    healed = await _import_all()
    assert healed.files_imported == 1 and healed.errors == []
    async with AsyncSession(test_engine) as s:
        row = (await s.exec(select(ChatSession))).one()
    assert row.last_error is None and row.failed_bytes == 0


async def _next_seq(agent_id) -> int:
    async with AsyncSession(test_engine) as s:
        return (await s.exec(select(Chat.next_seq).where(Chat.legacy_agent_id == agent_id))).one()


async def test_growth_writes_only_the_new_rows(host_home, flag_on, make_agent):
    """One appended line on a grown file: one row written, one seq used —
    the unchanged rows are not rewritten (critic: 51 written, 50 rewritten)."""
    agent = await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "s.jsonl"
    lines = [_user(f"u{i}", f"2026-11-01T10:00:{i:02d}.000Z", f"line {i}") for i in range(50)]
    _write(path, lines)
    first = await _import_all()
    assert first.events == 50
    seq_before = await _next_seq(agent.id)
    _append(path, [_user("u50", "2026-11-01T10:01:00.000Z", "line 50")])
    grown = await _import_all()
    assert grown.events == 1
    assert await _next_seq(agent.id) == seq_before + 1


@pytest.mark.postgres
async def test_pg_growth_leaves_unchanged_rows_alone(host_home, flag_on, make_agent):
    from sqlalchemy import text

    await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "s.jsonl"
    _write(path, [_user(f"u{i}", f"2026-11-01T10:00:{i:02d}.000Z", f"line {i}") for i in range(50)])
    await _import_all()
    async with AsyncSession(test_engine) as s:
        before = dict((await s.exec(text("SELECT source_key, xmin::text FROM chat_events"))).all())
    _append(path, [_user("u50", "2026-11-01T10:01:00.000Z", "line 50")])
    await _import_all()
    async with AsyncSession(test_engine) as s:
        after = dict((await s.exec(text("SELECT source_key, xmin::text FROM chat_events"))).all())
    assert len(after) == 51
    assert [k for k in before if before[k] != after[k]] == []


async def test_changed_payload_is_still_updated(host_home, flag_on, make_agent):
    """The no-rewrite rule must not freeze a card: a tool result that
    arrives later still lands on the existing row."""
    agent = await make_agent(name="Alpha", slug="alpha")
    path = _claude_dir(host_home, "alpha") / "s.jsonl"
    _write(path, _session_one()[:2])
    await _import_all()
    _append(path, _session_one()[2:])
    await _import_all()
    async with AsyncSession(test_engine) as s:
        chat = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    tool = next(e for e in await _events(chat.id) if e.kind == "tool")
    assert tool.payload["result"] == "config.py\nloader.py"


async def test_recorder_backfills_every_session_file_not_only_the_current(host_home, flag_on, make_agent):
    """A session that rolled over while nobody watched (model/runtime
    switch) reaches the record when the next viewer opens the chat."""
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha")
    tdir = _claude_dir(host_home, "alpha")
    old, new = tdir / "old.jsonl", tdir / "new.jsonl"
    _write(old, _session_one())
    _write(new, _session_two())
    rec = chat_record.LiveRecorder(agent, adapter_for(agent), new, session_factory=_test_factory)
    await rec.backfilled.wait()
    rec.close()
    assert rec.errors == 0
    assert (await _counts())["events"] == 6
    assert (await _counts())["sessions"] == 2


async def test_scheduled_sync_imports_without_any_viewer(host_home, flag_on, make_agent, monkeypatch):
    import app.database

    monkeypatch.setattr(app.database, "async_session_maker", _test_factory)
    await make_agent(name="Alpha", slug="alpha")
    _write(_claude_dir(host_home, "alpha") / "s.jsonl", _session_one())
    stats = await chat_record_import.chat_record_sync.sweep_once()
    assert stats is not None and stats.events == 4
    assert (await _counts())["events"] == 4


async def test_scheduled_sync_idles_while_flag_is_off(host_home, make_agent):
    await make_agent(name="Alpha", slug="alpha")
    _write(_claude_dir(host_home, "alpha") / "s.jsonl", _session_one())
    assert await chat_record_import.chat_record_sync.sweep_once() is None
    assert (await _counts())["events"] == 0


def _omp_files(host_home, slug):
    folder = host_home / ".mc" / "agents" / slug / "omp-sessions" / "--workspace--"
    head = {"type": "session", "version": 3, "id": "s-acp", "timestamp": "2026-11-01T10:00:00.000Z",
            "cwd": "/workspace"}

    def _msg(mid, text):
        return {"type": "message", "id": mid, "parentId": None, "timestamp": "2026-11-01T10:00:01.000Z",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    native = folder / "2026-11-01T10-00-00_s-acp.jsonl"
    bridge = folder / ".mc" / "2026-11-01T10-00-00-000Z_s-acp.jsonl"
    _write(native, [head, _msg("n1", "hello acp")])
    return native, bridge, head, _msg


async def test_omp_native_then_bridge_file_never_doubles_a_turn(host_home, flag_on, make_agent):
    """Under ACP the tailer can start on omp's own file and move to the
    bridge's copy of the SAME session. The two files have independent entry
    ids; the record must end with the turn once (the bridge's)."""
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    adapter = adapter_for(agent)
    native, bridge, head, _msg = _omp_files(host_home, "omp-a")
    rec = chat_record.LiveRecorder(agent, adapter, native, session_factory=_test_factory)
    await rec.backfilled.wait()
    _write(bridge, [dict(head, bridge="acp"), _msg("b1", "hello acp")])
    rec.record(bridge, {"kind": "message", "uuid": "b1", "ts": "2026-11-01T10:00:01.000Z",
                        "role": "user", "text": "hello acp"})
    await rec.wait_idle()
    rec.close()
    assert rec.errors == 0
    async with AsyncSession(test_engine) as s:
        texts = [e.search_text for e in (await s.exec(select(ChatEvent))).all()]
    assert texts == ["hello acp"]
    # A later importer run agrees.
    await _import_all()
    assert (await _counts())["events"] == 1
