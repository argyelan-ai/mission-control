"""bench_studio orchestrator — state machine (advance, render, compose,
task_done hook, partial failure -> grid from survivors)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator
from tests.conftest import test_engine


async def _seed(session, *, mode="side_by_side", entry_specs=None):
    ch = BenchChallenge(title="T", prompt_text="p", mode=mode)
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = []
    for spec in entry_specs or []:
        e = BenchEntry(challenge_id=ch.id, **spec)
        session.add(e)
        entries.append(e)
    await session.commit()
    for e in entries:
        await session.refresh(e)
    return ch, entries


def _record_ok(monkeypatch):
    async def fake_record(entry):
        return {
            "video_path": f"/shared-deliverables/bench-x/{entry.model_label}/clip.mp4",
            "screenshot_path": f"/shared-deliverables/bench-x/{entry.model_label}/shot.png",
        }

    monkeypatch.setattr(orchestrator, "record_entry", fake_record)


@pytest.mark.asyncio
async def test_maybe_advance_waits_for_pending_entries(session, monkeypatch):
    ch, _ = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "generated"},
            {"model_label": "B", "source_kind": "agent", "status": "generating"},
        ],
    )
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock())
    await orchestrator.maybe_advance(session, ch.id)
    await session.refresh(ch)
    assert ch.status == "generating"
    orchestrator.record_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_advance_renders_composes_and_reaches_review(session, monkeypatch):
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/a/index.html"},
            {"model_label": "B", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/b/index.html"},
        ],
    )
    _record_ok(monkeypatch)
    compose_mock = AsyncMock(return_value="/shared-deliverables/bench-x/grid.mp4")
    monkeypatch.setattr(orchestrator, "compose_challenge", compose_mock)

    await orchestrator.maybe_advance(session, ch.id)
    await session.refresh(ch)

    assert ch.status == "review"
    assert ch.composed_video_path == "/shared-deliverables/bench-x/grid.mp4"
    for e in entries:
        await session.refresh(e)
        assert e.status == "rendered"
        assert e.video_path is not None
    compose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_failure_grid_from_survivors(session, monkeypatch):
    """One entry failed in generation, one render fails -> grid is built from
    the remaining survivor(s); challenge still reaches review (spec §4)."""
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/a/index.html"},
            {"model_label": "B", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/b/index.html"},
            {"model_label": "C", "source_kind": "spark", "status": "failed",
             "error": "generation failed: timeout"},
        ],
    )

    async def flaky_record(entry):
        if entry.model_label == "B":
            raise RuntimeError("ffmpeg exploded")
        return {"video_path": "/sd/a.mp4", "screenshot_path": "/sd/a.png"}

    monkeypatch.setattr(orchestrator, "record_entry", flaky_record)
    compose_mock = AsyncMock(return_value="/sd/grid.mp4")
    monkeypatch.setattr(orchestrator, "compose_challenge", compose_mock)

    await orchestrator.maybe_advance(session, ch.id)
    await session.refresh(ch)

    assert ch.status == "review"
    # Only 1 survivor -> side_by_side compose is skipped (single video stands in)
    compose_mock.assert_not_awaited()
    assert ch.composed_video_path is None
    statuses = {}
    for e in entries:
        await session.refresh(e)
        statuses[e.model_label] = e.status
    assert statuses == {"A": "rendered", "B": "failed", "C": "failed"}


@pytest.mark.asyncio
async def test_all_entries_failed_challenge_failed(session, monkeypatch):
    ch, _ = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "failed"},
            {"model_label": "B", "source_kind": "spark", "status": "failed"},
        ],
    )
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock())
    await orchestrator.maybe_advance(session, ch.id)
    await session.refresh(ch)
    assert ch.status == "failed"
    assert "all entries failed" in ch.error


@pytest.mark.asyncio
async def test_compose_failure_fails_challenge(session, monkeypatch):
    ch, _ = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/a/index.html"},
            {"model_label": "B", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/b/index.html"},
        ],
    )
    _record_ok(monkeypatch)
    monkeypatch.setattr(
        orchestrator, "compose_challenge", AsyncMock(side_effect=RuntimeError("no ffmpeg"))
    )
    await orchestrator.maybe_advance(session, ch.id)
    await session.refresh(ch)
    assert ch.status == "failed"
    assert "compose failed" in ch.error


@pytest.mark.asyncio
async def test_single_mode_skips_compose(session, monkeypatch):
    ch, entries = await _seed(
        session,
        mode="single",
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "generated",
             "artifact_path": "/tmp/a/index.html"},
        ],
    )
    _record_ok(monkeypatch)
    compose_mock = AsyncMock()
    monkeypatch.setattr(orchestrator, "compose_challenge", compose_mock)
    await orchestrator.maybe_advance(session, ch.id)
    await session.refresh(ch)
    assert ch.status == "review"
    compose_mock.assert_not_awaited()


# ── task_done hook ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_task_done_collects_html_deliverable(
    session, tmp_path, monkeypatch, make_board, make_task
):
    from app.models.deliverable import TaskDeliverable

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    now = datetime.now(timezone.utc)
    task = await make_task(
        board.id,
        title="[Bench] one-shot",
        status="done",
        dispatched_at=now - timedelta(seconds=42),
        completed_at=now,
    )

    src = tmp_path / "agent-out" / "index.html"
    src.parent.mkdir(parents=True)
    src.write_text("<html><body>agent</body></html>")
    session.add(TaskDeliverable(task_id=task.id, deliverable_type="file",
                                title="index.html", path=str(src)))

    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "Claude", "source_kind": "agent",
             "status": "generating", "task_id": task.id},
        ],
    )
    # Prevent the advance cascade from hitting real HTTP:
    monkeypatch.setattr(orchestrator, "record_entry",
                        AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"}))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    await orchestrator.on_task_done(session, task)

    entry = entries[0]
    await session.refresh(entry)
    assert entry.artifact_path == str(tmp_path / f"bench-{ch.id}" / "Claude" / "index.html")
    assert (tmp_path / f"bench-{ch.id}" / "Claude" / "index.html").read_text() == \
        "<html><body>agent</body></html>"
    assert entry.metrics["duration_ms"] == 42000
    await session.refresh(ch)
    assert ch.status == "review"  # advance cascade ran


@pytest.mark.asyncio
async def test_on_task_done_ignores_unrelated_tasks(session, make_board, make_task):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="normal task", status="done")
    # Must be a silent no-op:
    await orchestrator.on_task_done(session, task)


@pytest.mark.asyncio
async def test_on_task_done_without_deliverable_fails_entry(
    session, tmp_path, monkeypatch, make_board, make_task
):
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="[Bench] x", status="done")
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "Claude", "source_kind": "agent",
             "status": "generating", "task_id": task.id},
        ],
    )
    await orchestrator.on_task_done(session, task)
    await session.refresh(entries[0])
    assert entries[0].status == "failed"
    assert "no index.html deliverable" in entries[0].error


# ── dispatch_agent_entry ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_agent_entry_creates_task_and_dispatches(
    session, monkeypatch, make_board, make_agent
):
    from app.models.task import Task

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(name="Cody", board_id=board.id)
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "Claude", "source_kind": "agent", "agent_id": agent.id},
        ],
    )
    dispatched = []

    def fake_create_task(coro):
        coro.close()  # don't actually run auto_dispatch_task
        dispatched.append(True)

    monkeypatch.setattr(orchestrator.asyncio, "create_task", fake_create_task)

    await orchestrator.dispatch_agent_entry(session, entries[0], ch)
    await session.refresh(entries[0])

    assert entries[0].status == "generating"
    assert entries[0].task_id is not None
    assert dispatched == [True]
    task = await session.get(Task, entries[0].task_id)
    assert task.assigned_agent_id == agent.id
    assert task.is_auto_created is True
    assert task.human_review_required is True  # bench: human judges, never the lead
    assert "index.html" in task.description
    assert ch.prompt_text in task.description


@pytest.mark.asyncio
async def test_dispatch_agent_entry_without_board_fails_entry(session, make_agent):
    agent = await make_agent(name="Loose", board_id=None)
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "X", "source_kind": "agent", "agent_id": agent.id},
        ],
    )
    await orchestrator.dispatch_agent_entry(session, entries[0], ch)
    await session.refresh(entries[0])
    assert entries[0].status == "failed"
    assert "board" in entries[0].error


# ── format_speed_label / reconcile ────────────────────────────────────────


def test_format_speed_label():
    assert orchestrator.format_speed_label({"duration_ms": 42000}) == "42 s"
    assert orchestrator.format_speed_label(
        {"duration_ms": 42000, "tok_per_s": 87.3}
    ) == "42 s · 87 tok/s"
    assert orchestrator.format_speed_label({}) == ""


@pytest.mark.asyncio
async def test_reconcile_marks_failed_agent_tasks(
    session, monkeypatch, make_board, make_task
):
    """Failed agent tasks never fire the task_done hook — the GET-time
    reconcile sweeps them so nothing hangs silently."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="[Bench] x", status="failed")
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "Claude", "source_kind": "agent",
             "status": "generating", "task_id": task.id},
        ],
    )
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock())
    await orchestrator.reconcile_challenge(session, ch, entries)
    await session.refresh(entries[0])
    await session.refresh(ch)
    assert entries[0].status == "failed"
    assert ch.status == "failed"  # only entry failed -> all failed


# ── rerender_challenge outer exception handler ─────────────────────────────


@pytest.mark.asyncio
async def test_rerender_challenge_crash_writes_failed_status(session, monkeypatch):
    """Outer exception handler in rerender_challenge must flip to failed,
    not just log — same pattern as start_challenge (Task 4 review fix)."""
    # rerender_challenge creates its own AsyncSession(engine); patch it to the
    # test engine so the failure write lands in the same in-memory SQLite DB.
    monkeypatch.setattr("app.database.engine", test_engine)

    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark",
             "status": "rendered", "artifact_path": "/a/index.html",
             "video_path": "/a/clip.mp4"},
        ],
    )
    ch.status = "rendering"
    session.add(ch)
    await session.commit()

    # Make _render_and_compose crash so the outer handler fires.
    monkeypatch.setattr(
        orchestrator, "_render_and_compose",
        AsyncMock(side_effect=RuntimeError("injected crash")),
    )

    await orchestrator.rerender_challenge(ch.id)

    # Verify via a fresh session (rerender_challenge's own session committed the status).
    async with AsyncSession(test_engine, expire_on_commit=False) as verify_session:
        updated_ch = await verify_session.get(BenchChallenge, ch.id)
    assert updated_ch is not None
    assert updated_ch.status == "failed"
    assert updated_ch.error is not None


# ── FK flush-order regression (Postgres-only bug, invisible with FKs off) ──


@pytest.mark.asyncio
async def test_dispatch_agent_entry_survives_fk_enforcement(monkeypatch):
    """dispatch_agent_entry must INSERT the Task before the bench_entries
    UPDATE that references it. There is no relationship() between the two
    mappers, so the unit of work has no dependency edge — without an explicit
    flush the UPDATE can run first, a ForeignKeyViolation on Postgres
    (2026-07-12 incident: first live challenge stuck in 'generating').
    The shared test engine runs with FKs off (see conftest note), so this
    test uses its own SQLite engine with PRAGMA foreign_keys=ON.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel

    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    fk_engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(fk_engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with fk_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    try:
        async with AsyncSession(fk_engine, expire_on_commit=False) as session:
            board = Board(id=uuid.uuid4(), name="B", slug="b-fk")
            session.add(board)
            await session.commit()
            agent = Agent(id=uuid.uuid4(), name="Cody", board_id=board.id,
                          agent_runtime="cli-bridge")
            session.add(agent)
            await session.commit()
            ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
            session.add(ch)
            await session.commit()
            entry = BenchEntry(challenge_id=ch.id, model_label="Claude",
                               source_kind="agent", agent_id=agent.id)
            session.add(entry)
            await session.commit()

            # Neutralize the fire-and-forget dispatch (orchestrator does a
            # function-local import, so patch the source module). Patching
            # asyncio.create_task itself would break AsyncSession.__aexit__.
            monkeypatch.setattr(
                "app.services.dispatch.auto_dispatch_task", AsyncMock()
            )

            await orchestrator.dispatch_agent_entry(session, entry, ch)
            await session.refresh(entry)

            assert entry.status == "generating"
            assert entry.task_id is not None
            task = await session.get(Task, entry.task_id)
            assert task is not None
    finally:
        await fk_engine.dispose()
