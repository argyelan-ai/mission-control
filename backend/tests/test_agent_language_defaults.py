"""Fleet default languages for new agents (Settings → Language).

Two app_settings rows seed ``operator_language`` / ``work_language`` of every
NEW agent; existing agents keep theirs. Each create path is tested on its own
so a path that forgets ``apply_language_defaults`` turns red, and a source
guard catches a future create path nobody wrote a test for.
"""
import re
import uuid
from pathlib import Path

import pytest
from fastapi import BackgroundTasks
from sqlmodel import select

from app.models.agent import Agent
from app.models.app_setting import AppSetting
from app.services import agent_language_defaults as lang


async def _set_german(session) -> None:
    await lang.save_defaults(session, {"operator_language": "de", "work_language": "de"})


# ── store ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_defaults_are_english_without_saved_rows(async_session):
    assert (await lang.load_defaults(async_session)).to_dict() == {
        "operator_language": "en", "work_language": "en",
    }


@pytest.mark.asyncio
async def test_save_then_load_roundtrip(async_session):
    saved = await lang.save_defaults(async_session, {"operator_language": "DE"})
    assert saved.operator_language == "de"
    assert saved.work_language == "en"
    assert (await lang.load_defaults(async_session)).operator_language == "de"


@pytest.mark.asyncio
async def test_invalid_value_writes_nothing(async_session):
    with pytest.raises(ValueError) as exc:
        await lang.save_defaults(async_session, {"operator_language": "de", "work_language": "xx"})
    assert exc.value.args[0] == ["work_language"]
    rows = (await async_session.exec(select(AppSetting))).all()
    assert [r for r in rows if r.key in lang.SETTING_FIELDS] == []


@pytest.mark.asyncio
async def test_broken_row_falls_back_to_english(async_session):
    async_session.add(AppSetting(key="agent_default_operator_language", value="klingon"))
    await async_session.commit()
    assert (await lang.load_defaults(async_session)).operator_language == "en"


# ── endpoint ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_endpoint_get_and_put(auth_client):
    resp = await auth_client.get("/api/v1/settings/agent-language")
    assert resp.status_code == 200
    body = resp.json()
    assert body["defaults"] == {"operator_language": "en", "work_language": "en"}
    assert body["supported"] == ["en", "de"]

    resp = await auth_client.put(
        "/api/v1/settings/agent-language", json={"operator_language": "de"},
    )
    assert resp.status_code == 200
    assert resp.json()["defaults"]["operator_language"] == "de"

    resp = await auth_client.get("/api/v1/settings/agent-language")
    assert resp.json()["defaults"]["operator_language"] == "de"


@pytest.mark.asyncio
async def test_endpoint_rejects_unknown_language(auth_client):
    resp = await auth_client.put("/api/v1/settings/agent-language", json={"work_language": "xx"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_language"


@pytest.mark.asyncio
async def test_endpoint_does_not_shadow_generic_user_setting(auth_client):
    """The static route sits in front of /settings/{key}; the generic
    per-user store (voice_language) must still work."""
    put = await auth_client.put("/api/v1/settings/voice_language", json={"value": {"language": "de"}})
    assert put.status_code == 200
    got = await auth_client.get("/api/v1/settings/voice_language")
    assert got.json()["value"] == {"language": "de"}


# ── every create path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_agent_endpoint_uses_defaults(auth_client, async_session):
    await _set_german(async_session)
    resp = await auth_client.post("/api/v1/agents", json={"name": "LangCreate"})
    assert resp.status_code == 201, resp.text
    agent_id = uuid.UUID(resp.json()["agent"]["id"] if "agent" in resp.json() else resp.json()["id"])
    async_session.expire_all()
    agent = await async_session.get(Agent, agent_id)
    assert (agent.operator_language, agent.work_language) == ("de", "de")


@pytest.mark.asyncio
async def test_template_instantiate_uses_defaults(async_session):
    from app.models.agent_template import AgentTemplate
    from app.routers.agent_templates import _do_instantiate

    await _set_german(async_session)
    tpl = AgentTemplate(name="LangTpl", emoji="🧠", role="planner", scopes=[])
    async_session.add(tpl)
    await async_session.commit()
    await async_session.refresh(tpl)
    agent, _ = await _do_instantiate(
        template=tpl, board_id=None, name="LangFromTpl", model=None, session=async_session,
    )
    assert (agent.operator_language, agent.work_language) == ("de", "de")


@pytest.mark.asyncio
async def test_setup_specialized_uses_defaults(auth_client, async_session, make_board):
    await _set_german(async_session)
    board = await make_board(name="Lang Board", slug="lang-board")
    resp = await auth_client.post("/api/v1/agents/setup-specialized", json={"board_id": str(board.id)})
    assert resp.status_code == 201, resp.text
    async_session.expire_all()
    agents = (await async_session.exec(select(Agent).where(Agent.board_id == board.id))).all()
    assert agents, "setup-specialized created no agents"
    assert {(a.operator_language, a.work_language) for a in agents} == {("de", "de")}


@pytest.mark.asyncio
async def test_lead_agent_create_uses_defaults(async_session):
    from app.routers.agent_scoped import AgentCreateByAgentRequest, agent_create_custom

    await _set_german(async_session)
    lead = Agent(name="LangLead", is_board_lead=True)
    async_session.add(lead)
    await async_session.commit()
    await async_session.refresh(lead)
    result = await agent_create_custom(
        AgentCreateByAgentRequest(name="LangByLead"), BackgroundTasks(), async_session, lead,
    )
    created_id = result["agent"]["id"] if isinstance(result["agent"], dict) else result["agent"].id
    created = await async_session.get(Agent, uuid.UUID(str(created_id)))
    assert (created.operator_language, created.work_language) == ("de", "de")


@pytest.mark.asyncio
async def test_soul_preview_renders_with_defaults(auth_client, async_session):
    """The wizard's persona preview shows what the agent will get."""
    await _set_german(async_session)
    resp = await auth_client.post("/api/v1/agents/preview-soul", json={"name": "LangPreview"})
    assert resp.status_code == 200
    assert "always in **de**" in resp.json()["soul_md"]


# ── source guard: no create path without the defaults ──────────────────────

#: Agent(...) constructions that are never persisted.
_TRANSIENT = {
    "app/services/heads/launcher.py",  # throwaway object for a head launch, never added to the session
}


def test_every_agent_construction_applies_defaults():
    root = Path(__file__).resolve().parents[1]
    missing: list[str] = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _TRANSIENT:
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"\s*(\w+)\s*=\s*Agent\(", line)
            if not m:
                continue
            var = m.group(1)
            call = re.compile(rf"^\s*await apply_language_defaults\(session, {var}\)")
            if not any(call.match(ln) for ln in lines[i : i + 40]):
                missing.append(f"{rel}:{i + 1}")
    assert missing == [], f"Agent created without apply_language_defaults: {missing}"
