"""Gruppen-Vorschau: Live-Tipp-Vorschau eines Mitglieds im Gruppenraum.

Der Sessions-Chat zeigt schon, was ein Agent gerade im Terminal schreibt
(``chat_event`` mit ``kind="preview"``, siehe ``transcript_chat``). Der
Gruppenraum verwendet genau diesen Strom wieder: Der Gruppen-SSE abonniert
zusaetzlich die Chat-Kanaele seiner Mitglieder und uebersetzt deren Frames
hier in ``group.preview {agent_id, text, ts}``. Leerer ``text`` = Vorschau
loeschen (fertige Antwort, Agent wieder idle, neue Sitzung).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from app.redis_client import RedisKeys

GROUP_PREVIEW_EVENT = "group.preview"


def _clear(agent_id: str, frame_id: str | None) -> dict:
    return {
        "id": frame_id,
        "event": GROUP_PREVIEW_EVENT,
        "data": {"agent_id": agent_id, "text": "", "ts": None},
    }


def member_chat_event_to_group_event(agent_id: str, payload: dict) -> dict | None:
    """Ein ``chat_event``-Frame eines Mitglieds → ``group.preview`` oder None."""
    if payload.get("event") != "chat_event":
        return None
    data = payload.get("data") or {}
    kind = data.get("kind")
    frame_id = payload.get("id")
    if kind == "preview":
        return {
            "id": frame_id,
            "event": GROUP_PREVIEW_EVENT,
            "data": {"agent_id": agent_id, "text": data.get("text", ""), "ts": data.get("ts")},
        }
    if kind == "message" and data.get("role") == "assistant":
        return _clear(agent_id, frame_id)
    if kind == "state" and data.get("status") == "idle":
        return _clear(agent_id, frame_id)
    if kind == "session_changed":
        return _clear(agent_id, frame_id)
    return None


def build_group_transform(group_channel: str, member_ids: list[str]):
    """Uebersetzer fuer ``_sse_generator``: Gruppenkanal 1:1 durchreichen,
    Mitglieder-Chatkanaele in ``group.preview`` umschreiben, Rest verwerfen."""
    by_channel = {RedisKeys.agent_chat_channel(a): a for a in member_ids}

    def transform(channel: str, payload: dict) -> dict | None:
        if channel == group_channel:
            return payload
        agent_id = by_channel.get(channel)
        if agent_id is None:
            return None
        return member_chat_event_to_group_event(agent_id, payload)

    return transform


async def group_stream_frames(
    group_id: str,
    sources: list[tuple[Any, Path]],
    manager: Any | None = None,
) -> AsyncGenerator[dict, None]:
    """SSE-Frames eines Gruppenraums inklusive Mitglieder-Vorschau.

    ``sources`` = (Agent, Transkriptpfad) je Mitglied, das eine Live-Sitzung
    hat. Fuer jedes wird der Chat-Tailer gemietet (startet Pane-Stream +
    Vorschau, wenn noch kein Sessions-Chat-Tab offen ist) und beim Trennen
    wieder freigegeben — sonst liefe ``tmux pipe-pane`` ewig weiter.
    """
    from app.services.sse import _sse_generator
    from app.services.transcript_chat import tailer_manager as default_manager

    mgr = manager if manager is not None else default_manager
    group_channel = RedisKeys.group_events(group_id)
    acquired: list[str] = []
    try:
        for agent, path in sources:
            agent_id = str(agent.id)
            await mgr.acquire(agent_id, path, agent)
            acquired.append(agent_id)
        channels = [group_channel] + [RedisKeys.agent_chat_channel(a) for a in acquired]
        transform = build_group_transform(group_channel, acquired)
        async for frame in _sse_generator(channels, transform=transform):
            yield frame
    finally:
        for agent_id in acquired:
            await mgr.release(agent_id)
