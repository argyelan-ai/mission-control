"""read_briefing liest den echten Morgenbriefing-Task statt eines Aggregats.

Bug #3 der Welle A+B: auf "Morgenbriefing" rief Jarvis briefing() auf — ein
Status-Aggregat aus dem Vault. Das echte Briefing ist ein ~19k Zeichen langes
Deliverable an einem abgeschlossenen Researcher-Task.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "jarvis_tools_brief", _ROOT / "jarvis_core" / "tools.py"
)
jtools = importlib.util.module_from_spec(_spec)
sys.modules["jarvis_tools_brief"] = jtools
_spec.loader.exec_module(jtools)

from jarvis_core.channels import VOICE

_LONG = "Zeile mit Inhalt. " * 1200  # ~20k Zeichen, wie ein echtes Briefing


class _Client:
    def __init__(self, tasks, deliverables):
        self._tasks = tasks
        self._deliverables = deliverables

    async def list_tasks(self, status=None, limit=10):
        if status in (None, "done"):
            return {"ok": True, "tasks": self._tasks}
        return {"ok": True, "tasks": []}

    async def get_deliverables(self, task_id, include_content=False):
        return {"ok": True, "deliverables": self._deliverables}

    async def voice_display(self, kind, data, title=None):
        return {"ok": True, "kind": kind}


@pytest.mark.asyncio
async def test_summarizes_long_briefing_via_frontier(monkeypatch):
    calls = {}

    async def fake_summarize(text):
        calls["len"] = len(text)
        return {"ok": True, "answer": "Kurzfassung des Briefings."}

    client = _Client(
        tasks=[{"id": "b1", "title": "Morning Briefing - Tech & News Digest",
                "status": "done"}],
        deliverables=[{"title": "Morgenbriefing (Markdown)",
                       "deliverable_type": "document", "content": _LONG}],
    )
    monkeypatch.setattr(jtools, "_summarize_text", fake_summarize, raising=False)

    result = await jtools._read_briefing(client, VOICE)
    assert result["ok"] is True
    assert result["summary"] == "Kurzfassung des Briefings."
    assert len(result["summary"]) < 500
    assert calls["len"] == len(_LONG)


@pytest.mark.asyncio
async def test_falls_back_to_truncated_text_when_frontier_off(monkeypatch):
    async def fake_summarize(text):
        return {"ok": False, "reason": "frontier_disabled"}

    monkeypatch.setattr(jtools, "_summarize_text", fake_summarize, raising=False)

    client = _Client(
        tasks=[{"id": "b1", "title": "Morgenbriefing", "status": "done"}],
        deliverables=[{"title": "MD", "deliverable_type": "document", "content": _LONG}],
    )
    result = await jtools._read_briefing(client, VOICE)

    assert result["ok"] is True
    assert result["degraded"] is True
    assert result["reason"] == "frontier_disabled"
    assert len(result["excerpt"]) <= 2000


@pytest.mark.asyncio
async def test_picks_the_longest_deliverable(monkeypatch):
    """Am selben Task haengt neben dem langen Markdown eine kurze Telegram-Fassung."""
    seen = {}

    async def fake_summarize(text):
        seen["text"] = text
        return {"ok": True, "answer": "Kurzfassung."}

    monkeypatch.setattr(jtools, "_summarize_text", fake_summarize, raising=False)

    client = _Client(
        tasks=[{"id": "b1", "title": "Morning Briefing", "status": "done"}],
        deliverables=[
            {"title": "Telegram Summary", "deliverable_type": "text",
             "content": "Kurz und knapp."},
            {"title": "Morgenbriefing (Markdown)", "deliverable_type": "document",
             "content": _LONG},
        ],
    )
    result = await jtools._read_briefing(client, VOICE)
    assert result["document_title"] == "Morgenbriefing (Markdown)"
    assert seen["text"] == _LONG


@pytest.mark.asyncio
async def test_no_briefing_today_is_honest():
    client = _Client(tasks=[], deliverables=[])
    result = await jtools._read_briefing(client, VOICE)
    assert result["ok"] is False
    assert result["reason"] == "no_briefing_found"


@pytest.mark.asyncio
async def test_task_without_content_is_reported(monkeypatch):
    client = _Client(
        tasks=[{"id": "b1", "title": "Morgenbriefing X-Trends", "status": "done"}],
        deliverables=[{"title": "leer", "deliverable_type": "document", "content": "  "}],
    )
    result = await jtools._read_briefing(client, VOICE)
    assert result["ok"] is False
    assert result["reason"] == "no_content"


@pytest.mark.asyncio
async def test_summarize_text_reports_disabled_frontier(monkeypatch):
    """_summarize_text darf ohne Frontier nicht werfen, sondern strukturiert melden."""
    from jarvis_core import frontier

    monkeypatch.setattr(frontier, "is_tool_enabled", lambda: False)
    out = await jtools._summarize_text("egal")
    assert out == {"ok": False, "reason": "frontier_disabled"}
