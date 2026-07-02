"""Tests fuer template_renderer role_type_map + SOUL.md Rendering."""
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
    """FreeCode soll weiterhin role=developer bekommen."""
    agent = _make_agent("FreeCode", role="Developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "developer"


def test_boss_gets_orchestrator_role():
    """Boss soll role=orchestrator bekommen."""
    agent = _make_agent("Boss", role="Orchestrator")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "orchestrator"


def test_henry_gets_lead_role():
    """Henry soll role=lead bekommen."""
    agent = _make_agent("Henry", role="Board Lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "lead"


def test_shakespeare_gets_writer_role():
    """Shakespeare soll role=writer bekommen."""
    agent = _make_agent("Shakespeare", role="Content Writer")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "writer"


def test_davinci_gets_designer_role():
    """Davinci soll role=designer bekommen."""
    agent = _make_agent("Davinci", role="Graphic Designer")
    ctx = build_agent_context(agent, agents_on_board=[])
    assert ctx["role"] == "designer"


def test_rendered_soul_no_mc_token():
    """Kein $MC_TOKEN im gerenderten SOUL.md — soll $MC_AGENT_TOKEN sein."""
    agent = _make_agent("Rex", role="reviewer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "$MC_TOKEN" not in soul, "$MC_TOKEN noch im Template!"


def test_rendered_developer_soul_no_mc_token():
    """Kein $MC_TOKEN im developer SOUL.md."""
    agent = _make_agent("FreeCode", role="developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "$MC_TOKEN" not in soul


def test_henry_soul_has_autonomy_level_rule():
    """Henry's SOUL.md muss autonomy_level-Regel enthalten."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "autonomy_level" in soul
    assert "execute_low_risk" in soul


def test_henry_soul_has_tags_rule():
    """Henry's SOUL.md muss Tags-Pflicht enthalten."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Tags setzen" in soul


def test_henry_soul_has_preview_url_rule():
    """Henry's SOUL.md muss Preview-URL-Regel enthalten."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Preview-URL" in soul or "target_url" in soul


def test_henry_soul_has_markdown_description_rule():
    """Henry's SOUL.md muss Markdown-Description-Pflicht enthalten."""
    agent = _make_agent("Henry", role="lead", is_board_lead=True)
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Markdown" in soul and "VERBOTEN" in soul


def test_freecode_soul_has_workspace_path():
    """FreeCode's SOUL.md muss Workspace-Pfad enthalten."""
    agent = _make_agent("FreeCode", role="developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "~/FreeCode/projects/" in soul


def test_freecode_soul_has_git_workflow():
    """FreeCode's SOUL.md muss Git-Workflow-Sektion enthalten.

    Historisch war die Annahme: der Agent macht `git checkout -b task/...`
    selbst. Seit dem Worktree-basierten Dispatch ist das Worktree aber
    schon vorher auf `task/<slug>` ausgecheckt — der Agent pushed nur.
    Dieser Test prueft darum die neuen essentiellen Marker:
    - Git-Workflow Sektion existiert
    - task/ Branch Naming wird erwaehnt
    - git push zum remote ist dokumentiert
    """
    agent = _make_agent("FreeCode", role="developer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Git-Workflow" in soul
    assert "task/" in soul
    assert "git push" in soul


def test_researcher_soul_has_knowledge_base():
    """Researcher's SOUL.md muss Knowledge-Base-Eintrag als Output erwähnen."""
    agent = _make_agent("Researcher", role="researcher")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "knowledge" in soul.lower() or "/knowledge" in soul


def test_shakespeare_soul_has_marks_stil():
    """Shakespeare's SOUL.md muss die Stil-Referenz des Operators enthalten."""
    agent = _make_agent("Shakespeare", role="writer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "Corporate" in soul or "persoenlich" in soul or "direkt" in soul


def test_deployer_soul_has_deploy_url_deliverable():
    """Deployer's SOUL.md muss Deploy-URL als Deliverable erwähnen."""
    agent = _make_agent("Deployer", role="deployer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "deliverable" in soul.lower() or "Deliverable" in soul


def test_davinci_soul_has_tools():
    """Davinci's SOUL.md muss ComfyUI oder Remotion erwähnen."""
    agent = _make_agent("Davinci", role="designer")
    ctx = build_agent_context(agent, agents_on_board=[])
    soul = render_agent_file("SOUL.md.j2", ctx)
    assert "ComfyUI" in soul or "Remotion" in soul
