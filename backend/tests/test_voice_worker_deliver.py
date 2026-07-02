"""Tests for the Voice Concierge `deliver_to_telegram` function_tool flow.

The voice_worker is a separate Python package — we test the routing logic
by importing the VoiceAssistant class directly and calling the wrapped
underlying coroutine. Each test mocks mc_client so we never hit the network.

Three branches covered:
- 1 hit → forward to vault_deliver_to_telegram with that path.
- 0 hits → return nothing_found, suggest research.
- >1 hits → return ambiguous + candidates list.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# Make the voice_worker package importable in the backend test env. The
# repo layout has voice_worker/ at the top level, sibling to backend/.
VOICE_DIR = Path(__file__).resolve().parents[2] / "voice_worker"
if str(VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_DIR))


def _import_assistant():
    """Lazy import — livekit + xAI deps might not be installed in CI; skip
    cleanly if they're absent."""
    try:
        import main as voice_main  # type: ignore
    except ImportError as exc:
        pytest.skip(f"voice_worker deps not installed: {exc}")
    return voice_main


@pytest.mark.asyncio
async def test_deliver_single_hit_forwards(monkeypatch):
    voice = _import_assistant()
    assistant = voice.VoiceAssistant.__new__(voice.VoiceAssistant)

    fake_search = AsyncMock(return_value={
        "hits": [{
            "path": "agents/researcher/deliverables/wetter-x.md",
            "title": "Wetterbericht",
            "type": "deliverable",
            "agent": "researcher",
        }],
    })
    fake_deliver = AsyncMock(return_value={
        "ok": True, "telegram_message_id": 7777,
        "size": 1234, "caption": "Wetterbericht",
    })
    monkeypatch.setattr(voice.mc_client, "vault_search", fake_search)
    monkeypatch.setattr(voice.mc_client, "vault_deliver_to_telegram", fake_deliver)

    # function_tool wraps the function in a FunctionTool object; the original
    # coroutine sits on `.callable` (livekit) or `.fn` depending on lib version.
    tool = voice.VoiceAssistant.deliver_to_telegram
    inner = getattr(tool, "callable", None) or getattr(tool, "fn", None) or tool
    res = await inner(assistant, query="wetter")

    assert res["ok"] is True
    assert res["telegram_message_id"] == 7777
    fake_search.assert_awaited_once()
    fake_deliver.assert_awaited_once_with(
        "agents/researcher/deliverables/wetter-x.md", caption=None
    )


@pytest.mark.asyncio
async def test_deliver_zero_hits_returns_nothing_found(monkeypatch):
    voice = _import_assistant()
    assistant = voice.VoiceAssistant.__new__(voice.VoiceAssistant)

    fake_search = AsyncMock(return_value={"hits": []})
    fake_deliver = AsyncMock()
    monkeypatch.setattr(voice.mc_client, "vault_search", fake_search)
    monkeypatch.setattr(voice.mc_client, "vault_deliver_to_telegram", fake_deliver)

    tool = voice.VoiceAssistant.deliver_to_telegram
    inner = getattr(tool, "callable", None) or getattr(tool, "fn", None) or tool
    res = await inner(assistant, query="nicht_vorhanden")

    assert res["ok"] is False
    assert res["reason"] == "nothing_found"
    assert res["suggest_research"] is True
    fake_deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_multiple_hits_returns_ambiguous(monkeypatch):
    voice = _import_assistant()
    assistant = voice.VoiceAssistant.__new__(voice.VoiceAssistant)

    fake_search = AsyncMock(return_value={
        "hits": [
            {"path": "agents/r/deliverables/a.md", "title": "Wetter A",
             "type": "deliverable", "agent": "researcher"},
            {"path": "agents/r/deliverables/b.md", "title": "Wetter B",
             "type": "deliverable", "agent": "researcher"},
            {"path": "agents/r/deliverables/c.md", "title": "Wetter C",
             "type": "deliverable", "agent": "researcher"},
        ],
    })
    fake_deliver = AsyncMock()
    monkeypatch.setattr(voice.mc_client, "vault_search", fake_search)
    monkeypatch.setattr(voice.mc_client, "vault_deliver_to_telegram", fake_deliver)

    tool = voice.VoiceAssistant.deliver_to_telegram
    inner = getattr(tool, "callable", None) or getattr(tool, "fn", None) or tool
    res = await inner(assistant, query="wetter")

    assert res["ok"] is False
    assert res["reason"] == "ambiguous"
    assert len(res["candidates"]) == 3
    titles = {c["title"] for c in res["candidates"]}
    assert titles == {"Wetter A", "Wetter B", "Wetter C"}
    fake_deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_force_path_skips_search(monkeypatch):
    """When the operator says 'die zweite, schick los', Voice passes force_path
    directly and we MUST skip the search to avoid re-disambiguation."""
    voice = _import_assistant()
    assistant = voice.VoiceAssistant.__new__(voice.VoiceAssistant)

    fake_search = AsyncMock(return_value={"hits": []})  # would say nothing_found
    fake_deliver = AsyncMock(return_value={"ok": True, "telegram_message_id": 1})
    monkeypatch.setattr(voice.mc_client, "vault_search", fake_search)
    monkeypatch.setattr(voice.mc_client, "vault_deliver_to_telegram", fake_deliver)

    tool = voice.VoiceAssistant.deliver_to_telegram
    inner = getattr(tool, "callable", None) or getattr(tool, "fn", None) or tool
    res = await inner(
        assistant,
        query="ignored",
        force_path="agents/r/deliverables/explicit.md",
        caption="Hier ist die Datei",
    )

    assert res["ok"] is True
    fake_search.assert_not_awaited()
    fake_deliver.assert_awaited_once_with(
        "agents/r/deliverables/explicit.md",
        caption="Hier ist die Datei",
    )
