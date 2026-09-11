"""W5-C: `POST /api/v1/agent/questions` was documented in SOUL.md.j2 but no
such route exists (404) — a dead reference that sent Boss down a dead end
whenever it needed to ask the operator for a spawn approval. Pins both the
absence of the dead path and the presence of the real one, in the raw
template (source of truth) and in a rendered SOUL for the affected roles.
"""
from __future__ import annotations

import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOUL_TEMPLATE = REPO_ROOT / "backend" / "templates" / "SOUL.md.j2"


def test_raw_template_has_no_dead_questions_endpoint():
    text = SOUL_TEMPLATE.read_text(encoding="utf-8")
    assert "agent/questions" not in text, (
        "SOUL.md.j2 still references the dead POST /api/v1/agent/questions "
        "endpoint (404 in production)."
    )


def test_raw_template_points_at_the_real_spawn_endpoint():
    text = SOUL_TEMPLATE.read_text(encoding="utf-8")
    assert "POST /api/v1/agent/agents/request-spawn" in text


def test_rendered_orchestrator_soul_has_no_dead_reference():
    from app.models.agent import Agent
    from app.services.template_renderer import build_agent_context, render_agent_file

    for role in ("orchestrator", "lead"):
        agent = Agent(id=uuid.uuid4(), name="X", role=role, board_id=uuid.uuid4())
        soul = render_agent_file(
            "SOUL.md.j2", build_agent_context(agent, agents_on_board=[])
        )
        assert "agent/questions" not in soul, role
