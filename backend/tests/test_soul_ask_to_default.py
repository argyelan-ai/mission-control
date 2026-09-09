"""`mc ask --to` defaults to `boss` — the SOUL must say so.

An omitted `--to` addresses the agent's lead, never the operator. For a
worker that default is right; for an orchestrator it is a dead letter —
the question is addressed to the sender and waits forever. The rule was
missing from the template until a real orchestrator lost two operator
decisions to it, so this is a regression guard, not documentation polish.

Renders through build_agent_context() + render_agent_file() (the
production path) because SOUL.md.j2 runs under StrictUndefined.
"""
import uuid

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file


def _render(role: str, *, is_board_lead: bool, name: str) -> str:
    agent = Agent(
        id=uuid.uuid4(),
        name=name,
        role=role,
        board_id=uuid.uuid4(),
        is_board_lead=is_board_lead,
        comm_v2=True,
    )
    return render_agent_file("SOUL.md.j2", build_agent_context(agent, agents_on_board=[]))


def test_worker_soul_states_the_to_default():
    """Every comm_v2 agent must know the flag defaults to the lead."""
    rendered = _render("Developer", is_board_lead=False, name="alpha")
    assert "`--to` defaults to `boss`" in rendered
    assert "--to mark" in rendered


def test_worker_soul_omits_the_no_lead_above_you_clause():
    """A worker DOES have a lead — the dead-letter warning must not apply."""
    rendered = _render("Developer", is_board_lead=False, name="alpha")
    assert "You have no lead above you" not in rendered


def test_orchestrator_soul_warns_about_the_dead_letter():
    """Without --to mark an orchestrator asks itself; nobody ever reads it."""
    rendered = _render("Orchestrator", is_board_lead=False, name="the lead")
    assert "You have no lead above you" in rendered


def test_board_lead_soul_warns_about_the_dead_letter():
    rendered = _render("Developer", is_board_lead=True, name="the lead")
    assert "You have no lead above you" in rendered
