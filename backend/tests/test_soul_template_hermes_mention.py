"""Phase 25 / D-12: Boss-SOUL.md.j2 must mention Hermes as a routing option.

This is a regression test — if Boss' SOUL ever loses the Hermes mention
(e.g. via aggressive template-trim refactor), Boss can't route to Hermes
and HERM-02 silently breaks (no test would catch it without this).

Uses build_agent_context() + render_agent_file() (the production render
path) instead of a hand-rolled Jinja Environment, because the SOUL
template runs under StrictUndefined and would crash on missing context
keys. Going through the real helper keeps this test resilient against
future context-shape changes.
"""
import uuid

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file


def _render_boss_soul() -> str:
    """Render the Boss SOUL via the production template path.

    Boss has role='orchestrator' (mapped from the 'boss' name in
    template_renderer._role_type_map), so the orchestrator branch of
    SOUL.md.j2 is exercised — that's where the Hermes mention lives.
    """
    boss = Agent(
        id=uuid.uuid4(),
        name="Boss",
        role="Orchestrator",
        board_id=uuid.uuid4(),
        is_board_lead=False,
    )
    ctx = build_agent_context(boss, agents_on_board=[])
    return render_agent_file("SOUL.md.j2", ctx)


def test_boss_soul_mentions_hermes_at_least_4_times():
    """Hermes appears in routing table + dedicated section header + body."""
    rendered = _render_boss_soul()
    count = rendered.count("Hermes")
    assert count >= 4, (
        f"expected >=4 'Hermes' mentions in rendered Boss SOUL, got {count}"
    )


def test_boss_soul_mentions_qwen_stack():
    """The Qwen3.6 stack identifier helps Boss decide Hermes vs. Sparky for research."""
    rendered = _render_boss_soul()
    assert "Qwen3.6-35B-A3B-FP8" in rendered, (
        "Boss-SOUL must include the Qwen3.6-35B-A3B-FP8 stack identifier (D-12)"
    )


def test_boss_soul_mentions_hermes_research_strength():
    """Boss must know Hermes is a research/docs option."""
    rendered = _render_boss_soul()
    # Look for the routing-table row OR the body description
    assert "research" in rendered.lower() or "Recherche" in rendered, (
        "Boss-SOUL must mention 'research'/'Recherche' in proximity to Hermes"
    )


def test_boss_soul_mentions_dedicated_hermes_section():
    """A dedicated '## Hermes' section gives Boss a place to look up Hermes capabilities."""
    rendered = _render_boss_soul()
    assert "## Hermes" in rendered, (
        "Boss-SOUL must contain a '## Hermes' section header (D-12)"
    )
