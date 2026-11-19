"""Many chats per harness (slice S2 of "chats instead of fixed agents").

The backend part: a second (third, …) chat on the SAME agent pins its own
native session id, the importer and the live sink route that session's
events into that chat instead of the agent's default chat, and the resume
plan tells the harness side whether to start or to continue that session.

Fixture lines are synthetic copies of the Claude Code / omp JSONL schema,
the same helpers as tests/test_chat_record.py.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.chat_record import Chat, ChatEvent, ChatSession
from app.services import chat_record
from tests.conftest import test_engine
from tests.test_chat_record import (  # noqa: F401 — fixtures are used by name
    _claude_dir,
    _import_all,
    _omp_files,
    _session_one,
    _session_two,
    _test_factory,
    _write,
    flag_on,
    host_home,
)


class _User:
    """A caller without a users row: the chat gets no owner (the Postgres
    lane enforces the users FK; the API tests use a real user)."""

    def __init__(self, uid=None, role="admin"):
        self.id = uid
        self.role = role


async def _new_chat(agent, title="Second chat", user=None):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.create_chat(s, user or _User(), agent, title=title)
        await s.commit()
        return chat


async def _pin(chat_id, native):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = (await s.exec(select(Chat).where(Chat.id == chat_id))).one()
        moved = await chat_record.pin_native_session(s, chat, native)
        await s.commit()
        return moved


async def _chat_texts(chat_id) -> list[str]:
    async with AsyncSession(test_engine) as s:
        rows = (await s.exec(
            select(ChatEvent).where(ChatEvent.chat_id == chat_id, ChatEvent.kind == "message")
            .order_by(ChatEvent.ts, ChatEvent.seq)
        )).all()
    return [r.payload.get("text") for r in rows]


async def _default_chat(agent) -> Chat:
    async with AsyncSession(test_engine) as s:
        return (await s.exec(
            select(Chat).where(Chat.legacy_agent_id == agent.id, Chat.agent_default.is_(True))
        )).one()


# ── service ─────────────────────────────────────────────────────────────────


async def test_two_chats_on_one_claude_agent_keep_their_own_records(host_home, flag_on, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    second = await _new_chat(agent)
    # Claude Code: MC picks the id up front (``claude --session-id <id>``),
    # so the file of that session is named after it.
    assert second.native_session_id and uuid.UUID(second.native_session_id)
    folder = _claude_dir(host_home, "alpha")
    _write(folder / "older.jsonl", _session_one())
    _write(folder / f"{second.native_session_id}.jsonl", _session_two())

    await _import_all()

    default = await _default_chat(agent)
    assert default.id != second.id
    assert await _chat_texts(default.id) == [
        "please rename the config loader", "Looking at it.", "Renamed loader.py to settings_loader.py.",
    ]
    assert await _chat_texts(second.id) == ["what about the tests", "The tests are green."]
    # A second run changes nothing, and nothing lands twice.
    await _import_all()
    async with AsyncSession(test_engine) as s:
        assert (await s.exec(select(func.count()).select_from(ChatEvent))).one() == 6
        assert (await s.exec(select(func.count()).select_from(ChatSession))).one() == 2


async def test_live_recorder_writes_a_pinned_session_into_its_own_chat(host_home, flag_on, make_agent):
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    second = await _new_chat(agent)
    path = _claude_dir(host_home, "alpha") / f"{second.native_session_id}.jsonl"
    _write(path, [])
    rec = chat_record.LiveRecorder(agent, adapter_for(agent), None, session_factory=_test_factory)
    rec.record(path, {"kind": "message", "uuid": "x1", "ts": "2026-11-03T08:00:00.000Z",
                      "role": "user", "text": "only in the second chat"})
    await rec.wait_idle()
    rec.close()
    assert rec.errors == 0
    assert await _chat_texts(second.id) == ["only in the second chat"]
    assert await _chat_texts((await _default_chat(agent)).id) == []


async def test_running_live_recorder_follows_a_session_the_pin_moved(host_home, flag_on, make_agent):
    """The tailer cached "this file -> default chat"; a pin moves the session
    meanwhile. The next live event must land in the pinned chat, not in the
    old one under the moved session."""
    from app.services.transcript_adapters import adapter_for

    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    native, _bridge, _head, _msg = _omp_files(host_home, "omp-a")
    rec = chat_record.LiveRecorder(agent, adapter_for(agent), native, session_factory=_test_factory)
    await rec.backfilled.wait()
    rec.record(native, {"kind": "message", "uuid": "n2", "ts": "2026-11-01T10:00:02.000Z",
                        "role": "assistant", "text": "first answer"})
    await rec.wait_idle()
    default = await _default_chat(agent)
    assert await _chat_texts(default.id) == ["hello acp", "first answer"]

    second = await _new_chat(agent)
    await _pin(second.id, "s-acp")
    rec.record(native, {"kind": "message", "uuid": "n3", "ts": "2026-11-01T10:00:03.000Z",
                        "role": "user", "text": "after the pin"})
    await rec.wait_idle()
    rec.close()
    assert rec.errors == 0
    assert await _chat_texts(default.id) == []
    assert await _chat_texts(second.id) == ["hello acp", "first answer", "after the pin"]


async def test_default_chat_stays_one_per_agent(host_home, flag_on, make_agent):
    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    await _new_chat(agent)
    await _new_chat(agent, title="Third")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await chat_record.ensure_agent_chat(s, agent)
        b = await chat_record.ensure_agent_chat(s, agent)
        await s.commit()
    assert a.id == b.id and a.agent_default is True
    async with AsyncSession(test_engine) as s:
        rows = (await s.exec(select(Chat).where(Chat.legacy_agent_id == agent.id))).all()
    assert sorted(r.agent_default for r in rows) == [False, False, True]


async def test_pinning_an_omp_session_moves_rows_the_default_chat_already_has(host_home, flag_on, make_agent):
    """omp hands out the session id itself (ACP ``session/new``). The sweep
    can import the new file into the agent's default chat before the bridge
    reports the id; pinning moves that session over — once, without gaps."""
    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    native, _bridge, _head, _msg = _omp_files(host_home, "omp-a")
    await _import_all()
    default = await _default_chat(agent)
    assert await _chat_texts(default.id) == ["hello acp"]

    second = await _new_chat(agent)
    assert second.native_session_id is None  # omp: not known until session/new
    moved = await _pin(second.id, "s-acp")
    assert moved == 1

    assert await _chat_texts(default.id) == []
    assert await _chat_texts(second.id) == ["hello acp"]
    async with AsyncSession(test_engine) as s:
        sess = (await s.exec(select(ChatSession).where(ChatSession.native_session_id == "s-acp"))).one()
        assert sess.chat_id == second.id
    # The sweep agrees and doubles nothing.
    await _import_all()
    await _import_all()
    assert await _chat_texts(second.id) == ["hello acp"]
    assert await _chat_texts(default.id) == []
    # Pinning the same id again is a no-op.
    assert await _pin(second.id, "s-acp") == 0


async def test_moved_rows_get_fresh_unique_seqs_in_the_target_chat(host_home, flag_on, make_agent):
    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    _omp_files(host_home, "omp-a")
    await _import_all()
    second = await _new_chat(agent)
    async with AsyncSession(test_engine) as s:
        s.add(ChatEvent(chat_id=second.id, session_id=(await s.exec(select(ChatSession))).one().id,
                        seq=1, source_key="placeholder", kind="notice", payload={"kind": "notice"}))
        chat = (await s.exec(select(Chat).where(Chat.id == second.id))).one()
        chat.next_seq = 2
        s.add(chat)
        await s.commit()
    await _pin(second.id, "s-acp")
    async with AsyncSession(test_engine) as s:
        seqs = [e.seq for e in (await s.exec(select(ChatEvent).where(ChatEvent.chat_id == second.id))).all()]
        nxt = (await s.exec(select(Chat.next_seq).where(Chat.id == second.id))).one()
    assert sorted(seqs) == [1, 2] and nxt == 3


async def test_pin_conflicts_are_refused(host_home, flag_on, make_agent):
    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    a = await _new_chat(agent, title="A")
    b = await _new_chat(agent, title="B")
    await _pin(a.id, "s-1")
    with pytest.raises(chat_record.ChatRecordConflict) as exc:
        await _pin(b.id, "s-1")
    assert exc.value.reason == "pinned_by_other_chat"
    with pytest.raises(chat_record.ChatRecordConflict) as exc:
        await _pin(a.id, "s-2")
    assert exc.value.reason == "already_pinned"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        default = await chat_record.ensure_agent_chat(s, agent)
        await s.commit()
    with pytest.raises(chat_record.ChatRecordConflict) as exc:
        await _pin(default.id, "s-3")
    assert exc.value.reason == "default_chat"


async def test_create_refuses_a_harness_without_pinned_sessions(host_home, flag_on, make_agent):
    agent = await make_agent(name="Kimi", slug="kimi", harness="kimi")
    with pytest.raises(chat_record.ChatRecordInvalid) as exc:
        await _new_chat(agent)
    assert exc.value.reason == "harness_cannot_pin_sessions"


async def test_stray_rows_of_a_moved_session_are_healed_by_the_next_import(host_home, flag_on, make_agent):
    """Race: the sweep resolved the default chat, the pin moved the session
    meanwhile, then the sweep wrote. The rows sit in the wrong chat under
    the moved session — the next import puts them where they belong."""
    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    _omp_files(host_home, "omp-a")
    second = await _new_chat(agent)
    await _pin(second.id, "s-acp")
    default = await _default_chat(agent)
    async with AsyncSession(test_engine) as s:
        await chat_record.ensure_agent_chat(s, agent)
        sess = await chat_record.ensure_session(
            s, second, native_session_id="s-acp", harness="omp", runtime_id=None,
            model=None, place="container", transcript_path=None,
        )
        await chat_record.upsert_events(s, default.id, sess.id, [
            ("n1:message:0", {"kind": "message", "uuid": "n1", "role": "user", "text": "hello acp",
                              "ts": "2026-11-01T10:00:01.000Z"}),
        ])
        await s.commit()
    assert await _chat_texts(default.id) == ["hello acp"]
    await _import_all()
    assert await _chat_texts(default.id) == []
    assert await _chat_texts(second.id) == ["hello acp"]


async def test_resume_plan_starts_then_continues_the_pinned_session(host_home, flag_on, make_agent):
    claude = await make_agent(name="Alpha", slug="alpha", harness="claude")
    chat = await _new_chat(claude)
    async with AsyncSession(test_engine) as s:
        plan = await chat_record.resume_plan(s, chat)
    assert plan["action"] == "start"
    assert plan["claudeArgs"] == ["--session-id", chat.native_session_id]
    _write(_claude_dir(host_home, "alpha") / f"{chat.native_session_id}.jsonl", _session_two())
    await _import_all()
    async with AsyncSession(test_engine) as s:
        plan = await chat_record.resume_plan(s, chat)
    assert plan["action"] == "resume"
    assert plan["claudeArgs"] == ["--resume", chat.native_session_id]

    omp = await make_agent(name="Omp", slug="omp-a", harness="omp")
    ochat = await _new_chat(omp)
    async with AsyncSession(test_engine) as s:
        assert (await chat_record.resume_plan(s, ochat))["acp"] == {"method": "session/new"}
    await _pin(ochat.id, "s-acp")
    async with AsyncSession(test_engine) as s:
        ochat = (await s.exec(select(Chat).where(Chat.id == ochat.id))).one()
        plan = await chat_record.resume_plan(s, ochat)
    assert plan["action"] == "resume"
    assert plan["acp"] == {"method": "session/load", "sessionId": "s-acp"}


# ── API ─────────────────────────────────────────────────────────────────────

_BASE = "/api/v1/chat-record"


async def test_write_api_is_404_while_flag_is_off(auth_client):
    cid = uuid.uuid4()
    for method, url, body in (
        ("post", f"{_BASE}/chats", {"agentId": str(uuid.uuid4())}),
        ("patch", f"{_BASE}/chats/{cid}", {"title": "x"}),
        ("put", f"{_BASE}/chats/{cid}/native-session", {"nativeSessionId": "s-1"}),
        ("get", f"{_BASE}/chats/{cid}/resume", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(auth_client, method)(url, **kwargs)
        assert r.status_code == 404, url
        assert r.json()["detail"] == {"reason": "chat_record_disabled"}


async def test_api_creates_lists_renames_and_archives_chats(host_home, flag_on, make_agent, auth_client):
    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    r = await auth_client.post(f"{_BASE}/chats", json={"agentId": str(agent.id), "title": "Refactor"})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["title"] == "Refactor"
    assert created["agentDefault"] is False and created["nativeSessionId"]
    assert created["ownerUserId"] == "00000000-0000-0000-0000-000000000099"

    r = await auth_client.get(f"{_BASE}/chats", params={"agentId": str(agent.id)})
    listed = r.json()["chats"]
    # The agent's default chat is created with it, so un-pinned sessions keep a home.
    assert [(c["title"], c["agentDefault"]) for c in listed] == [("Refactor", False), ("Alpha", True)]

    r = await auth_client.patch(f"{_BASE}/chats/{created['id']}", json={"title": "Refactor v2"})
    assert r.status_code == 200 and r.json()["title"] == "Refactor v2"

    r = await auth_client.get(f"{_BASE}/chats/{created['id']}/resume")
    assert r.status_code == 200
    assert r.json()["claudeArgs"] == ["--session-id", created["nativeSessionId"]]

    r = await auth_client.patch(f"{_BASE}/chats/{created['id']}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    left = (await auth_client.get(f"{_BASE}/chats", params={"agentId": str(agent.id)})).json()["chats"]
    assert [c["title"] for c in left] == ["Alpha"]


async def test_api_refuses_bad_input(host_home, flag_on, make_agent, auth_client):
    r = await auth_client.post(f"{_BASE}/chats", json={"agentId": str(uuid.uuid4())})
    assert r.status_code == 404 and r.json()["detail"] == {"reason": "agent_not_found"}
    kimi = await make_agent(name="Kimi", slug="kimi", harness="kimi")
    r = await auth_client.post(f"{_BASE}/chats", json={"agentId": str(kimi.id)})
    assert r.status_code == 422 and r.json()["detail"] == {"reason": "harness_cannot_pin_sessions"}

    omp = await make_agent(name="Omp", slug="omp-a", harness="omp")
    cid = (await auth_client.post(f"{_BASE}/chats", json={"agentId": str(omp.id)})).json()["id"]
    # The id goes to a CLI flag / an ACP call: nothing but a plain token.
    for bad in ("../../etc", "a b", "", "x" * 201):
        r = await auth_client.put(f"{_BASE}/chats/{cid}/native-session", json={"nativeSessionId": bad})
        assert r.status_code == 422, bad
    r = await auth_client.put(f"{_BASE}/chats/{cid}/native-session", json={"nativeSessionId": "s-acp"})
    assert r.status_code == 200 and r.json()["nativeSessionId"] == "s-acp"
    r = await auth_client.put(f"{_BASE}/chats/{cid}/native-session", json={"nativeSessionId": "s-other"})
    assert r.status_code == 409 and r.json()["detail"] == {"reason": "already_pinned"}


async def test_api_keeps_the_default_chat(host_home, flag_on, make_agent, auth_client):
    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    _write(_claude_dir(host_home, "alpha") / "s.jsonl", _session_one())
    await _import_all()
    default = await _default_chat(agent)
    r = await auth_client.patch(f"{_BASE}/chats/{default.id}", json={"archived": True})
    assert r.status_code == 409 and r.json()["detail"] == {"reason": "default_chat"}
    r = await auth_client.patch(f"{_BASE}/chats/{default.id}", json={"title": "Alpha main"})
    assert r.status_code == 200 and r.json()["title"] == "Alpha main"


async def test_api_write_routes_need_admin(host_home, flag_on, make_agent, client):
    from app.auth import create_access_token
    from app.models.user import User

    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email="op@mc.local", name="Op", role="operator", is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), 'operator')}"
    r = await client.post(f"{_BASE}/chats", json={"agentId": str(agent.id)})
    assert r.status_code == 403


# ── Postgres lane: real partial unique indexes, real transactions ──────────


@pytest.mark.postgres
async def test_pg_default_chat_is_created_once_under_concurrency(host_home, flag_on, make_agent):
    """``ON CONFLICT … WHERE agent_default`` must match the partial index
    of migration 0229, or Postgres refuses the statement."""
    import asyncio

    agent = await make_agent(name="Alpha", slug="alpha", harness="claude")
    await _new_chat(agent)

    async def _ensure():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            chat = await chat_record.ensure_agent_chat(s, agent)
            await s.commit()
            return chat.id

    ids = await asyncio.gather(_ensure(), _ensure(), _ensure())
    assert len(set(ids)) == 1
    async with AsyncSession(test_engine) as s:
        n = (await s.exec(select(func.count()).select_from(Chat).where(Chat.legacy_agent_id == agent.id))).one()
    assert n == 2


@pytest.mark.postgres
async def test_pg_pin_moves_the_session_and_a_second_pin_is_refused(host_home, flag_on, make_agent):
    agent = await make_agent(name="Omp", slug="omp-a", harness="omp")
    _omp_files(host_home, "omp-a")
    await _import_all()
    a = await _new_chat(agent, title="A")
    b = await _new_chat(agent, title="B")
    assert await _pin(a.id, "s-acp") == 1
    assert await _chat_texts(a.id) == ["hello acp"]
    with pytest.raises(chat_record.ChatRecordConflict):
        await _pin(b.id, "s-acp")
    await _import_all()
    assert await _chat_texts(a.id) == ["hello acp"]
    assert await _chat_texts((await _default_chat(agent)).id) == []
