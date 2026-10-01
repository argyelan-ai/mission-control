"""PR merge monitor: merged PRs close their review/user_test cards.

Regression for the 12 stale head cards: a head passes, the card lands in
review with ``pr_url``, the PR gets merged on GitHub — and the card sat in
review forever. The monitor polls GitHub for review/user_test cards with a
``pr_url`` and walks merged ones to done through the normal status path
(TaskEvent, actor = system). Open or closed-but-unmerged PRs are left alone;
without a configured GitHub token nothing is called at all (ADR-055).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_review_task(*, status: str = "review", pr_url: str | None = None,
                              n: int = 1) -> list:
    """Board + n tasks in the given status with the given pr_url."""
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Merge Monitor", slug=f"mm-{board_id.hex[:8]}"))
        tasks = []
        for i in range(n):
            task = Task(
                id=uuid.uuid4(),
                board_id=board_id,
                title=f"Card {i}",
                status=status,
                pr_url=pr_url,
            )
            s.add(task)
            tasks.append(task)
        await s.commit()
        for t in tasks:
            await s.refresh(t)
    return tasks


def _pr_url(n: int = 42) -> str:
    return f"https://github.com/acme/widgets/pull/{n}"


def _configured_github(monkeypatch) -> None:
    """ADR-055 env fallback: monitor only polls with owner+token set."""
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GH_TOKEN", "test-token")


# ── pr_url parsing ────────────────────────────────────────────────────


def test_parse_pr_url():
    from app.services.pr_merge_monitor import parse_pr_url
    assert parse_pr_url(_pr_url()) == ("acme", "widgets", 42)
    assert parse_pr_url("https://github.com/acme/widgets") is None
    assert parse_pr_url(None) is None
    assert parse_pr_url("https://gitlab.com/acme/widgets/merge_requests/1") is None


# ── merged → done ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merged_pr_moves_review_task_to_done(monkeypatch):
    _configured_github(monkeypatch)
    from app.models.task import TaskComment, TaskEvent
    from app.services.pr_merge_monitor import check_once

    task = (await _create_review_task(status="review", pr_url=_pr_url()))[0]
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        fetch.return_value = True
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 1
    fetch.assert_awaited_once_with("acme", "widgets", 42)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(type(task), task.id)
        assert fresh.status == "done"
        events = (await s.execute(
            __import__("sqlalchemy").select(TaskEvent).where(
                TaskEvent.task_id == task.id)
        )).scalars().all()
        assert any(
            e.from_status == "review" and e.to_status == "done"
            and e.changed_by == "system" and e.actor_label == "system"
            for e in events
        ), [f"{e.from_status}->{e.to_status} by {e.changed_by}" for e in events]
        comments = (await s.execute(
            __import__("sqlalchemy").select(TaskComment).where(
                TaskComment.task_id == task.id)
        )).scalars().all()
        assert any(c.comment_type == "resolution" for c in comments)


@pytest.mark.asyncio
async def test_merged_pr_moves_user_test_task_to_done(monkeypatch):
    _configured_github(monkeypatch)
    from app.services.pr_merge_monitor import check_once

    task = (await _create_review_task(status="user_test", pr_url=_pr_url(7)))[0]
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        fetch.return_value = True
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 1
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(type(task), task.id)
        assert fresh.status == "done"


# ── not merged → untouched ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_pr_leaves_task_alone(monkeypatch):
    _configured_github(monkeypatch)
    from app.services.pr_merge_monitor import check_once

    task = (await _create_review_task(status="review", pr_url=_pr_url()))[0]
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        fetch.return_value = False  # open or closed-unmerged
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 0
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(type(task), task.id)
        assert fresh.status == "review"


@pytest.mark.asyncio
async def test_github_error_leaves_task_alone(monkeypatch):
    _configured_github(monkeypatch)
    from app.services.pr_merge_monitor import check_once

    task = (await _create_review_task(status="review", pr_url=_pr_url()))[0]
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        fetch.return_value = None  # gh failed — unknown state
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 0
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(type(task), task.id)
        assert fresh.status == "review"


@pytest.mark.asyncio
async def test_unparseable_pr_url_skipped():
    from app.services.pr_merge_monitor import check_once

    task = (await _create_review_task(status="review", pr_url="https://github.com/acme/widgets"))[0]
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_tasks_without_pr_url_ignored():
    from app.services.pr_merge_monitor import check_once

    await _create_review_task(status="review", pr_url=None)
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_done_tasks_not_rechecked():
    from app.services.pr_merge_monitor import check_once

    await _create_review_task(status="done", pr_url=_pr_url())
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 0
    fetch.assert_not_awaited()


# ── rate limiting + config gate ───────────────────────────────────────


@pytest.mark.asyncio
async def test_checks_capped_per_cycle(monkeypatch):
    _configured_github(monkeypatch)
    from app.services import pr_merge_monitor as mon

    await _create_review_task(status="review", pr_url=_pr_url(), n=mon.MAX_PR_CHECKS_PER_CYCLE + 5)
    with patch.object(mon, "fetch_pr_merged", new_callable=AsyncMock) as fetch:
        fetch.return_value = True
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await mon.check_once(s)
    assert fetch.await_count == mon.MAX_PR_CHECKS_PER_CYCLE
    assert closed == mon.MAX_PR_CHECKS_PER_CYCLE


@pytest.mark.asyncio
async def test_no_token_no_github_calls(monkeypatch):
    from app.services.pr_merge_monitor import check_once

    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    await _create_review_task(status="review", pr_url=_pr_url())
    with patch(
        "app.services.pr_merge_monitor.fetch_pr_merged", new_callable=AsyncMock
    ) as fetch:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            closed = await check_once(s)
    assert closed == 0
    fetch.assert_not_awaited()


# ── fetch_pr_merged (gh subprocess, mocked at subprocess level) ───────


@pytest.mark.asyncio
async def test_fetch_pr_merged_true_false_error():
    from app.services import pr_merge_monitor as mon

    async def fake_run(*args, **kwargs):
        return 0, '{"state": "MERGED", "mergedAt": "2026-10-01T10:00:00Z"}', ""

    with patch.object(mon, "_run_gh", new=fake_run):
        assert await mon.fetch_pr_merged("acme", "widgets", 42) is True

    async def fake_run_open(*args, **kwargs):
        return 0, '{"state": "OPEN", "mergedAt": null}', ""

    with patch.object(mon, "_run_gh", new=fake_run_open):
        assert await mon.fetch_pr_merged("acme", "widgets", 42) is False

    async def fake_run_closed(*args, **kwargs):
        return 0, '{"state": "CLOSED", "mergedAt": null}', ""

    with patch.object(mon, "_run_gh", new=fake_run_closed):
        assert await mon.fetch_pr_merged("acme", "widgets", 42) is False

    async def fake_run_fail(*args, **kwargs):
        return 1, "", "gh: Not Found"

    with patch.object(mon, "_run_gh", new=fake_run_fail):
        assert await mon.fetch_pr_merged("acme", "widgets", 42) is None
