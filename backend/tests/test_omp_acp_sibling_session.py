"""P3 live finding 13.10.2026 — the chat must follow the BRIDGE file.

Under OMP_DRIVER=acp one session has two files in the same folder: omp's own
(``<ms>Z_<sid>.jsonl``, created first, written last) and the bridge sink's
(``<s>_<sid>.jsonl``, header ``"bridge": "acp"``). Only the bridge file
carries what MC adds (question cards, chat errors, mode lines). Live after
``/new``: the tailer rolled onto omp's file, the card arrived only after a
reload and the open chat read "ended". Layout and header shapes below are
copied from the live folder (names/ids made up).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.services import omp_chat

OLD = "01a134a8-0000-7000-8000-000000000001"
NEW = "01a134b7-0000-7000-8000-000000000002"


def _omp():
    from app.services.transcript_adapters import adapter_for

    return adapter_for(SimpleNamespace(harness="omp"))


def _native(folder: Path, stamp: str, sid: str, *entries_ts: str) -> Path:
    path = folder / f"{stamp}Z_{sid}.jsonl"
    lines = [
        {"type": "title", "v": 1, "title": "", "updatedAt": "2026-10-13T10:02:56.297Z"},
        {"type": "session", "version": 3, "id": sid, "timestamp": "2026-10-13T10:02:56.297Z",
         "cwd": "/workspace"},
    ]
    for i, ts in enumerate(entries_ts):
        lines.append({"type": "message", "id": f"n{i}", "parentId": None, "timestamp": ts,
                      "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}})
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return path


def _bridge(folder: Path, stamp: str, sid: str, header_ts: str) -> Path:
    path = folder / f"{stamp}_{sid}.jsonl"
    header = {"type": "session", "version": 3, "id": sid, "timestamp": header_ts,
              "cwd": "", "bridge": "acp"}
    path.write_text(json.dumps(header) + "\n")
    return path


CARD = {
    "type": "custom_message", "customType": "acp-elicitation", "content": "Tee oder Kaffee?",
    "display": True, "attribution": "agent", "id": "acp0abc1", "parentId": None,
    "timestamp": "2026-10-13T10:03:10.000Z",
    "data": {"elicitationId": "el-0123456789ab", "message": "Tee oder Kaffee?",
             "schema": {"type": "object", "properties": {
                 "q0": {"type": "string", "oneOf": [{"const": "Tee", "title": "Tee"}]}}}},
}


def test_active_session_is_the_bridge_file_even_when_omps_file_is_newer(tmp_path):
    folder = tmp_path / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True)
    bridge = _bridge(folder, "2026-10-13T10-02-56", NEW, "2026-10-13T10:02:56.380Z")
    _native(folder, "2026-10-13T10-02-56-297", NEW, "2026-10-13T10:05:00.000Z")
    active = omp_chat.find_active_session(tmp_path / "omp-sessions")
    assert active is not None and active[0] == bridge


def test_a_session_without_bridge_file_stays_as_it_was(tmp_path):
    folder = tmp_path / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True)
    native = _native(folder, "2026-10-13T10-02-56-297", NEW, "2026-10-13T10:05:00.000Z")
    assert omp_chat.find_active_session(tmp_path / "omp-sessions")[0] == native


def test_tailer_moves_once_from_omps_file_to_the_bridge_file(tmp_path):
    from app.services import transcript_chat as tc

    folder = tmp_path / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True)
    bridge = _bridge(folder, "2026-10-13T10-02-56", NEW, "2026-10-13T10:02:56.380Z")
    native = _native(folder, "2026-10-13T10-02-56-297", NEW)
    omp = _omp()
    assert tc.ChatTailerManager._is_genuine_rollover(omp, bridge, native) is True
    assert tc.ChatTailerManager._is_genuine_rollover(omp, native, bridge) is False


async def test_open_chat_is_not_ended_while_omps_sibling_is_newest(tmp_path, monkeypatch):
    import os

    from app.services import transcript_chat as tc

    folder = tmp_path / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True)
    bridge = _bridge(folder, "2026-10-13T10-02-56", NEW, "2026-10-13T10:02:56.380Z")
    native = _native(folder, "2026-10-13T10-02-56-297", NEW, "2026-10-13T10:05:00.000Z")
    old = 1_000_000_000
    os.utime(bridge, (old, old))
    os.utime(native, (old + 10, old + 10))

    async def _alive(agent, name):
        return True

    monkeypatch.setattr(tc, "process_alive", _alive)
    agent = SimpleNamespace(harness="omp", agent_runtime="cli-bridge", slug="acp-one")
    # A page that still holds omp's file must not read "ended" either.
    assert await tc.resolve_aliveness(agent, native, _omp()) == "idle"
    assert await tc.resolve_aliveness(agent, bridge, _omp()) == "idle"


async def test_after_new_the_card_reaches_the_open_chat_live(tmp_path, monkeypatch):
    """The /new sequence as seen live: omp creates its file first, the
    bridge's a moment later, omp keeps writing its own — and the question
    card lands only in the bridge file. It must reach the open chat."""
    import app.services.transcript_chat as tc
    from app.services import transcript_adapters

    folder = tmp_path / "omp-sessions" / "--workspace--"
    folder.mkdir(parents=True)
    old_bridge = _bridge(folder, "2026-10-13T09-46-42", OLD, "2026-10-13T09:46:42.000Z")
    _native(folder, "2026-10-13T09-46-42-695", OLD, "2026-10-13T09:50:00.000Z")

    omp = _omp()
    monkeypatch.setattr(transcript_adapters, "adapter_for", lambda agent: omp)
    published: list[dict] = []

    async def _fake(channel, event_type, data):
        published.append(json.loads(json.dumps(data)))

    monkeypatch.setattr(tc.sse, "broadcast", _fake)
    m = tc.ChatTailerManager()
    m.POLL_INTERVAL = 0.02
    m.STATE_PROBE_INTERVAL_SECONDS = 0.02
    await m.acquire("a", old_bridge)
    try:
        await asyncio.sleep(0.1)
        native = _native(folder, "2026-10-13T10-02-56-297", NEW)
        await asyncio.sleep(0.1)
        bridge = _bridge(folder, "2026-10-13T10-02-56", NEW, "2026-10-13T10:02:56.380Z")
        await asyncio.sleep(0.1)
        with native.open("a") as fh:  # omp keeps writing ITS file, newer stamps
            fh.write(json.dumps({"type": "message", "id": "n9", "parentId": None,
                                 "timestamp": "2026-10-13T10:04:00.000Z",
                                 "message": {"role": "user", "content": [
                                     {"type": "text", "text": "hi"}]}}) + "\n")
        await asyncio.sleep(0.1)
        with bridge.open("a") as fh:
            fh.write(json.dumps(CARD) + "\n")
        for _ in range(150):
            if any(d.get("kind") == "elicitation" for d in published):
                break
            await asyncio.sleep(0.02)
    finally:
        await m.release("a")
    assert any(d.get("kind") == "session_changed" for d in published)
    cards = [d for d in published if d.get("kind") == "elicitation"]
    assert [c["elicitationId"] for c in cards] == ["el-0123456789ab"]
