"""task_progress beantwortet 'wie weit ist X?' ohne dass Mark ins UI muss.

Die Fixtures kommen in ZWEI Formen vor, weil die realen Endpoints anders
antworten als der Plan annahm (verifiziert 25.07.):

- ``GET /api/v1/agent/boards/{bid}/tasks/{tid}/checklist`` liefert eine nackte
  Liste von ``TaskChecklistItem`` mit ``title`` + ``status``
  (pending|in_progress|done|blocked|skipped) — KEIN ``text``/``done``.
- ``GET /api/v1/agent/boards/{bid}/tasks/{tid}/events`` liefert eine nackte
  Liste von ``TaskEvent`` mit ``from_status``/``to_status``/``changed_by``/
  ``reason`` — KEIN ``message``/``event_type``.

Der Handler muss beide Formen tragen: die reale (title/status) und die
tolerante (text/done), damit ein Formwechsel im Backend das Tool nicht killt.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "jarvis_tools_prog", _ROOT / "jarvis_core" / "tools.py"
)
jtools = importlib.util.module_from_spec(_spec)
sys.modules["jarvis_tools_prog"] = jtools
_spec.loader.exec_module(jtools)

from jarvis_core.channels import VOICE


class _Client:
    def __init__(self, events=None, checklist=None):
        self._events = events or []
        self._checklist = checklist or []

    async def list_tasks(self, status=None, limit=10):
        return {"ok": True, "tasks": [
            {"id": "t1", "title": "Refactor Auth", "status": "in_progress",
             "assignee": "Rex"},
        ]}

    async def get_task_events(self, task_id, limit=10):
        return {"ok": True, "events": self._events}

    async def get_task_checklist(self, task_id):
        return {"ok": True, "items": self._checklist}


@pytest.mark.asyncio
async def test_reports_checklist_progress():
    client = _Client(checklist=[
        {"text": "A", "done": True},
        {"text": "B", "done": True},
        {"text": "C", "done": False},
    ])
    result = await jtools._task_progress(client, VOICE, query="Refactor")
    assert result["ok"] is True
    assert result["checklist"]["done"] == 2
    assert result["checklist"]["total"] == 3


@pytest.mark.asyncio
async def test_reports_checklist_progress_real_shape():
    """Reale Form: `title` + `status` statt `text` + `done`."""
    client = _Client(checklist=[
        {"title": "Tests schreiben", "status": "done"},
        {"title": "Implementieren", "status": "skipped"},
        {"title": "Review", "status": "in_progress"},
        {"title": "Deploy", "status": "pending"},
    ])
    result = await jtools._task_progress(client, VOICE, query="Refactor")
    assert result["ok"] is True
    # done + skipped zaehlen als abgehakt (skip ist ein bewusster Abschluss)
    assert result["checklist"]["done"] == 2
    assert result["checklist"]["total"] == 4
    assert result["checklist"]["open_items"] == ["Review", "Deploy"]


@pytest.mark.asyncio
async def test_events_use_real_field_names():
    """TaskEvent hat from_status/to_status/changed_by/reason."""
    client = _Client(events=[
        {"from_status": "in_progress", "to_status": "review",
         "changed_by": "agent", "reason": "handoff"},
        {"from_status": "todo", "to_status": "in_progress",
         "changed_by": "agent", "reason": None},
    ])
    result = await jtools._task_progress(client, VOICE, query="Refactor")
    assert result["ok"] is True
    assert len(result["recent_events"]) == 2
    assert "in_progress" in result["recent_events"][0]
    assert "review" in result["recent_events"][0]


@pytest.mark.asyncio
async def test_survives_missing_checklist():
    """Kein Checklist-Endpoint-Ergebnis darf das ganze Tool killen."""
    class _Broken(_Client):
        async def get_task_checklist(self, task_id):
            raise RuntimeError("boom")

    result = await jtools._task_progress(_Broken(), VOICE, query="Refactor")
    assert result["ok"] is True
    assert result["checklist"] is None


@pytest.mark.asyncio
async def test_survives_missing_events():
    """Faellt die Event-Quelle aus, kommt die Checkliste trotzdem durch."""
    class _Broken(_Client):
        async def get_task_events(self, task_id, limit=10):
            raise RuntimeError("boom")

    client = _Broken(checklist=[{"title": "A", "status": "done"}])
    result = await jtools._task_progress(client, VOICE, query="Refactor")
    assert result["ok"] is True
    assert result["recent_events"] == []
    assert result["checklist"]["done"] == 1


@pytest.mark.asyncio
async def test_unknown_task_is_structured():
    class _Empty(_Client):
        async def list_tasks(self, status=None, limit=10):
            return {"ok": True, "tasks": []}

    result = await jtools._task_progress(_Empty(), VOICE, query="Gibt es nicht")
    assert result["ok"] is False
    assert result["reason"] == "nothing_found"


@pytest.mark.asyncio
async def test_tool_is_registered():
    spec = jtools.BY_NAME.get("task_progress")
    assert spec is not None, "task_progress fehlt in ALL_TOOLS"
    assert spec.parameters["required"] == ["query"]
