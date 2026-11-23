"""Search across chats (concept P3): word-start matching, one best hit per
chat with the event to jump to and a snippet, only the user's and the
assistant's message text, stable ranking (exact > prefix > substring, then
newest). Events are written straight into the record (neutral content), so
each test states exactly what is searchable.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.chat_record import Chat, ChatEvent, ChatSession
from app.services import chat_record_search
from tests.conftest import POSTGRES_LANE, test_engine

ADMIN = type("U", (), {"role": "admin", "id": None})()
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


async def _chat(title: str, events: list[tuple], *, agent_id=None, owner=None, archived=False) -> dict:
    """``events`` = (kind, role, text, minutes after T0). Returns ids."""
    chat = Chat(title=title, legacy_agent_id=agent_id, owner_user_id=owner)
    if archived:
        chat.archived_at = T0
    sess = ChatSession(chat_id=chat.id, native_session_id=f"n-{uuid.uuid4()}")
    ids = []
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(chat)
        await s.flush()
        s.add(sess)
        await s.flush()
        for seq, (kind, role, text, minute) in enumerate(events, start=1):
            ev = ChatEvent(
                chat_id=chat.id, session_id=sess.id, seq=seq, source_key=f"k{seq}",
                ts=T0 + timedelta(minutes=minute), kind=kind, role=role,
                payload={"kind": kind, "role": role, "text": text}, search_text=text,
            )
            s.add(ev)
            ids.append(str(ev.id))
        await s.commit()
    return {"chat": str(chat.id), "events": ids}


async def _search(q: str, **kw) -> list[dict]:
    async with AsyncSession(test_engine) as s:
        return await chat_record_search.search(s, kw.pop("user", ADMIN), q, **kw)


# ── matching ────────────────────────────────────────────────────────────────


async def test_word_start_finds_the_whole_word():
    """"deplo" finds "deploy" — the old search needed whole words."""
    c = await _chat("Release", [("message", "user", "please deploy the backend tonight", 1)])
    hits = await _search("deplo")
    assert [h["chatId"] for h in hits] == [c["chat"]]
    assert hits[0]["eventId"] == c["events"][0]
    assert hits[0]["match"] == "prefix"


async def test_every_word_must_match():
    await _chat("One", [("message", "user", "deploy the backend", 1)])
    await _chat("Two", [("message", "user", "deploy the frontend", 2)])
    hits = await _search("deplo back")
    assert [h["chatTitle"] for h in hits] == ["One"]


async def test_only_user_and_assistant_message_text_is_searched():
    """Thinking, tool titles, command output and teammate notes are noise."""
    await _chat("Noise", [
        ("thinking", None, "maybe the kettle is broken", 1),
        ("tool", None, "kettle inspector", 2),
        ("command", None, "grep kettle", 3),
        ("message", "teammate", "kettle report", 4),
        ("message", "system", "kettle hint", 5),
    ])
    good = await _chat("Real", [("message", "assistant", "The kettle works again.", 6)])
    hits = await _search("kettle")
    assert [h["chatId"] for h in hits] == [good["chat"]]


async def test_archived_chats_are_not_searched():
    await _chat("Old", [("message", "user", "lantern", 1)], archived=True)
    assert await _search("lantern") == []


async def test_query_without_word_characters_finds_nothing():
    await _chat("Any", [("message", "user", "a & b | c", 1)])
    assert await _search("&|") == []


# ── grouping and snippet ──────────────────────────────────────────────────


async def test_one_hit_per_chat_with_the_best_event():
    """A long chat must not fill every place: one row per chat, pointing at
    its best event (exact beats a newer prefix match), plus how many matched."""
    c = await _chat("Long", [
        ("message", "user", "the harbour is busy", 1),
        ("message", "assistant", "harbourmaster says hello", 2),
        ("message", "user", "harbourside cafe", 3),
    ])
    other = await _chat("Short", [("message", "user", "harbour light", 0)])
    hits = await _search("harbour")
    assert [h["chatId"] for h in hits] == [c["chat"], other["chat"]]
    assert hits[0]["eventId"] == c["events"][0]
    assert hits[0]["match"] == "exact"
    assert hits[0]["matches"] == 3


async def test_snippet_sits_around_the_match_and_marks_it():
    filler = "word " * 60
    text = f"{filler}here the Compass points north {filler}"
    await _chat("Map", [("message", "assistant", text, 1)])
    hit = (await _search("compass"))[0]
    snippet = hit["snippet"]
    assert "Compass points north" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= chat_record_search.SNIPPET_CHARS + 2
    start, end = hit["highlight"]
    assert snippet[start:end] == "Compass"


async def test_snippet_of_a_short_message_is_the_message():
    await _chat("Tiny", [("message", "user", "  find   the\n\nrope ", 1)])
    hit = (await _search("rope"))[0]
    assert hit["snippet"] == "find the rope"
    assert hit["snippet"][slice(*hit["highlight"])] == "rope"


# ── ranking ───────────────────────────────────────────────────────────────


async def test_rank_exact_before_prefix_before_substring_then_newest():
    """Stable order: match quality first, recency only inside one quality."""
    exact_old = await _chat("exact old", [("message", "user", "the river", 1)])
    exact_new = await _chat("exact new", [("message", "user", "a river bend", 5)])
    prefix_new = await _chat("prefix", [("message", "user", "riverside walk", 9)])
    sub_newest = await _chat("substring", [("message", "user", "the outriver path", 20)])
    hits = await _search("river")
    order = [h["chatId"] for h in hits]
    if POSTGRES_LANE:  # the word index finds word starts only, never inside a word
        assert order == [exact_new["chat"], exact_old["chat"], prefix_new["chat"]]
        assert [h["match"] for h in hits] == ["exact", "exact", "prefix"]
    else:
        assert order == [exact_new["chat"], exact_old["chat"], prefix_new["chat"], sub_newest["chat"]]
        assert [h["match"] for h in hits] == ["exact", "exact", "prefix", "substring"]


async def test_limit_counts_chats_not_events():
    for i in range(4):
        await _chat(f"C{i}", [("message", "user", f"meadow {j}", i * 10 + j) for j in range(3)])
    hits = await _search("meadow", limit=2)
    assert [h["chatTitle"] for h in hits] == ["C3", "C2"]


# ── filters and visibility ────────────────────────────────────────────────


async def test_agent_filter(make_agent):
    a = await make_agent(name="Alpha", slug="alpha")
    b = await make_agent(name="Beta", slug="beta")
    ca = await _chat("A", [("message", "user", "orchard", 1)], agent_id=a.id)
    await _chat("B", [("message", "user", "orchard", 2)], agent_id=b.id)
    hits = await _search("orchard", agent_id=a.id)
    assert [h["chatId"] for h in hits] == [ca["chat"]]
    assert hits[0]["agentId"] == str(a.id)


async def test_chats_owned_by_someone_else_stay_hidden():
    from app.models.user import User

    other, me = uuid.uuid4(), uuid.uuid4()
    async with AsyncSession(test_engine) as s:
        s.add(User(id=other, email="o@mc.local", name="O", role="operator", is_active=True))
        s.add(User(id=me, email="m@mc.local", name="M", role="operator", is_active=True))
        await s.commit()
    await _chat("Theirs", [("message", "user", "pebble", 1)], owner=other)
    mine = await _chat("Mine", [("message", "user", "pebble", 2)], owner=me)
    shared = await _chat("Shared", [("message", "user", "pebble", 3)])
    user = type("U", (), {"role": "operator", "id": me})()
    hits = await _search("pebble", user=user)
    assert {h["chatId"] for h in hits} == {mine["chat"], shared["chat"]}


# ── the route ─────────────────────────────────────────────────────────────


async def test_route_is_flag_gated(auth_client):
    await _chat("Off", [("message", "user", "lighthouse", 1)])
    r = await auth_client.get("/api/v1/chat-record/search?q=lighthouse")
    assert r.status_code == 200
    assert r.json() == {"hits": [], "enabled": False}


async def test_route_returns_grouped_hits_and_takes_agent_id(flag_on, auth_client, make_agent):
    a = await make_agent(name="Alpha", slug="alpha")
    c = await _chat("Trip", [
        ("message", "user", "book the lighthouse tour", 1),
        ("message", "assistant", "lighthouse tour booked", 2),
    ], agent_id=a.id)
    await _chat("Else", [("message", "user", "lighthouse", 3)])
    r = await auth_client.get("/api/v1/chat-record/search?q=lightho")
    body = r.json()
    assert body["enabled"] is True
    assert len(body["hits"]) == 2
    r = await auth_client.get(f"/api/v1/chat-record/search?q=lightho&agentId={a.id}")
    hits = r.json()["hits"]
    assert [h["chatId"] for h in hits] == [c["chat"]]
    assert hits[0]["eventId"] == c["events"][1]  # same quality: the newer one
    assert set(hits[0]) >= {"chatId", "chatTitle", "agentId", "eventId", "kind", "role",
                            "ts", "snippet", "highlight", "match", "matches"}


# ── Postgres lane: the real word index ────────────────────────────────────


@pytest.mark.postgres
async def test_pg_prefix_uses_the_word_index():
    await _chat("Pg", [("message", "user", "Deployment of the Übersicht page", 1)])
    assert [h["chatTitle"] for h in await _search("deplo")] == ["Pg"]
    assert [h["chatTitle"] for h in await _search("übers")] == ["Pg"]
    assert [h["chatTitle"] for h in await _search("deplo page")] == ["Pg"]
    assert await _search("eploy") == []  # inside a word: not a word start
    # tsquery syntax in the input is plain text, never an error
    assert await _search("deplo:* & !(") != []
