"""Tests for voice_worker/mc_client.py — Voice-Worker tool bridge.

The voice_worker package lives outside the backend tree (no editable install)
so we load it via importlib like test_hermes_bridge.

Bug A + B + C verification (2026-05-14):
- A: query_memory → /api/v1/agent/knowledge (not /agent/boards/{id}/knowledge)
- B: create_task fallback to board lead when assignee unknown / None
- C: STT-disambiguation via single-char fuzzy match in _resolve_agent_id
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLIENT_PATH = REPO_ROOT / "voice_worker" / "mc_client.py"


def _load_mc_client():
    spec = importlib.util.spec_from_file_location("voice_mc_client", MC_CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mc():
    return _load_mc_client()


@pytest.fixture
def agents_fixture():
    """Snapshot of mc-dev roster — minimal fields needed for resolver tests."""
    return [
        {"id": "aaa", "name": "Boss", "is_board_lead": True},
        {"id": "bbb", "name": "Sparky", "is_board_lead": False},
        {"id": "ccc", "name": "Davinci", "is_board_lead": False},
        {"id": "ddd", "name": "Rex", "is_board_lead": False},
        {"id": "eee", "name": "Jarvis", "is_board_lead": False},
    ]


def _patch_client_get(mc, agents):
    """Patch mc._client.get to return a 200-response with agents JSON for /agents calls."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=agents)
    mc._client.get = AsyncMock(return_value=resp)


# ────────────────────────────────────────────────────────────────────────
# Bug C: STT-Disambiguierung — _resolve_agent_id Fuzzy-Match
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_agent_id_exact_match(mc, agents_fixture):
    _patch_client_get(mc, agents_fixture)
    rid, name = await mc._resolve_agent_id("Sparky")
    assert rid == "bbb" and name == "Sparky"


@pytest.mark.asyncio
async def test_resolve_agent_id_case_insensitive(mc, agents_fixture):
    _patch_client_get(mc, agents_fixture)
    rid, name = await mc._resolve_agent_id("sparky")
    assert rid == "bbb" and name == "Sparky"


@pytest.mark.asyncio
async def test_resolve_agent_id_fuzzy_single_substitution(mc, agents_fixture):
    """Sparky → 'Sperky' (single-char swap) should match."""
    _patch_client_get(mc, agents_fixture)
    rid, name = await mc._resolve_agent_id("Sperky")
    assert rid == "bbb" and name == "Sparky"


@pytest.mark.asyncio
async def test_resolve_agent_id_fuzzy_single_insertion(mc, agents_fixture):
    """Davinci → 'Daviinci' (single-char insert) should match."""
    _patch_client_get(mc, agents_fixture)
    rid, name = await mc._resolve_agent_id("Daviinci")
    assert rid == "ccc" and name == "Davinci"


@pytest.mark.asyncio
async def test_resolve_agent_id_no_match_when_too_far(mc, agents_fixture):
    """Multi-char distance must NOT match — defensive default to fallback."""
    _patch_client_get(mc, agents_fixture)
    rid, name = await mc._resolve_agent_id("Spaki")  # 2-char distance from Sparky
    assert rid is None and name is None


@pytest.mark.asyncio
async def test_resolve_agent_id_empty_input(mc, agents_fixture):
    rid, name = await mc._resolve_agent_id(None)
    assert rid is None and name is None
    rid, name = await mc._resolve_agent_id("")
    assert rid is None and name is None


@pytest.mark.asyncio
async def test_resolve_agent_id_handles_http_error(mc):
    resp = MagicMock()
    resp.status_code = 500
    mc._client.get = AsyncMock(return_value=resp)
    rid, name = await mc._resolve_agent_id("Sparky")
    assert rid is None and name is None


# ────────────────────────────────────────────────────────────────────────
# Bug B: Board-Lead Fallback
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_board_lead_returns_boss(mc, agents_fixture):
    _patch_client_get(mc, agents_fixture)
    rid, name = await mc._find_board_lead()
    assert rid == "aaa" and name == "Boss"


@pytest.mark.asyncio
async def test_find_board_lead_none_when_no_lead(mc):
    agents_no_lead = [{"id": "x", "name": "Worker", "is_board_lead": False}]
    _patch_client_get(mc, agents_no_lead)
    rid, name = await mc._find_board_lead()
    assert rid is None and name is None


@pytest.mark.asyncio
async def test_create_task_without_assignee_routes_to_board_lead(mc, agents_fixture):
    """Bug B: kein assignee → muss an Board Lead, NIE an creator (Jarvis) selbst."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json = MagicMock(return_value=agents_fixture)

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json = MagicMock(return_value={"id": "task-xxx", "title": "Test"})
    post_resp.raise_for_status = MagicMock()

    captured: dict = {}

    async def _post(url, json=None):
        captured["url"] = url
        captured["payload"] = json
        return post_resp

    mc._client.get = AsyncMock(return_value=get_resp)
    mc._client.post = AsyncMock(side_effect=_post)

    result = await mc.create_task(title="Test", description="")
    assert result["ok"] is True
    assert result["assigned_to"] == "Boss"
    assert captured["payload"]["assigned_agent_id"] == "aaa"  # Boss


@pytest.mark.asyncio
async def test_create_task_unknown_assignee_falls_back_to_board_lead(mc, agents_fixture):
    """Bug B: unbekannter Name → Board Lead + erklärende Note."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json = MagicMock(return_value=agents_fixture)

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json = MagicMock(return_value={"id": "task-yyy", "title": "Test"})
    post_resp.raise_for_status = MagicMock()

    mc._client.get = AsyncMock(return_value=get_resp)
    mc._client.post = AsyncMock(return_value=post_resp)

    result = await mc.create_task(title="Test", assigned_agent_name="Kodi")
    assert result["ok"] is True
    assert result["assigned_to"] == "Boss"
    assert "Kodi" in (result["note"] or "")
    assert "Board Lead" in (result["note"] or "")


@pytest.mark.asyncio
async def test_create_task_known_assignee_goes_to_named_agent(mc, agents_fixture):
    """Happy path: exact name match routes to that agent, no fallback."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json = MagicMock(return_value=agents_fixture)

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json = MagicMock(return_value={"id": "task-zzz", "title": "Test"})
    post_resp.raise_for_status = MagicMock()

    captured_payload: dict = {}

    async def _post(url, json=None):
        captured_payload.update(json)
        return post_resp

    mc._client.get = AsyncMock(return_value=get_resp)
    mc._client.post = AsyncMock(side_effect=_post)

    result = await mc.create_task(title="Test", assigned_agent_name="Sparky")
    assert result["ok"] is True
    assert result["assigned_to"] == "Sparky"
    assert result.get("note") is None
    assert captured_payload["assigned_agent_id"] == "bbb"  # Sparky


# ────────────────────────────────────────────────────────────────────────
# Bug A: query_memory uses correct endpoint
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_memory_uses_agent_knowledge_endpoint(mc):
    """Bug A: query_memory MUST hit /api/v1/agent/knowledge (no board_id prefix)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=[
        {"title": "Lesson X", "memory_type": "lesson", "content": "We learned A is better than B because..."},
    ])

    captured: dict = {}

    async def _get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return resp

    mc._client.get = AsyncMock(side_effect=_get)

    result = await mc.query_memory("lesson")
    assert result["ok"] is True
    assert result["count"] == 1
    assert captured["url"] == "/api/v1/agent/knowledge"  # NOT /agent/boards/{}/knowledge
    assert captured["params"]["search"] == "lesson"


@pytest.mark.asyncio
async def test_query_memory_returns_snippet(mc):
    """query_memory entries include a content snippet (≤200 chars)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=[
        {"title": "Test", "memory_type": "knowledge", "content": "A" * 500},
    ])
    mc._client.get = AsyncMock(return_value=resp)
    result = await mc.query_memory("test")
    assert len(result["entries"][0]["snippet"]) == 200


@pytest.mark.asyncio
async def test_query_memory_handles_404(mc):
    resp = MagicMock()
    resp.status_code = 404
    mc._client.get = AsyncMock(return_value=resp)
    result = await mc.query_memory("x")
    assert result["ok"] is False
    assert "404" in result["error"]
