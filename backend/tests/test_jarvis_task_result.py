"""get_task_result liefert das Ergebnis eines Tasks, auch wenn er done ist.

Kernpunkt: das eigentliche Arbeitsergebnis liegt in task_deliverables, nicht
im Task selbst. Ohne dieses Tool sah Jarvis nur Titel und Status.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "jarvis_tools_result", _ROOT / "jarvis_core" / "tools.py"
)
jtools = importlib.util.module_from_spec(_spec)
sys.modules["jarvis_tools_result"] = jtools
_spec.loader.exec_module(jtools)

from jarvis_core.channels import VOICE


class _Client:
    def __init__(self, tasks=None, deliverables=None, tasks_by_status=None):
        self._tasks = tasks or []
        self._tasks_by_status = tasks_by_status
        self._deliverables = deliverables or []
        self.calls = []

    async def list_tasks(self, status=None, limit=10):
        self.calls.append(("list_tasks", status))
        if self._tasks_by_status is not None:
            return {"ok": True, "tasks": self._tasks_by_status.get(status, [])}
        return {"ok": True, "tasks": self._tasks}

    async def get_deliverables(self, task_id, include_content=False):
        self.calls.append(("get_deliverables", task_id, include_content))
        return {"ok": True, "deliverables": self._deliverables}


@pytest.mark.asyncio
async def test_finds_done_task_by_title():
    client = _Client(
        tasks=[{"id": "t9", "title": "Morgenbriefing 25. Juli", "status": "done"}],
        deliverables=[{"title": "Briefing MD", "deliverable_type": "document",
                       "content": "Inhalt des Briefings"}],
    )
    result = await jtools._get_task_result(client, VOICE, query="Morgenbriefing")

    assert result["ok"] is True
    assert result["task"]["status"] == "done"
    assert result["deliverables"][0]["title"] == "Briefing MD"
    assert result["deliverables"][0]["content"] == "Inhalt des Briefings"
    # Volltext wird angefordert — sonst waere content leer.
    assert ("get_deliverables", "t9", True) in client.calls


@pytest.mark.asyncio
async def test_searches_done_before_giving_up():
    """Nichts unter offenen Tasks → es wird zusaetzlich unter done gesucht."""
    client = _Client(tasks=[])
    await jtools._get_task_result(client, VOICE, query="Irgendwas")
    statuses = [c[1] for c in client.calls if c[0] == "list_tasks"]
    assert "done" in statuses


@pytest.mark.asyncio
async def test_open_task_wins_over_done():
    """Offene Tasks werden zuerst durchsucht — 'das Briefing' meint das aktuelle."""
    client = _Client(
        tasks_by_status={
            None: [{"id": "open1", "title": "Briefing heute", "status": "in_progress"}],
            "done": [{"id": "old1", "title": "Briefing gestern", "status": "done"}],
        }
    )
    result = await jtools._get_task_result(client, VOICE, query="Briefing")
    assert result["task"]["id"] == "open1"
    assert "done" not in [c[1] for c in client.calls if c[0] == "list_tasks"]


@pytest.mark.asyncio
async def test_nothing_found_is_structured_not_raised():
    client = _Client(tasks=[])
    result = await jtools._get_task_result(client, VOICE, query="Existiert nicht")
    assert result["ok"] is False
    assert result["reason"] == "nothing_found"


@pytest.mark.asyncio
async def test_task_without_deliverables_says_so():
    client = _Client(
        tasks=[{"id": "t1", "title": "Leer", "status": "done"}],
        deliverables=[],
    )
    result = await jtools._get_task_result(client, VOICE, query="Leer")
    assert result["ok"] is True
    assert result["reason"] == "no_deliverables"


@pytest.mark.asyncio
async def test_content_is_truncated_for_realtime_context():
    client = _Client(
        tasks=[{"id": "t2", "title": "Riesig", "status": "done"}],
        deliverables=[{"title": "Gross", "deliverable_type": "document",
                       "content": "x" * 10000}],
    )
    result = await jtools._get_task_result(client, VOICE, query="Riesig")
    assert len(result["deliverables"][0]["content"]) == 4000


@pytest.mark.asyncio
async def test_tool_is_registered():
    names = {t.name for t in jtools.ALL_TOOLS}
    assert "get_task_result" in names


@pytest.mark.asyncio
async def test_resolve_task_empty_query_returns_none():
    client = _Client(tasks=[{"id": "t1", "title": "Egal", "status": "done"}])
    assert await jtools._resolve_task(client, "  ") is None
    assert client.calls == []
