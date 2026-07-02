"""Phase 2 slim test: Researcher subtask dispatch must be lean.

After Phase 1 moved Worker-Contract / Lifecycle / Two-Zone rules into
SOUL.md (--append-system-prompt persistent), the per-task dispatch
message no longer needs to repeat them. This test asserts:

1. Researcher subtask dispatch ≤ 1500 chars (was 6293 observed in prod).
2. No Worker-Contract / 5-Min-Blocker / Output-Location-Regel boilerplate
   sneaks back in.
3. Essential per-task content (title, description, id, mc finish hint)
   is still present.

Plan: docs/superpowers/plans/2026-05-23-dispatch-message-refactor.md
Phase: 2 / Task 2.1
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.services.dispatch_message_builder import _format_dispatch_message
from app.services.task_context_builder import DispatchContext


@pytest.fixture
def researcher_subtask_ctx():
    """Realistic researcher subtask: parent set, short description, no project."""
    task = MagicMock()
    task.id = uuid.uuid4()
    task.board_id = uuid.uuid4()
    task.title = "Recherche: Top-3 ARM Cloud-Provider"
    task.description = (
        "Vergleiche AWS Graviton, Oracle Ampere, Hetzner CAX. "
        "Preise + Performance + Verfuegbarkeit."
    )
    task.priority = "medium"
    task.parent_task_id = uuid.uuid4()
    task.status = "inbox"
    task.target_url = None
    task.credentials_encrypted = None
    task.credential_id = None
    task.help_request_from = None
    task.workspace_path = None
    task.workspace_port = None
    task.acceptance_criteria = None
    task.intake_mode = None
    task.auto_reason = None
    task.dispatch_attempt_id = None

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Researcher"
    agent.role = "researcher"
    agent.is_board_lead = False
    agent.requires_git_workflow = False
    agent.rules_md = None
    agent.workspace_path = "/home/agent/workspace"

    ctx = DispatchContext(
        project=None,
        project_tags=[],
        dependency_context=None,
        semantic_memory_context=None,
        credentials_text=None,
        team_agents=[],
        child_tasks=[],
        feedback_context=None,
    )
    return task, agent, ctx


def test_researcher_subtask_dispatch_under_1500_chars(researcher_subtask_ctx):
    task, agent, ctx = researcher_subtask_ctx
    msg = _format_dispatch_message(task, agent, ctx)
    assert len(msg) <= 1500, (
        f"Researcher subtask dispatch is {len(msg)} chars, expected ≤ 1500.\n"
        f"--- rendered ---\n{msg}\n--- end ---"
    )


def test_researcher_subtask_dispatch_no_worker_contract(researcher_subtask_ctx):
    task, agent, ctx = researcher_subtask_ctx
    msg = _format_dispatch_message(task, agent, ctx)
    # Worker-Contract block markers — must NOT appear in dispatch
    assert "Worker-Contract" not in msg, "Worker-Contract header leaked into dispatch"
    assert "Task-Status ist die einzige Wahrheit" not in msg, (
        "Task-Status truth block leaked — should be in SOUL only"
    )
    assert "5-Minuten-Blocker-Regel" not in msg, (
        "5-Min-Blocker rule leaked — should be in SOUL only"
    )
    assert "Output-Location-Regel" not in msg, (
        "Output-Location rule leaked — should be in SOUL only"
    )


def test_researcher_subtask_dispatch_no_two_zone_convention(researcher_subtask_ctx):
    task, agent, ctx = researcher_subtask_ctx
    msg = _format_dispatch_message(task, agent, ctx)
    # Two-Zone block markers — must NOT appear (was in git_section additions)
    assert "Zwei-Zonen-Konvention" not in msg, (
        "Two-Zone-Convention leaked — should be in SOUL only"
    )


def test_researcher_subtask_dispatch_no_lifecycle_block(researcher_subtask_ctx):
    task, agent, ctx = researcher_subtask_ctx
    msg = _format_dispatch_message(task, agent, ctx)
    # The verbose `## Lifecycle` command catalog must NOT appear; the
    # single-line ACK reminder may.
    assert "## Lifecycle" not in msg, (
        "Lifecycle command catalog leaked — should be in SOUL only. "
        "Single-line ACK reminder is OK but no '## Lifecycle' header."
    )


def test_researcher_subtask_dispatch_contains_essentials(researcher_subtask_ctx):
    task, agent, ctx = researcher_subtask_ctx
    msg = _format_dispatch_message(task, agent, ctx)
    assert task.title in msg, "Task title missing"
    assert task.description in msg, "Task description missing"
    assert str(task.id) in msg, "Task-ID missing"
    # ACK reminder still present for inbox tasks
    assert "mc ack" in msg.lower() or "ACK" in msg, "ACK guidance missing"
