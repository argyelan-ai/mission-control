"""Phase 3 — Budget enforcement test.

`_assemble_with_budget` existed since the file was written but was never
wired into the production code path (verified in plan V4: 0 production
callers). Phase 3 wires it live: msg_parts becomes list[DispatchSection]
with priority field; optional sections drop when total > HARD.

Plan: docs/superpowers/plans/2026-05-23-dispatch-message-refactor.md
Phase: 3 / Task 3.1
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.services.dispatch_message_builder import (
    DISPATCH_HARD_CHARS,
    _format_dispatch_message,
)
from app.services.task_context_builder import DispatchContext


def make_bloated_subtask_ctx():
    """Force a dispatch over HARD by inflating optional sections."""
    task = MagicMock()
    task.id = uuid.uuid4()
    task.board_id = uuid.uuid4()
    task.title = "Bloat test"
    # 1500 chars description (mandatory content)
    task.description = "DESCRIPTION " * 125  # ~1500 chars
    task.priority = "medium"
    task.parent_task_id = uuid.uuid4()  # subtask
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

    # Inflate optional sections to FORCE overflow:
    bloated_project = MagicMock()
    bloated_project.name = "BloatProject"
    bloated_project.description = "PROJ-DESC " * 200  # ~2000 chars
    bloated_project.workspace_path = "/host"
    bloated_project.github_repo_url = None
    bloated_project.github_repo_name = None
    bloated_project.project_config = None

    ctx = DispatchContext(
        project=bloated_project,
        project_tags=["bloat-tag"],
        dependency_context="DEP-CTX " * 250,  # ~2000 chars
        semantic_memory_context="MEM-HIT " * 100,  # ~800 chars
        credentials_text=None,
        team_agents=[],
        child_tasks=[],
        feedback_context=None,
    )
    return task, agent, ctx


def test_budget_keeps_mandatory_drops_optional():
    """Mandatory (title/description) must always be present.
    Optional (project, dependency, memory) drops in priority order."""
    task, agent, ctx = make_bloated_subtask_ctx()
    msg = _format_dispatch_message(task, agent, ctx)

    # Mandatory content survives no matter what
    assert task.title in msg, "title (mandatory) was dropped"
    assert task.description in msg, "description (mandatory) was dropped"
    assert str(task.id) in msg, "task id (mandatory) was dropped"

    # Either we stayed under HARD or we dropped optional content
    if len(msg) > DISPATCH_HARD_CHARS:
        # Over HARD — verify optionals dropped in priority order
        # (lowest priority drops first → memory first, then project, then deps)
        # At minimum, total content of optional sections should have been
        # reduced. If ALL optionals still present and over HARD, drop failed.
        mem_present = "MEM-HIT" in msg
        dep_present = "DEP-CTX" in msg
        proj_present = "PROJ-DESC" in msg
        all_present = mem_present and dep_present and proj_present
        assert not all_present, (
            f"Message is {len(msg)} chars > HARD {DISPATCH_HARD_CHARS} "
            f"but ALL optional sections still present — drop didn't fire. "
            f"mem={mem_present} dep={dep_present} proj={proj_present}"
        )


def test_budget_keeps_under_hard_when_possible():
    """If after dropping optionals we can fit under HARD, we should."""
    task, agent, ctx = make_bloated_subtask_ctx()
    msg = _format_dispatch_message(task, agent, ctx)

    # Description alone is ~1500 chars + ~1000 overhead = ~2500 chars mandatory
    # If we successfully drop the ~5000 chars of optionals, total should be
    # under HARD 4000.
    # Note: if mandatory itself > HARD, we accept that (can't help it).
    mandatory_estimate = len(task.description) + 1500  # rough mandatory floor
    if mandatory_estimate < DISPATCH_HARD_CHARS:
        assert len(msg) <= DISPATCH_HARD_CHARS, (
            f"Message is {len(msg)} chars > HARD {DISPATCH_HARD_CHARS}. "
            f"Mandatory estimate {mandatory_estimate} < HARD — drop should "
            f"have brought us back under."
        )
