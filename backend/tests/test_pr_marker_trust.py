"""Round 8 / L6 — the merge only trusts the PR note MC itself wrote.

Bug (round-7 re-check, r1 N5): the four readers of the ``PR erstellt:`` note
(`task_lifecycle._merge_pr_if_exists`, `agent_git.handle_done_pr_merge`,
`adr_gate.pr_number_for_task`, `dispatch_message_builder._build_review_message`)
took the NEWEST comment that contained the text, from anyone. A chat reply, an
agent progress note or MC's own task-done summary (which quotes the last two
comments) that names another PR of the same repo made MC squash-merge THAT PR
when the card was approved.

The only writer of the real note is `agent_git.handle_review_pr_creation`:
``author_type="system"``, ``comment_type="progress"``, content starting with
``**PR erstellt:** <url>``, plus ``task.pr_number`` / ``task.pr_url``.

Sabotage in both directions:
  - newer chat_reply / agent / summary note with another PR → MC's own PR wins
  - only an agent-written note (no MC note) → no merge at all (no guess)
  - MC note and ``task.pr_number`` disagree → no merge (no guess)
  - the real MC note alone → merged as before
"""

import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.task import Task, TaskComment
from app.utils import utcnow

from tests.conftest import test_engine

REPO = "argyelan-ai/mission-control"
URL = f"https://github.com/{REPO}/pull"


async def _mk(objs: list):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in objs:
            s.add(o)
        await s.commit()
        for o in objs:
            await s.refresh(o)


async def _card(*, pr_number: int | None = None, with_project: bool = True) -> Task:
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    project_id = None
    if with_project:
        project = Project(
            id=uuid.uuid4(), board_id=board.id, name=f"Proj-{uuid.uuid4().hex[:5]}",
            github_repo_name=REPO, github_repo_url=f"https://github.com/{REPO}.git",
        )
        await _mk([project])
        project_id = project.id
    task = Task(id=uuid.uuid4(), board_id=board.id, project_id=project_id,
                title="Karte mit PR", status="review", pr_number=pr_number)
    await _mk([task])
    return task


def _system_marker(task_id, n: int, at) -> TaskComment:
    """Byte-identical to what handle_review_pr_creation writes."""
    return TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="system",
                       comment_type="progress", content=f"**PR erstellt:** {URL}/{n}",
                       created_at=at)


def _imposters(task_id, base) -> list[TaskComment]:
    """Every newer note that names another PR — none of them is MC's note."""
    return [
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="user",
                    comment_type="chat_reply",
                    content=f"Schau mal: PR erstellt: {URL}/2", created_at=base + timedelta(minutes=1)),
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="agent",
                    comment_type="progress",
                    content=f"**Update** — PR erstellt: {URL}/3", created_at=base + timedelta(minutes=2)),
        # an agent copying MC's exact wording is still not MC
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="agent",
                    comment_type="progress",
                    content=f"**PR erstellt:** {URL}/3", created_at=base + timedelta(minutes=2, seconds=30)),
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="agent",
                    comment_type="chat_reply",
                    content=f"**PR erstellt:** {URL}/4", created_at=base + timedelta(minutes=3)),
        # MC's own task-done summary quotes the last comments (auto_memory.py)
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="system",
                    comment_type="reflection",
                    content=f"**Task erledigt:** X\n**Letzte Kommentare:**\n- PR erstellt: {URL}/5",
                    created_at=base + timedelta(minutes=4)),
        # system, but not a creation note — same text in the middle
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="system",
                    comment_type="progress",
                    content=f"Hinweis: der Agent schrieb **PR erstellt:** {URL}/6",
                    created_at=base + timedelta(minutes=5)),
        # system, exact wording at the start, but not a progress note
        TaskComment(id=uuid.uuid4(), task_id=task_id, author_type="system",
                    comment_type="message",
                    content=f"**PR erstellt:** {URL}/6",
                    created_at=base + timedelta(minutes=6)),
    ]


def _merged_numbers(run_cmd: AsyncMock, merge_pr: AsyncMock) -> list[int]:
    out = [c.args[1] for c in merge_pr.await_args_list]
    for c in run_cmd.await_args_list:
        if c.args[:3] == ("gh", "pr", "merge"):
            out.append(int(c.args[3]))
    return out


async def _run_lifecycle_merge(task_id) -> list[int]:
    from app.services.git_service import git_service
    from app.services.task_lifecycle import _merge_pr_if_exists

    with patch.object(git_service, "_run_cmd", new=AsyncMock(return_value="")) as run_cmd, \
         patch.object(git_service, "merge_pr", new=AsyncMock(return_value=None)) as merge_pr:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await _merge_pr_if_exists(s, await s.get(Task, task_id), None)
    return _merged_numbers(run_cmd, merge_pr)


async def _run_agent_git_merge(task_id) -> list[int]:
    from app.routers.agent_git import handle_done_pr_merge
    from app.services.git_service import git_service

    reviewer = Agent(id=uuid.uuid4(), name="Rex", workspace_path=None)
    with patch.object(git_service, "_run_cmd", new=AsyncMock(return_value="")) as run_cmd, \
         patch.object(git_service, "merge_pr", new=AsyncMock(return_value=None)) as merge_pr, \
         patch.object(git_service, "cleanup_task_worktree", new=AsyncMock(return_value=None)):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await handle_done_pr_merge(s, await s.get(Task, task_id), reviewer)
    return _merged_numbers(run_cmd, merge_pr)


MERGERS = [_run_lifecycle_merge, _run_agent_git_merge]


# ── merge helpers ────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("merge", MERGERS)
async def test_newer_foreign_note_does_not_steer_the_merge(merge, fake_redis):
    task = await _card(pr_number=1)
    base = utcnow() - timedelta(hours=1)
    await _mk([_system_marker(task.id, 1, base), *_imposters(task.id, base)])
    assert await merge(task.id) == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", MERGERS)
async def test_only_agent_written_note_merges_nothing(merge, fake_redis, caplog):
    task = await _card(pr_number=None)
    base = utcnow() - timedelta(hours=1)
    await _mk(_imposters(task.id, base))
    with caplog.at_level(logging.WARNING):
        assert await merge(task.id) == []
    assert "not written by MC" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", MERGERS)
async def test_note_and_card_field_disagree_merges_nothing(merge, fake_redis, caplog):
    task = await _card(pr_number=9)
    await _mk([_system_marker(task.id, 1, utcnow() - timedelta(hours=1))])
    with caplog.at_level(logging.WARNING):
        assert await merge(task.id) == []
    assert "disagree" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", MERGERS)
async def test_real_note_alone_still_merges(merge, fake_redis):
    """Old cards that carry only MC's note (pr_number column still empty)."""
    task = await _card(pr_number=None)
    await _mk([_system_marker(task.id, 7, utcnow() - timedelta(hours=1))])
    assert await merge(task.id) == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", MERGERS)
async def test_newest_real_note_wins_after_a_second_pr(merge, fake_redis):
    """A re-review that opened a new PR writes a second MC note + field."""
    task = await _card(pr_number=8)
    base = utcnow() - timedelta(hours=1)
    await _mk([_system_marker(task.id, 7, base), _system_marker(task.id, 8, base + timedelta(minutes=1))])
    assert await merge(task.id) == [8]


# ── ADR gate: which PR it inspects ───────────────────────────────────────

@pytest.mark.asyncio
async def test_adr_gate_reads_the_same_trusted_note(fake_redis):
    from app.services.adr_gate import pr_number_for_task

    trusted = await _card(pr_number=None)
    base = utcnow() - timedelta(hours=1)
    await _mk([_system_marker(trusted.id, 1, base), *_imposters(trusted.id, base)])
    foreign_only = await _card(pr_number=None)
    await _mk(_imposters(foreign_only.id, base))

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await pr_number_for_task(s, await s.get(Task, trusted.id)) == 1
        assert await pr_number_for_task(s, await s.get(Task, foreign_only.id)) is None


# ── Reviewer brief: which PR it shows ────────────────────────────────────

@pytest.mark.asyncio
async def test_review_brief_shows_mcs_pr_not_a_quoted_one(fake_redis):
    from app.services.dispatch_message_builder import _build_review_message

    task = await _card(pr_number=1)
    base = utcnow() - timedelta(hours=1)
    await _mk([_system_marker(task.id, 1, base), *_imposters(task.id, base)])
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        msg = await _build_review_message(await s.get(Task, task.id), Agent(name="Rex"), s)
    section = msg.split("## Pull Request", 1)[1].split("##", 1)[0]
    assert f"{URL}/1" in section
    for n in (2, 3, 4, 5, 6):
        assert f"{URL}/{n}" not in section


@pytest.mark.asyncio
async def test_review_brief_falls_back_to_the_card_field(fake_redis):
    """Registry-repo cards carry the PR only in the field (agent PATCH)."""
    from app.services.dispatch_message_builder import _build_review_message

    task = await _card(pr_number=None, with_project=False)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.pr_number, t.pr_url = 11, f"{URL}/11"
        s.add(t)
        await s.commit()
        await _mk(_imposters(task.id, utcnow() - timedelta(hours=1)))
        msg = await _build_review_message(await s.get(Task, task.id), Agent(name="Rex"), s)
    section = msg.split("## Pull Request", 1)[1].split("##", 1)[0]
    assert f"{URL}/11" in section
    assert f"{URL}/2" not in section


# ── Proof gate: where it finds the card's PR ─────────────────────────────

@pytest.mark.asyncio
async def test_proof_gate_ignores_a_quoted_pr_note(fake_redis):
    """MC's task-done summary (system / reflection) quoting an agent's
    "PR erstellt:" line is not the card's PR for the proof gate either."""
    from app.services.proof_gate import _pr_tied_to_card, discover_pr

    base = utcnow() - timedelta(hours=1)
    quoted = await _card(pr_number=None)
    await _mk(_imposters(quoted.id, base))
    real = await _card(pr_number=1)
    await _mk([_system_marker(real.id, 1, base), *_imposters(real.id, base)])
    pr = {"title": "unrelated", "body": "no card id here"}

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        q = await s.get(Task, quoted.id)
        url, where = await discover_pr(s, q, "")
        assert where != "marker" and url is None
        for n in (2, 3, 4, 5, 6):
            assert await _pr_tied_to_card(s, q, pr, f"{URL}/{n}") is False

        r = await s.get(Task, real.id)
        assert await discover_pr(s, r, "") == (f"{URL}/1", "marker")
        assert await _pr_tied_to_card(s, r, pr, f"{URL}/1") is True
        assert await _pr_tied_to_card(s, r, pr, f"{URL}/5") is False
