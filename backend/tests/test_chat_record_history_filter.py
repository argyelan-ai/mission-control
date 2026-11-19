"""``GET /chat-record/chats/{id}/history?kind=…`` (follow-up of #1049).

The switch cards of a chat must be loadable even when they are older than
the newest N events of the record: the kind filter pages over the matching
rows only, with the same ``before`` paging as the plain history.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services import chat_record
from tests.conftest import test_engine


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_record_enabled", True, raising=False)


T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


async def _seed(make_agent):
    """One chat: 3 switch rows spread between 30 messages and 1 usage row."""
    agent = await make_agent(name="Alpha", slug="alpha")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        chat = await chat_record.ensure_agent_chat(s, agent)
        row = await chat_record.ensure_session(
            s, chat, native_session_id="n-1", harness="claude", runtime_id=None, model=None,
            place="container", transcript_path=None,
        )
        items = []
        for i in range(30):
            ts = (T0 + timedelta(seconds=i)).isoformat()
            items.append((f"m:{i}", {"kind": "message", "uuid": f"m{i}", "ts": ts, "role": "user", "text": f"t{i}"}))
            if i in (2, 5, 20):
                items.append((f"switch:{i}", {"kind": "switch", "uuid": f"s{i}", "ts": ts, "status": "done",
                                               "n": i}))
        items.append(("usage:0", {"kind": "usage", "uuid": "u0", "ts": (T0 + timedelta(minutes=5)).isoformat(),
                                  "inputTokens": 1, "outputTokens": 1}))
        await chat_record.upsert_events(s, chat.id, row.id, items)
        await s.commit()
        return chat.id


async def test_kind_filter_returns_switch_cards_older_than_the_last_page(flag_on, make_agent, auth_client):
    chat_id = await _seed(make_agent)
    url = f"/api/v1/chat-record/chats/{chat_id}/history"

    # Without a filter the newest 10 events hold no switch card at all.
    plain = (await auth_client.get(url, params={"limit": 10})).json()
    assert "switch" not in {e["event"]["kind"] for e in plain["events"]}

    r = await auth_client.get(url, params={"limit": 10, "kind": "switch"})
    assert r.status_code == 200
    body = r.json()
    assert [e["event"]["n"] for e in body["events"]] == [2, 5, 20]  # oldest first
    assert body["hasMore"] is False
    assert body["chat"]["id"] == str(chat_id)


async def test_kind_filter_pages_backwards_inside_the_kind(flag_on, make_agent, auth_client):
    chat_id = await _seed(make_agent)
    url = f"/api/v1/chat-record/chats/{chat_id}/history"
    first = (await auth_client.get(url, params={"limit": 2, "kind": "switch"})).json()
    assert [e["event"]["n"] for e in first["events"]] == [5, 20]
    assert first["hasMore"] is True
    older = (await auth_client.get(
        url, params={"limit": 2, "kind": "switch", "before": first["events"][0]["id"]}
    )).json()
    assert [e["event"]["n"] for e in older["events"]] == [2]
    assert older["hasMore"] is False


async def test_kind_filter_takes_several_kinds(flag_on, make_agent, auth_client):
    chat_id = await _seed(make_agent)
    r = await auth_client.get(
        f"/api/v1/chat-record/chats/{chat_id}/history",
        params=[("kind", "switch"), ("kind", "usage"), ("limit", "50")],
    )
    kinds = [e["event"]["kind"] for e in r.json()["events"]]
    assert kinds == ["switch", "switch", "switch", "usage"]


async def test_kind_filter_rejects_a_malformed_kind(flag_on, make_agent, auth_client):
    chat_id = await _seed(make_agent)
    r = await auth_client.get(f"/api/v1/chat-record/chats/{chat_id}/history", params={"kind": "sw itch;"})
    assert r.status_code == 422


async def test_kind_filter_is_404_while_flag_is_off(auth_client):
    r = await auth_client.get(f"/api/v1/chat-record/chats/{uuid.uuid4()}/history", params={"kind": "switch"})
    assert r.status_code == 404
    assert r.json()["detail"] == {"reason": "chat_record_disabled"}
