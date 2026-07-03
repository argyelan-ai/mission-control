"""Tests for Resilient Agent Recovery (Layered Recovery System)."""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.watchdog import WatchdogService


# ── Task 1: Error recovery in rules_md + dispatch message ──


@pytest.mark.asyncio
async def test_dispatch_message_contains_blocked_hint(
    session: AsyncSession, make_agent, make_task,
):
    """Workstream A2: the full Error-Recovery block moved to SOUL.md, but
    the dispatch message must still surface the `mc blocked` escape hatch
    so agents know what to do when they get stuck."""
    from app.services.dispatch import _build_dispatch_message

    board_id = uuid.uuid4()
    agent = await make_agent(
        "Cody", board_id=board_id, role="developer",
    )
    task = await make_task(
        board_id, title="Test Task", status="inbox",
        assigned_agent_id=agent.id,
    )

    msg = await _build_dispatch_message(task, agent, session)

    assert "mc blocked" in msg  # CLI escape hatch
    assert "blocked" in msg.lower()


@pytest.mark.asyncio
async def test_cody_rules_contain_error_recovery():
    """Cody rules_md must have an error recovery block."""
    from app.routers.agents import AGENT_CONFIGS

    cody_config = AGENT_CONFIGS["cody"]
    rules = cody_config["rules_md"]

    assert "When you are stuck" in rules
    assert "blocked" in rules
    assert "NEVER" in rules


@pytest.mark.asyncio
async def test_rex_rules_contain_error_recovery():
    """Rex rules_md must have an error recovery block."""
    from app.routers.agents import AGENT_CONFIGS

    rex_config = AGENT_CONFIGS["rex"]
    rules = rex_config["rules_md"]

    assert "When you are stuck" in rules
    assert "blocked" in rules


@pytest.mark.asyncio
async def test_henry_rules_contain_error_recovery():
    """Henry rules_md must have an error recovery block."""
    from app.routers.agents import AGENT_CONFIGS

    henry_config = AGENT_CONFIGS["henry"]
    rules = henry_config["rules_md"]

    assert "When you are stuck" in rules
    assert "blocked" in rules
    assert "NEVER" in rules


@pytest.mark.asyncio
async def test_all_specialized_agents_have_error_recovery():
    """All SPECIALIZED_AGENTS_SPECS must have error recovery."""
    from app.routers.agents import SPECIALIZED_AGENTS_SPECS

    for spec in SPECIALIZED_AGENTS_SPECS:
        rules = spec.get("rules_md")
        assert rules is not None, f"{spec['name']} hat kein rules_md"
        assert "When you are stuck" in rules, f"{spec['name']} fehlt Error-Recovery"
        assert "blocked" in rules, f"{spec['name']} fehlt 'blocked' in Error-Recovery"


def test_rules_md_in_config_file_types_after_gateway_sunset():
    """Phase 29: _GATEWAY_SYNC_FILE_TYPES was removed from agents.py (gateway path gone).
    rules_md remains available in CONFIG_FILE_TYPES for UI access, though.
    """
    from app.routers.agents import CONFIG_FILE_TYPES

    assert "rules_md" in CONFIG_FILE_TYPES  # in CONFIG_FILE_TYPES for UI access


# ── Task 2: Session health monitor ──


@pytest.mark.asyncio
async def test_build_recovery_recap(session: AsyncSession, make_agent, make_task):
    """Recovery recap must contain task ID, title, and instructions."""
    board_id = uuid.uuid4()
    agent = await make_agent("Cody", board_id=board_id, role="developer")
    task = await make_task(
        board_id, title="Build Feature X", status="in_progress",
        assigned_agent_id=agent.id,
    )

    wd = WatchdogService()
    recap = await wd._build_recovery_recap(task, agent, session)

    assert "Build Feature X" in recap
    assert str(task.id) in recap
    assert "weiter" in recap.lower()


@pytest.mark.asyncio
async def test_build_recovery_recap_with_workspace(
    session: AsyncSession, make_agent, make_task,
):
    """Recovery recap with a workspace path must include it."""
    board_id = uuid.uuid4()
    agent = await make_agent(
        "Cody", board_id=board_id, role="developer",
        workspace_path="/home/henry/.openclaw/workspace-cody",
    )
    task = await make_task(
        board_id, title="Build Feature X", status="in_progress",
        assigned_agent_id=agent.id,
    )

    wd = WatchdogService()
    recap = await wd._build_recovery_recap(task, agent, session)

    assert "workspace-cody" in recap
    assert "Build Feature X" in recap


@pytest.mark.asyncio
async def test_build_recovery_recap_uses_project_workspace(
    session: AsyncSession, make_agent, make_task,
):
    """Recovery recap prefers project.workspace_path over agent.workspace_path."""
    from app.models.board import Project

    board_id = uuid.uuid4()
    project = Project(
        board_id=board_id, name="Portfolio Website",
        workspace_path="/private/tmp/demo-portfolio",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)

    agent = await make_agent(
        "Cody", board_id=board_id, role="developer",
        workspace_path="/home/henry/.openclaw/workspace-cody",
    )
    task = await make_task(
        board_id, title="Build Feature X", status="in_progress",
        assigned_agent_id=agent.id, project_id=project.id,
    )

    wd = WatchdogService()
    recap = await wd._build_recovery_recap(task, agent, session)

    # Project workspace must be included, NOT agent workspace
    assert "demo-portfolio" in recap
    assert "workspace-cody" not in recap


# ── Task 2/3 dropped (Phase 29): _check_session_health + _escalate_to_lead
# were gateway-only and were removed with the gateway sunset. Stale-task
# ownership now lives in task_runner._check_dispatch_ack (Phase 26).


# ── Task 4: Improved approval format ──


def test_agent_stuck_approval_format():
    """agent_stuck approval description must be human-readable."""
    from app.services.task_runner import _build_agent_stuck_description

    desc = _build_agent_stuck_description(
        task_title="Projekt-Showcase mit interaktiven Cards",
        agent_name="Cody",
        error_summary="TypeScript-Fehler in vitest.setup.tsx:28 — children ist unknown statt ReactNode",
        timeline_events=[
            ("09:08", "Task gestartet"),
            ("09:15", "Session neugestartet (leere Antwort)"),
            ("09:25", "Henry informiert — keine Reaktion"),
        ],
    )

    assert "Cody" in desc
    assert "Projekt-Showcase" in desc
    assert "TypeScript" in desc
    assert "09:08" in desc
    assert "zuweisen" in desc


# ── Task 5: Telegram push ──
# test_send_agent_stuck_telegram removed (Phase 29 / Wave 4 cleanup):
# `app.services.telegram` and `send_telegram_notification` no longer
# exist — Telegram push now runs directly via `telegram_bot.send_telegram_*`
# in `task_runner._create_dispatch_approval` (see Bug 18 / D-2 commit
# 3da66c22). The test covered a gateway RPC bridge pattern that died with
# the Openclaw sunset (Phase 29).
