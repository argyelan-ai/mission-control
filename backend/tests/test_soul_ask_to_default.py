"""`mc ask` does not reach the operator — the SOUL must say so.

`mc ask` posts onto the task thread; its `--to` flag (default `boss`) is
stored as message metadata and routes nothing (agent_scoped.py, POST
/tasks/current/ask). A question meant for the operator sent that way sits
in a thread nobody opens. The channel the operator actually sees is
`mc question` (POST /boards/{id}/clarification -> approval).

The template documented `--to boss|mark|agent` without either fact, and
an orchestrator lost two operator decisions to it before the gap
surfaced. These are regression guards, not documentation polish.

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


def test_soul_states_that_ask_routes_nowhere():
    """Every comm_v2 agent must know --to is metadata, not delivery."""
    rendered = _render("Developer", is_board_lead=False, name="alpha")
    assert "posts to the task thread and nowhere else" in rendered
    assert "it routes nothing" in rendered


def test_soul_points_operator_decisions_at_mc_question():
    rendered = _render("Developer", is_board_lead=False, name="alpha")
    assert "goes through `mc question` instead" in rendered
    assert "`mc question` requires an `in_progress` task" in rendered


def test_worker_soul_omits_the_no_lead_above_you_clause():
    """A worker DOES have a lead — the dead-letter warning must not apply."""
    rendered = _render("Developer", is_board_lead=False, name="alpha")
    assert "no lead above you" not in rendered


def test_orchestrator_soul_warns_about_the_dead_letter():
    """Without a lead, an `mc ask` is addressed to the sender itself."""
    rendered = _render("Orchestrator", is_board_lead=False, name="the lead")
    assert "no lead above you" in rendered


def test_board_lead_soul_warns_about_the_dead_letter():
    rendered = _render("Developer", is_board_lead=True, name="the lead")
    assert "no lead above you" in rendered
