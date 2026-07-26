"""list_tasks erreicht auch abgeschlossene Tasks.

Bug #2 der Welle A+B: list_open_tasks filterte client-seitig auf
inbox|in_progress|blocked|review — "done" fiel raus und war fuer Jarvis
unerreichbar, obwohl das Backend ?status=done kann.

Das Modul liegt ausserhalb des backend-Trees (kein editable install), deshalb
laden wir es per importlib wie in test_voice_worker_mc_client.py.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLIENT_PATH = REPO_ROOT / "jarvis_core" / "mc_client.py"


def _load_mc_client():
    spec = importlib.util.spec_from_file_location("jarvis_mc_client", MC_CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["jarvis_mc_client"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mc():
    return _load_mc_client()


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.mark.asyncio
async def test_list_tasks_passes_status_to_backend(mc, monkeypatch):
    seen = {}

    async def fake_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        return _FakeResponse([
            {"id": "t1", "title": "Fertig", "status": "done",
             "assigned_agent_name": "Researcher"},
        ])

    monkeypatch.setattr(mc._client, "get", fake_get)
    result = await mc.list_tasks(status="done")

    assert seen["params"]["status"] == "done"
    assert result["ok"] is True
    assert result["tasks"][0]["status"] == "done"
    assert result["tasks"][0]["id"] == "t1"
    assert result["tasks"][0]["assignee"] == "Researcher"


@pytest.mark.asyncio
async def test_list_tasks_default_is_open_only(mc, monkeypatch):
    """Ohne status bleibt das Verhalten wie bisher: nur offene Tasks."""
    seen = {}

    async def fake_get(url, params=None):
        seen["params"] = params
        return _FakeResponse([
            {"id": "a", "title": "Offen", "status": "in_progress"},
            {"id": "b", "title": "Fertig", "status": "done"},
            {"id": "c", "title": "Kaputt", "status": "failed"},
        ])

    monkeypatch.setattr(mc._client, "get", fake_get)
    result = await mc.list_tasks()

    titles = [t["title"] for t in result["tasks"]]
    assert titles == ["Offen"]
    assert "status" not in seen["params"]


@pytest.mark.asyncio
async def test_list_tasks_respects_limit(mc, monkeypatch):
    async def fake_get(url, params=None):
        return _FakeResponse([
            {"id": str(i), "title": f"T{i}", "status": "inbox"} for i in range(30)
        ])

    monkeypatch.setattr(mc._client, "get", fake_get)
    result = await mc.list_tasks(limit=3)
    assert len(result["tasks"]) == 3
    assert result["count"] == 3


@pytest.mark.asyncio
async def test_limit_is_clamped_to_backend_maximum(mc, monkeypatch):
    """Das Backend deckelt limit bei 200 (le=200) — wir schicken nie mehr."""
    seen = {}

    async def fake_get(url, params=None):
        seen["params"] = params
        return _FakeResponse([])

    monkeypatch.setattr(mc._client, "get", fake_get)
    await mc.list_tasks(limit=9999)
    assert seen["params"]["limit"] <= 50


@pytest.mark.asyncio
async def test_list_open_tasks_alias_still_works(mc, monkeypatch):
    """tools.py:_show_task ruft weiterhin list_open_tasks() auf."""
    async def fake_get(url, params=None):
        return _FakeResponse([
            {"id": "a", "title": "Offen", "status": "review"},
            {"id": "b", "title": "Fertig", "status": "done"},
        ])

    monkeypatch.setattr(mc._client, "get", fake_get)
    result = await mc.list_open_tasks()
    assert result["ok"] is True
    assert [t["title"] for t in result["tasks"]] == ["Offen"]
