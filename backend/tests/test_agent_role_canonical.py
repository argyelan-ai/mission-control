"""W1 (PR #514 Rex review): `Agent.role` is freetext-capable in the backend
(the generic PATCH /agents/{id} setattr loop bypasses the model's enum
validator — see app/scopes.py:normalize_agent_role docstring), but the
frontend needs a value it can safely compare against "reviewer". These tests
cover the canonicalization helper and its exposure on GET /api/v1/agents.
"""
import pytest
from sqlalchemy import update as _sa_update

from app.models.agent import Agent


async def _set_freetext_role(session, agent_id, freetext: str) -> None:
    """Write a role value the Pydantic validator would reject on create.

    Mirrors test_find_reviewer_board_lead_fallback.py's `_set_freetext_role`:
    SQLAlchemy ORM reconstruction from a DB row does not re-run
    field_validator(mode="before"), so a raw UPDATE is the only way to
    reproduce the real broken state (also how the generic PATCH
    /agents/{id} setattr loop writes it, since that bypasses validation too).
    """
    await session.exec(_sa_update(Agent).where(Agent.id == agent_id).values(role=freetext))
    await session.commit()


def test_normalize_agent_role_exact_match():
    from app.scopes import AgentRole, normalize_agent_role

    assert normalize_agent_role("reviewer") == AgentRole.REVIEWER


def test_normalize_agent_role_case_and_whitespace_variants():
    from app.scopes import AgentRole, normalize_agent_role

    assert normalize_agent_role("Reviewer") == AgentRole.REVIEWER
    assert normalize_agent_role(" REVIEWER ") == AgentRole.REVIEWER


def test_normalize_agent_role_unrecognized_freetext_is_none():
    from app.scopes import normalize_agent_role

    assert normalize_agent_role("does review stuff sometimes") is None


def test_normalize_agent_role_none_input_is_none():
    from app.scopes import normalize_agent_role

    assert normalize_agent_role(None) is None


@pytest.mark.asyncio
async def test_list_agents_exposes_role_canonical_for_clean_role(auth_client, make_agent):
    agent = await make_agent(name="CleanRoleAgent", role="reviewer")
    resp = await auth_client.get("/api/v1/agents")
    row = next(a for a in resp.json() if a["id"] == str(agent.id))
    assert row["role"] == "reviewer"
    assert row["role_canonical"] == "reviewer"


@pytest.mark.asyncio
async def test_list_agents_resolves_role_canonical_for_freetext_role(auth_client, make_agent, session):
    agent = await make_agent(name="FreetextRoleAgent", role="reviewer")
    await _set_freetext_role(session, agent.id, "Reviewer (on-call)")

    resp = await auth_client.get("/api/v1/agents")
    row = next(a for a in resp.json() if a["id"] == str(agent.id))
    assert row["role"] == "Reviewer (on-call)"
    # Doesn't match the enum → no canonical role → routing must fail safe (operator).
    assert row["role_canonical"] is None


@pytest.mark.asyncio
async def test_list_agents_role_canonical_none_for_no_role(auth_client, make_agent):
    agent = await make_agent(name="NoRoleAgent")
    resp = await auth_client.get("/api/v1/agents")
    row = next(a for a in resp.json() if a["id"] == str(agent.id))
    assert row["role"] is None
    assert row["role_canonical"] is None
