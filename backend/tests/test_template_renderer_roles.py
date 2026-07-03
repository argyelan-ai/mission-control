"""Tests for template_renderer role_type_map + SOUL.md rendering."""
import uuid
import pytest
from app.services.template_renderer import build_agent_context, render_agent_file
from app.models.agent import Agent


def _make_agent(name: str, role: str = "developer", is_board_lead: bool = False) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name=name,
        role=role,
        board_id=uuid.uuid4(),
        is_board_lead=is_board_lead,
    )


def test_freecode_gets_developer_role():
    """FreeCode should still get role=developer."""
    agent = _make_agent("FreeCode", role="Developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "developer"


def test_boss_gets_orchestrator_role():
    """Boss should get role=orchestrator."""
    agent = _make_agent("Boss", role="Orchestrator")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "orchestrator"


def test_henry_gets_lead_role():
    """Henry should get role=lead."""
    agent = _make_agent("Henry", role="Board Lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "lead"


def test_shakespeare_gets_writer_role():
    """Shakespeare should get role=writer."""
    agent = _make_agent("Shakespeare", role="Content Writer")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "writer"


def test_davinci_gets_designer_role():
    """Davinci should get role=designer."""
    agent = _make_agent("Davinci", role="Graphic Designer")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "designer"


def test_rendered_soul_no_mc_token():
    """No $MC_TOKEN in the rendered SOUL.md — should be $MC_AGENT_TOKEN."""
    agent = _make_agent("Rex", role="reviewer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "$MC_TOKEN" not in soul, "$MC_TOKEN noch im Template!"


def test_rendered_developer_soul_no_mc_token():
    """No $MC_TOKEN in the developer SOUL.md."""
    agent = _make_agent("FreeCode", role="developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "$MC_TOKEN" not in soul


def test_henry_soul_has_autonomy_level_rule():
    """Henry's SOUL.md must contain the autonomy_level rule."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "autonomy_level" in soul
    assert "execute_low_risk" in soul


def test_henry_soul_has_tags_rule():
    """Henry's SOUL.md must contain the mandatory tags rule."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Tags setzen" in soul


def test_henry_soul_has_preview_url_rule():
    """Henry's SOUL.md must contain the preview URL rule."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Preview-URL" in soul or "target_url" in soul


def test_henry_soul_has_markdown_description_rule():
    """Henry's SOUL.md must contain the mandatory Markdown description rule."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Markdown" in soul and "VERBOTEN" in soul


def test_freecode_soul_has_workspace_path():
    """FreeCode's SOUL.md must contain the workspace path."""
    agent = _make_agent("FreeCode", role="developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "~/FreeCode/projects/" in soul


def test_freecode_soul_has_git_workflow():
    """FreeCode's SOUL.md must contain a Git workflow section.

    Historically the assumption was: the agent runs `git checkout -b task/...`
    itself. Since the worktree-based dispatch, the worktree is already
    checked out to `task/<slug>` beforehand — the agent only pushes.
    This test therefore checks the new essential markers:
    - Git workflow section exists
    - task/ branch naming is mentioned
    - git push to remote is documented
    """
    agent = _make_agent("FreeCode", role="developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Git-Workflow" in soul
    assert "task/" in soul
    assert "git push" in soul


def test_researcher_soul_has_knowledge_base():
    """Researcher's SOUL.md must mention a knowledge base entry as output."""
    agent = _make_agent("Researcher", role="researcher")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "knowledge" in soul.lower() or "/knowledge" in soul


def test_shakespeare_soul_has_marks_stil():
    """Shakespeare's SOUL.md must contain the operator's style reference."""
    agent = _make_agent("Shakespeare", role="writer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Corporate" in soul or "persoenlich" in soul or "direkt" in soul


def test_deployer_soul_has_deploy_url_deliverable():
    """Deployer's SOUL.md must mention the deploy URL as a deliverable."""
    agent = _make_agent("Deployer", role="deployer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "deliverable" in soul.lower() or "Deliverable" in soul


def test_davinci_soul_has_tools():
    """Davinci's SOUL.md must mention ComfyUI or Remotion."""
    agent = _make_agent("Davinci", role="designer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "ComfyUI" in soul or "Remotion" in soul
