"""The built-in ``Lead`` template is the hands-on lead agent of ADR-089.

Until 2026-10 it was the fleet-era pure orchestrator ("You write NO code",
dispatch table naming one operator's private agents), and the quickstart had
to tell users not to pick it (plan E9). It is the template the first run
instantiates, so it describes the lead that exists today.
"""
from __future__ import annotations

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent_template import AgentTemplate
from app.services.template_seeder import BUILTIN_TEMPLATES, seed_builtin_templates
from tests.conftest import test_engine

LEAD = next(t for t in BUILTIN_TEMPLATES if t["name"] == "Lead")


def test_lead_template_is_hands_on():
    soul = LEAD["soul_md"]
    assert "pure orchestrator" not in soul
    assert "You write NO code" not in soul
    assert "mc question" in soul


def test_lead_template_names_no_private_agents():
    for name in ("Cody", "Rex", "Boss", "Sparky", "FreeCode"):
        assert name not in LEAD["soul_md"], name


def test_lead_template_gets_no_credentials_scope():
    """ADR-089 §3: MC hands the lead agent no task credentials."""
    assert "credentials:read" not in LEAD["scopes"]
    assert "tasks:create" in LEAD["scopes"] and "approvals:create" in LEAD["scopes"]


@pytest.mark.asyncio
async def test_seeding_updates_an_existing_lead_template_in_place():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(AgentTemplate(name="Lead", role="lead", soul_md="# old orchestrator",
                            scopes=["credentials:read"], is_builtin=True))
        await s.commit()
        await seed_builtin_templates(s)
        rows = (await s.exec(select(AgentTemplate).where(AgentTemplate.name == "Lead"))).all()
    assert len(rows) == 1
    assert rows[0].soul_md == LEAD["soul_md"]
    assert "credentials:read" not in rows[0].scopes
