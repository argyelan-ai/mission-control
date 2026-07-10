"""Tests for jarvis_core.tools + persona (ADR-061).

Covers the provider-neutral tool dispatch, per-channel availability, and the
voice-vs-telegram degradation of the show_*/highlight_graph handlers. No network
— the mc_client is a mock.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jarvis_core import tools as jtools
from jarvis_core.channels import TELEGRAM, VOICE
from jarvis_core.persona import build_instructions


# ── Persona composition ──────────────────────────────────────────────────


def test_persona_voice_has_voice_addendum():
    text = build_instructions(VOICE)
    assert "Jarvis" in text
    assert "VOICE-DRAWER" in text  # voice-only card guidance heading
    assert "KANAL — TEXT (TELEGRAM)" not in text  # telegram addendum must not leak


def test_persona_telegram_has_telegram_addendum():
    text = build_instructions(TELEGRAM)
    assert "Telegram" in text
    assert "kein display" in text.lower()
    assert "VOICE-DRAWER" not in text  # voice-only guidance must NOT leak in


def test_persona_includes_briefing_ctx():
    text = build_instructions(TELEGRAM, briefing_ctx="- Offen: 3 Tasks")
    assert "Pre-Session Briefing" in text
    assert "3 Tasks" in text


def test_persona_core_has_honesty_rules():
    """PERSONA_CORE mandates naming the age of results and never presenting
    stale content as current (V1.5 briefing hygiene)."""
    from jarvis_core.persona import PERSONA_CORE

    assert "EHRLICHKEIT" in PERSONA_CORE
    assert "duplicate_count" in PERSONA_CORE or "im Board" in PERSONA_CORE
    assert "staleness_summary" in PERSONA_CORE or "Aktualitaet" in PERSONA_CORE


# ── Briefing formatter ───────────────────────────────────────────────────


def test_format_briefing_includes_age_suffix_for_tasks_lessons_writes():
    briefing = {
        "current_time_of_day_de": "abends",
        "open_approvals_count": 1,
        "agents_online": 2,
        "agents_offline": 1,
        "open_tasks": [
            {"title": "Fix bug", "status": "in_progress", "assigned_to": "Sparky", "age_days": 3, "duplicate_count": 1},
        ],
        "recent_lessons": [
            {"title": "Lesson A", "agent": "sparky", "age_days": 0},
        ],
        "recent_writes": [
            {"path": "agents/sparky/foo.md", "agent": "sparky", "age_days": 55},
        ],
        "staleness_summary": {"newest_write_age_days": 55, "note": "no writes in last 7 days"},
    }
    text = jtools.format_briefing_as_context(briefing)
    assert "(vor 3 Tagen)" in text
    assert "(heute)" in text
    assert "(vor 55 Tagen)" in text
    assert "no writes in last 7 days" in text


def test_format_briefing_marks_duplicate_tasks():
    briefing = {
        "open_tasks": [
            {"title": "Post-launch retro board", "status": "inbox", "assigned_to": None, "age_days": 1, "duplicate_count": 3},
        ],
    }
    text = jtools.format_briefing_as_context(briefing)
    assert "[3x im Board]" in text


def test_format_briefing_unknown_age_says_so():
    briefing = {
        "open_tasks": [
            {"title": "Mystery task", "status": "inbox", "assigned_to": None, "age_days": None, "duplicate_count": 1},
        ],
    }
    text = jtools.format_briefing_as_context(briefing)
    assert "Datum unbekannt" in text


# ── Tool availability per channel ────────────────────────────────────────


def test_highlight_graph_voice_only():
    voice_names = {t.name for t in jtools.tools_for(VOICE)}
    tg_names = {t.name for t in jtools.tools_for(TELEGRAM)}
    assert "highlight_graph" in voice_names
    assert "highlight_graph" not in tg_names


def test_openai_schemas_shape():
    schemas = jtools.openai_tool_schemas(TELEGRAM)
    assert all(s["type"] == "function" for s in schemas)
    names = {s["function"]["name"] for s in schemas}
    assert "create_task" in names
    assert "highlight_graph" not in names  # filtered out for telegram


# ── Dispatch ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_create_task_forwards_to_client():
    client = AsyncMock()
    client.create_task = AsyncMock(return_value={"ok": True, "task_id": "t1"})
    res = await jtools.dispatch(
        "create_task", client, VOICE, {"title": "Deploy", "priority": "high"}
    )
    assert res == {"ok": True, "task_id": "t1"}
    client.create_task.assert_awaited_once_with("Deploy", "", None, "high")


@pytest.mark.asyncio
async def test_dispatch_to_agent_forwards_to_client():
    client = AsyncMock()
    client.dispatch_to_agent = AsyncMock(
        return_value={"ok": True, "task_id": "t9", "agent": "Cody",
                      "dispatch_status": "dispatched"}
    )
    res = await jtools.dispatch(
        "dispatch_to_agent", client, VOICE,
        {"agent_name": "Cody", "instruction": "Baue Feature X", "priority": "high"},
    )
    assert res["dispatch_status"] == "dispatched"
    client.dispatch_to_agent.assert_awaited_once_with("Cody", "Baue Feature X", "high")


@pytest.mark.asyncio
async def test_dispatch_to_agent_available_on_both_channels():
    voice_names = {t.name for t in jtools.tools_for(VOICE)}
    tg_names = {t.name for t in jtools.tools_for(TELEGRAM)}
    assert "dispatch_to_agent" in voice_names
    assert "dispatch_to_agent" in tg_names


@pytest.mark.asyncio
async def test_ask_frontier_tool_delegates_to_frontier(monkeypatch):
    from jarvis_core import frontier

    called: dict = {}

    async def _fake_ask(question, context_hint=None):
        called["question"] = question
        called["hint"] = context_hint
        return {"ok": True, "answer": "42", "model": "gpt-test"}

    monkeypatch.setattr(frontier, "ask_frontier", _fake_ask)
    res = await jtools.dispatch(
        "ask_frontier", AsyncMock(), TELEGRAM,
        {"question": "Sinn des Lebens?", "context_hint": "philosophisch"},
    )
    assert res == {"ok": True, "answer": "42", "model": "gpt-test"}
    assert called == {"question": "Sinn des Lebens?", "hint": "philosophisch"}


@pytest.mark.asyncio
async def test_ask_frontier_tool_available_on_both_channels():
    voice_names = {t.name for t in jtools.tools_for(VOICE)}
    tg_names = {t.name for t in jtools.tools_for(TELEGRAM)}
    assert "ask_frontier" in voice_names
    assert "ask_frontier" in tg_names


@pytest.mark.asyncio
async def test_dispatch_unknown_tool():
    res = await jtools.dispatch("does_not_exist", AsyncMock(), VOICE, {})
    assert res["ok"] is False
    assert "Unbekanntes Tool" in res["error"]


@pytest.mark.asyncio
async def test_dispatch_highlight_graph_unavailable_on_telegram():
    res = await jtools.dispatch("highlight_graph", AsyncMock(), TELEGRAM, {"type": "lesson"})
    assert res["ok"] is False
    assert res["reason"] == "unavailable_on_channel"


# ── Channel degradation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_url_voice_pushes_card():
    client = AsyncMock()
    client.voice_display = AsyncMock(return_value={"ok": True})
    res = await jtools.dispatch("show_url", client, VOICE, {"url": "https://x.io/a"})
    assert res == {"ok": True}
    client.voice_display.assert_awaited_once()
    assert client.voice_display.await_args.kwargs["kind"] == "url"


@pytest.mark.asyncio
async def test_show_url_telegram_returns_link_text():
    client = AsyncMock()
    client.voice_display = AsyncMock()
    res = await jtools.dispatch("show_url", client, TELEGRAM, {"url": "https://x.io/a"})
    assert res["ok"] is True and res["degraded"] is True
    assert res["url"] == "https://x.io/a"
    client.voice_display.assert_not_awaited()  # no card push on telegram


@pytest.mark.asyncio
async def test_show_memory_telegram_returns_snippet_not_card():
    client = AsyncMock()
    client.vault_search = AsyncMock(return_value={"hits": [
        {"path": "a/b.md", "title": "Rate-Limit Decision", "type": "decision",
         "content": "Wir haben X entschieden."},
    ]})
    client.voice_display = AsyncMock()
    res = await jtools.dispatch("show_memory", client, TELEGRAM, {"query": "rate limit"})
    assert res["ok"] is True and res["degraded"] is True
    assert res["title"] == "Rate-Limit Decision"
    assert "entschieden" in res["snippet"]
    client.voice_display.assert_not_awaited()


@pytest.mark.asyncio
async def test_highlight_graph_desk_only_message():
    # Direct handler call (bypasses dispatch availability gate) still degrades.
    res = await jtools.BY_NAME["highlight_graph"].handler(
        AsyncMock(), TELEGRAM, type="lesson"
    )
    assert res["ok"] is False
    assert res["reason"] == "desk_only"


@pytest.mark.asyncio
async def test_deliver_ambiguous_multiple_hits():
    client = AsyncMock()
    client.vault_search = AsyncMock(return_value={"hits": [
        {"path": "a.md", "title": "A"}, {"path": "b.md", "title": "B"},
    ]})
    res = await jtools.dispatch("deliver_to_telegram", client, TELEGRAM, {"query": "x"})
    assert res["ok"] is False and res["reason"] == "ambiguous"
    assert len(res["candidates"]) == 2
