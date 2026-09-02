"""Gruppen-Vorschau: Chat-Events eines Mitglieds werden zu ``group.preview``.

Der Gruppenraum zeigt, was ein Mitglied gerade im Terminal tippt, BEVOR
seine Nachricht gepostet ist. Quelle ist derselbe ``preview``-Strom wie im
Sessions-Chat (Live-Schicht A); hier wird er nur mit der Agent-ID gestempelt.
"""
from __future__ import annotations

from app.services.group_preview import member_chat_event_to_group_event

AGENT = "11111111-1111-1111-1111-111111111111"


def _frame(kind: str, **data) -> dict:
    return {"id": "f1", "event": "chat_event", "data": {"kind": kind, **data}}


def test_preview_is_stamped_with_the_agent_id():
    out = member_chat_event_to_group_event(AGENT, _frame("preview", text="Ich denke, dass", ts="T"))
    assert out == {
        "id": "f1",
        "event": "group.preview",
        "data": {"agent_id": AGENT, "text": "Ich denke, dass", "ts": "T"},
    }


def test_finished_assistant_message_clears_the_preview():
    out = member_chat_event_to_group_event(AGENT, _frame("message", role="assistant", text="fertig"))
    assert out["event"] == "group.preview"
    assert out["data"] == {"agent_id": AGENT, "text": "", "ts": None}


def test_idle_state_and_session_change_clear_the_preview():
    idle = member_chat_event_to_group_event(AGENT, _frame("state", status="idle"))
    fresh = member_chat_event_to_group_event(AGENT, _frame("session_changed"))
    assert idle["data"]["text"] == "" and fresh["data"]["text"] == ""


def test_other_chat_events_are_dropped():
    # Operator-Nachrichten, „arbeitet"-Zustand, Fremdes: nichts davon gehoert in den Raum.
    assert member_chat_event_to_group_event(AGENT, _frame("message", role="user", text="hi")) is None
    assert member_chat_event_to_group_event(AGENT, _frame("state", status="working")) is None
    assert member_chat_event_to_group_event(AGENT, {"event": "ping", "data": {}}) is None


# --- Strom-Helfer: Tailer der Mitglieder mieten und wieder freigeben ---------

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.redis_client import RedisKeys
from app.services.group_preview import group_stream_frames
from tests.test_sse_generator import fake_pubsub  # noqa: F401  (Fixture)

GROUP = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


class _FakeManager:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, agent_id: str, path: Path, agent=None) -> None:
        self.acquired.append(agent_id)

    async def release(self, agent_id: str) -> None:
        self.released.append(agent_id)


@pytest.mark.asyncio
async def test_group_stream_subscribes_members_and_releases_them(fake_pubsub):  # noqa: F811
    manager = _FakeManager()
    sources = [(SimpleNamespace(id=AGENT), Path("/tmp/a.jsonl"))]
    gen = group_stream_frames(GROUP, sources, manager=manager)

    await fake_pubsub.queue.put(
        (
            RedisKeys.agent_chat_channel(AGENT),
            json.dumps({"id": "p", "event": "chat_event", "data": {"kind": "preview", "text": "Hallo", "ts": "T"}}),
        )
    )
    first = await asyncio.wait_for(gen.__anext__(), timeout=2)
    assert first["event"] == "group.preview"
    assert json.loads(first["data"]) == {"agent_id": AGENT, "text": "Hallo", "ts": "T"}

    # Gruppenkanal + Chat-Kanal jedes Mitglieds mit Transkript
    assert set(fake_pubsub.subscribed) == {
        RedisKeys.group_events(GROUP),
        RedisKeys.agent_chat_channel(AGENT),
    }
    assert manager.acquired == [AGENT]
    assert manager.released == []
    await gen.aclose()
    # Sabotage-Gegenprobe: ohne release bliebe der Pane-Tailer ewig an.
    assert manager.released == [AGENT]


@pytest.mark.asyncio
async def test_group_stream_passes_group_events_through_and_ignores_foreign_channels(fake_pubsub):  # noqa: F811
    gen = group_stream_frames(GROUP, [], manager=_FakeManager())
    await fake_pubsub.queue.put(
        (RedisKeys.agent_chat_channel(OTHER), json.dumps({"id": "x", "event": "chat_event", "data": {"kind": "preview", "text": "fremd"}}))
    )
    await fake_pubsub.queue.put(
        (RedisKeys.group_events(GROUP), json.dumps({"id": "g", "event": "group.turn_started", "data": {"speaker": AGENT}}))
    )
    first = await asyncio.wait_for(gen.__anext__(), timeout=2)
    await gen.aclose()
    assert first["event"] == "group.turn_started"
