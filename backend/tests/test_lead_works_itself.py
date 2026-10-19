"""A board lead with no team on its board does the work itself (ADR-089).

The old lead instructions came from the fleet model: "you write no code,
delegate everything". On a fresh install the lead agent is the only agent —
there is nobody to delegate to, so those instructions are a dead end (the
dispatch even said "(No team agents found — check board config)"). One rule,
one place (``services/lead_mode.py``), read by every surface that tells the
agent how to work:

* SOUL.md (hands-on lead block + the worker rules),
* the Operating Card (no "writing code yourself is forbidden" line),
* the dispatch message (approach + git section instead of the delegation block).

A lead WITH a team keeps the delegation behaviour unchanged.
"""
from __future__ import annotations

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.services.lead_mode import lead_works_itself
from app.services.template_renderer import build_agent_context, render_agent_file
from tests.conftest import test_engine


def _lead(board_id=None, **kw) -> Agent:
    return Agent(
        id=uuid.uuid4(), name="Lead", role="lead", is_board_lead=True,
        board_id=board_id, agent_runtime="cli-bridge", harness="claude",
        agent_token_hash=uuid.uuid4().hex, **kw,
    )


def _helper(board_id=None) -> Agent:
    return Agent(
        id=uuid.uuid4(), name="Builder", role="developer", board_id=board_id,
        agent_runtime="cli-bridge", agent_token_hash=uuid.uuid4().hex,
    )


# ── the rule ───────────────────────────────────────────────────────────────


def test_rule():
    assert lead_works_itself(is_lead=True, teammates=0) is True
    assert lead_works_itself(is_lead=True, teammates=2) is False
    assert lead_works_itself(is_lead=False, teammates=0) is False


# ── SOUL.md and the Operating Card ─────────────────────────────────────────


def _render(template: str, agent: Agent, on_board: list[Agent]) -> str:
    return render_agent_file(template, build_agent_context(agent, agents_on_board=on_board))


def test_lone_lead_soul_says_work_yourself_and_ask_before_irreversible_steps():
    lead = _lead()
    soul = _render("SOUL.md.j2", lead, [lead])
    assert "You do the work yourself" in soul
    assert "mc question" in soul
    assert "Worker-Truth" in soul, "a hands-on lead closes its own cards like a worker"
    assert "I never write code myself" not in soul


def test_lead_with_a_team_keeps_the_delegating_soul():
    lead = _lead()
    soul = _render("SOUL.md.j2", lead, [lead, _helper()])
    assert "I never write code myself" in soul
    assert "You do the work yourself" not in soul


def test_lone_lead_card_does_not_forbid_writing_code():
    lead = _lead()
    card = _render("CARD.md.j2", lead, [lead])
    assert "Writing/testing code yourself" not in card
    with_team = _render("CARD.md.j2", lead, [lead, _helper()])
    assert "Writing/testing code yourself" in with_team


# ── the dispatch message ───────────────────────────────────────────────────


async def _dispatch_text(with_team: bool) -> str:
    from app.services.dispatch_message_builder import _build_dispatch_message

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        lead = _lead(board.id, workspace_path="/tmp/mc-test-ws/lead")
        s.add(lead)
        if with_team:
            s.add(_helper(board.id))
        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="Add a test for parse()",
            description="Practice job.", status="inbox", assigned_agent_id=lead.id,
            skip_review=True, workspace_path="/tmp/mc-test-ws/lead/mc-practice",
        )
        s.add(task)
        await s.commit()
        return await _build_dispatch_message(task, lead, s)


@pytest.mark.asyncio
async def test_lone_lead_dispatch_carries_the_approach_and_git_section():
    text = await _dispatch_text(with_team=False)
    assert "## Approach" in text
    assert "Working directory" in text
    assert "Automation task — no review needed" in text
    assert "Create ONLY implementation tasks" not in text


@pytest.mark.asyncio
async def test_lead_with_a_team_still_gets_the_delegation_block():
    text = await _dispatch_text(with_team=True)
    assert "Create ONLY implementation tasks" in text
    assert "## Approach" not in text
