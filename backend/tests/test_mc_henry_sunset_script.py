"""Phase 28 Plan 28-03 - Script-level tests for mc_henry_sunset.py.

Three test layers:
  1. Markdown rendering (pure function, no DB).
  2. Dry-run integration: assert no DB mutations + Discord called.
  3. Commit integration: assert subprocess invoked + Discord called.

D-02: dry-run must be read-only (verifiable by row-count diff).
D-03/D-04: commit invokes alembic + Discord; failure posts critical alert.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text as sa_text
from sqlmodel.ext.asyncio.session import AsyncSession

from scripts.mc_henry_sunset import (
    HenryFootprint,
    _commit,
    _dry_run,
    _render_dry_run_md,
)


# ===== Markdown rendering (pure function) =================================

def test_render_md_with_full_footprint():
    """Full footprint produces a well-structured Markdown report."""
    fp = HenryFootprint(
        henry_id="abc12345", boss_id="def67890",
        tasks_assigned=[
            ("a1" * 4, "Task one", "inbox"),
            ("a2" * 4, "Task two", "in_progress"),
        ],
        tasks_callback=[],
        tasks_owner=[("a3" * 4, "Task three", "review")],
        comments_to_null=12,
        events_to_null=47,
        current_task_holders=[("cody", "deadbeef" + "11" * 12)],
        boss_state={
            "provision_status": "provisioned",
            "status": "idle",
            "scopes_count": 16,
            "scopes_meaning": "16/16",
        },
        henry_discord_channel_id="999888777",
        non_henry_discord_channels=10,
    )
    md = _render_dry_run_md(fp)
    assert "## Henry-Sunset" in md
    assert "Task one" in md
    assert "Task three" in md
    assert "12" in md and "47" in md  # comment + event counts
    assert "OK Boss provision_status" in md
    assert "Cross-agent current_task_id" in md
    assert "cody" in md
    assert "999888777" in md
    assert "DRY RUN" in md or "dry run" in md.lower()
    assert "Pre-Flight Check" in md
    assert "Tasks to reassign to Boss" in md


def test_render_md_when_boss_missing():
    """Boss missing produces a clear ABORT Pre-Flight failure block."""
    fp = HenryFootprint(
        henry_id="abc", boss_id=None,
        tasks_assigned=[], tasks_callback=[], tasks_owner=[],
        comments_to_null=0, events_to_null=0,
        current_task_holders=[], boss_state=None,
        henry_discord_channel_id=None,
        non_henry_discord_channels=0,
    )
    md = _render_dry_run_md(fp)
    assert "Boss agent not found" in md
    assert "MIGRATION WILL ABORT" in md


def test_render_md_truncates_long_task_lists():
    """If > 20 tasks, table truncates to 20 + 'N more rows' line."""
    tasks = [(f"t{i:04d}" * 2, f"Task {i}", "inbox") for i in range(35)]
    fp = HenryFootprint(
        henry_id="abc", boss_id="def",
        tasks_assigned=tasks, tasks_callback=[], tasks_owner=[],
        comments_to_null=0, events_to_null=0,
        current_task_holders=[],
        boss_state={
            "provision_status": "provisioned", "status": "idle",
            "scopes_count": 0, "scopes_meaning": "ALL_SCOPES",
        },
        henry_discord_channel_id=None,
        non_henry_discord_channels=5,
    )
    md = _render_dry_run_md(fp)
    assert "15 more rows truncated" in md


# ===== Dry-run integration ================================================

@pytest.mark.asyncio
async def test_dry_run_no_db_mutations(make_board, make_agent, make_task):
    """Dry-run does NOT alter any row in agents/tasks/task_comments.

    Concrete proof of D-02 via before/after row count snapshots.
    """
    from tests.conftest import test_engine

    # Seed Henry + Boss + a Henry-assigned task.
    board = await make_board()
    boss = await make_agent(
        name="Boss", board_id=board.id,
        provision_status="provisioned",
        is_board_lead=False, agent_runtime="host", scopes=[],
    )
    henry = await make_agent(
        name="Henry", board_id=board.id,
        provision_status="provisioned",
        is_board_lead=True, agent_runtime="openclaw",
    )
    await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=henry.id, title="Test",
    )

    # Snapshot row counts before dry-run.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        before_agents = (await s.exec(sa_text(
            "SELECT count(*) FROM agents"
        ))).scalar_one()
        before_tasks_henry = (await s.exec(sa_text(
            "SELECT count(*) FROM tasks WHERE assigned_agent_id = :hid"
        ).bindparams(hid=str(henry.id).replace("-", "")))).scalar_one()

    # Patch the engine to use the test engine + mock Discord.
    # patch where the symbol is used (scripts.mc_henry_sunset.send_discord_notification),
    # not where it's defined (PATTERNS.md gotcha #3).
    with patch("scripts.mc_henry_sunset.send_discord_notification",
               new_callable=AsyncMock) as mock_discord:
        with patch("app.database.engine", test_engine):
            exit_code = await _dry_run()

    assert exit_code == 0

    # Snapshot row counts after dry-run - must be identical.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after_agents = (await s.exec(sa_text(
            "SELECT count(*) FROM agents"
        ))).scalar_one()
        after_tasks_henry = (await s.exec(sa_text(
            "SELECT count(*) FROM tasks WHERE assigned_agent_id = :hid"
        ).bindparams(hid=str(henry.id).replace("-", "")))).scalar_one()

    assert before_agents == after_agents, "Dry-run mutated agents table"
    assert before_tasks_henry == after_tasks_henry, "Dry-run mutated tasks"

    # Sanity: Boss row still has is_board_lead=False (not promoted by dry-run).
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        boss_is_lead = (await s.exec(sa_text(
            "SELECT is_board_lead FROM agents WHERE name = 'Boss'"
        ))).scalar_one()
    assert bool(boss_is_lead) is False, "Dry-run must NOT promote Boss"

    # Discord called exactly once with warning severity.
    assert mock_discord.await_count == 1
    call = mock_discord.await_args
    assert "Dry-Run" in call.kwargs["title"]
    assert call.kwargs["severity"] == "warning"


# ===== Commit integration ================================================

@pytest.mark.asyncio
async def test_commit_invokes_alembic_and_notifies_on_success():
    """--commit shells out to alembic upgrade head and posts to Discord."""
    fake_proc = MagicMock(returncode=0, stdout="ok\n", stderr="")

    # _commit calls _gather_footprint AFTER the subprocess succeeds (post-state
    # for the confirmation embed). Mock both to keep the test free of seed
    # complexity.
    with patch("scripts.mc_henry_sunset.subprocess.run",
               return_value=fake_proc) as mock_run, \
         patch("scripts.mc_henry_sunset.send_discord_notification",
               new_callable=AsyncMock) as mock_discord, \
         patch("scripts.mc_henry_sunset._gather_footprint",
               new_callable=AsyncMock) as mock_gather:
        mock_gather.return_value = HenryFootprint(
            henry_id=None,    # post-migration: Henry gone
            boss_id="def67890",
            tasks_assigned=[], tasks_callback=[], tasks_owner=[],
            comments_to_null=0, events_to_null=0,
            current_task_holders=[],
            boss_state={
                "provision_status": "provisioned",
                "status": "idle",
                "scopes_count": 0,
                "scopes_meaning": "ALL_SCOPES",
            },
            henry_discord_channel_id=None,
            non_henry_discord_channels=10,
        )
        exit_code = await _commit()

    assert exit_code == 0
    # alembic invoked exactly once with upgrade head (argv-list form).
    assert mock_run.call_count == 1
    call_args = mock_run.call_args.args[0]
    assert "alembic" in call_args
    assert "upgrade" in call_args
    assert "head" in call_args

    # Discord notified with success.
    assert mock_discord.await_count == 1
    assert "applied" in mock_discord.await_args.kwargs["title"].lower()


@pytest.mark.asyncio
async def test_commit_alerts_on_alembic_failure():
    """If alembic exits non-zero, post critical alert + return 1.

    Proves D-04 failure path: severity=critical Discord alert + non-zero
    exit code so a CI/operator script can detect the failure.
    """
    fake_proc = MagicMock(returncode=1, stdout="", stderr="boom: FK violation")

    with patch("scripts.mc_henry_sunset.subprocess.run",
               return_value=fake_proc), \
         patch("scripts.mc_henry_sunset.send_discord_notification",
               new_callable=AsyncMock) as mock_discord:
        exit_code = await _commit()

    assert exit_code == 1
    assert mock_discord.await_count == 1
    kwargs = mock_discord.await_args.kwargs
    assert kwargs["severity"] == "critical"
    assert "FAILED" in kwargs["title"]
    assert "boom" in kwargs["description"]
