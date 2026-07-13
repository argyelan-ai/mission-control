"""bench_studio orchestrator — state machine (advance, render, compose,
task_done hook, partial failure -> grid from survivors)."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# ── /compose response contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_challenge_parses_sidecar_response(session, monkeypatch):
    """The sidecar's ComposeResponse names the field `output_path` (see
    docker/mc-playwright/media.py) — NOT `video_path` like RecordResponse.
    compose_challenge crashed with KeyError('video_path') on the first live
    side-by-side run (2026-07-12); existing flow tests mock compose_challenge
    away, so pin the parsing contract here."""

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            # Exact shape of media.ComposeResponse
            return {"output_path": "/shared-deliverables/bench-x/grid.mp4",
                    "bytes": 123, "inputs": 2}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            assert url.endswith("/compose")
            assert set(json) >= {"inputs", "labels", "layout", "output_path"}
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)

    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    ch.id = uuid.uuid4()
    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="spark",
                   status="rendered", video_path="/sd/a.mp4"),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="spark",
                   status="rendered", video_path="/sd/b.mp4"),
    ]
    result = await orchestrator.compose_challenge(session, ch, entries)
    assert result == "/shared-deliverables/bench-x/grid.mp4"


# ── branding payload (video-branding, 2026-07-12) ───────────────────────────


def _fake_compose_client(monkeypatch) -> dict:
    """Patches orchestrator httpx.AsyncClient with a capturing fake; returns
    the dict that receives the posted /compose json payload."""
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": "/sd/branded.mp4", "bytes": 1, "inputs": 2}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured.update(json)
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)
    return captured


@pytest.mark.asyncio
async def test_compose_challenge_sends_branding_for_two_entries(session, monkeypatch, make_agent, make_board):
    """side_by_side + exactly 2 rendered entries -> compose_challenge attaches
    a `branding` payload with title/run_label/models/outro_rows shape. Tags:
    spark -> "VLLM · SPARK", agent -> harness uppercased."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(board_id=board.id, name="Grok-Agent", harness="grok")

    ch = BenchChallenge(title="SVG timeline animation", prompt_text="  \nBuild a timeline.\nSecond line.",
                         mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    entries = [
        BenchEntry(challenge_id=ch.id, model_label="Qwen 3.6 35B A3B", source_kind="spark",
                   status="rendered", video_path="/sd/qwen.mp4",
                   metrics={"duration_ms": 768000}, artifact_path=None),
        BenchEntry(challenge_id=ch.id, model_label="Grok 4.5", source_kind="agent",
                   agent_id=agent.id, status="rendered", video_path="/sd/grok.mp4",
                   metrics={"duration_ms": 564000}, artifact_path=None),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _fake_compose_client(monkeypatch)

    result = await orchestrator.compose_challenge(session, ch, entries)
    assert result == "/sd/branded.mp4"

    branding = captured["branding"]
    assert branding["title"] == "SVG timeline animation"
    assert branding["run_label"] == "001"
    assert branding["prompt_line"] == "Build a timeline."

    models_by_label = {m["label"]: m["tag"] for m in branding["models"]}
    assert models_by_label["Grok 4.5"] == "GROK"  # harness "grok" uppercased
    assert models_by_label["Qwen 3.6 35B A3B"] == "VLLM · SPARK"

    rows_by_name = {r["name"]: r for r in branding["outro_rows"]}
    assert rows_by_name["Qwen 3.6 35B A3B"]["time"] == "12.8 min"
    assert rows_by_name["Grok 4.5"]["time"] == "9.4 min"
    # No artifact_path on either entry here -> size falls back to "—"
    assert rows_by_name["Qwen 3.6 35B A3B"]["size"] == "—"


@pytest.mark.asyncio
async def test_compose_challenge_display_tag_override_wins(session, monkeypatch, make_agent, make_board):
    """entry.display_tag beats every derived default — for spark AND agent."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(board_id=board.id, name="Grok-Agent", harness="grok")

    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="spark",
                   status="rendered", video_path="/sd/a.mp4",
                   display_tag="OMP · DGX SPARK"),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="agent",
                   agent_id=agent.id, status="rendered", video_path="/sd/b.mp4",
                   display_tag="FRONTIER · API"),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _fake_compose_client(monkeypatch)
    await orchestrator.compose_challenge(session, ch, entries)

    models_by_label = {m["label"]: m["tag"] for m in captured["branding"]["models"]}
    assert models_by_label["A"] == "OMP · DGX SPARK"
    assert models_by_label["B"] == "FRONTIER · API"


@pytest.mark.asyncio
async def test_compose_challenge_harness_default_and_name_fallback(session, monkeypatch, make_agent, make_board):
    """No display_tag: agent tag comes from the harness; agent NAME is only
    the fallback when the agent has no harness set."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    with_harness = await make_agent(board_id=board.id, name="Sparky", harness="omp")
    without_harness = await make_agent(board_id=board.id, name="Boss")

    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="agent",
                   agent_id=with_harness.id, status="rendered", video_path="/sd/a.mp4"),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="agent",
                   agent_id=without_harness.id, status="rendered", video_path="/sd/b.mp4"),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _fake_compose_client(monkeypatch)
    await orchestrator.compose_challenge(session, ch, entries)

    models_by_label = {m["label"]: m["tag"] for m in captured["branding"]["models"]}
    assert models_by_label["A"] == "OMP"   # harness wins
    assert models_by_label["B"] == "BOSS"  # name fallback


@pytest.mark.asyncio
async def test_compose_challenge_no_branding_for_three_entries(session, monkeypatch):
    """More than 2 rendered entries -> plain grid path, no `branding` key at all."""
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    entries = [
        BenchEntry(challenge_id=ch.id, model_label=label, source_kind="spark",
                   status="rendered", video_path=f"/sd/{label}.mp4")
        for label in ("A", "B", "C")
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": "/sd/grid.mp4", "bytes": 1, "inputs": 3}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured.update(json)
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)

    result = await orchestrator.compose_challenge(session, ch, entries)
    assert result == "/sd/grid.mp4"
    assert "branding" not in captured


@pytest.mark.asyncio
async def test_compose_challenge_artifact_size_in_outro_row(session, tmp_path, monkeypatch):
    """artifact_path -> real file size in KB via Path().stat()."""
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    artifact = tmp_path / "index.html"
    artifact.write_bytes(b"x" * 2048)  # exactly 2 KB

    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="spark",
                   status="rendered", video_path="/sd/a.mp4", artifact_path=str(artifact)),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="spark",
                   status="rendered", video_path="/sd/b.mp4", artifact_path="/does/not/exist.html"),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": "/sd/branded.mp4", "bytes": 1, "inputs": 2}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured.update(json)
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)

    await orchestrator.compose_challenge(session, ch, entries)
    rows_by_name = {r["name"]: r for r in captured["branding"]["outro_rows"]}
    assert rows_by_name["A"]["size"] == "2 KB"
    assert rows_by_name["B"]["size"] == "—"  # nonexistent path -> fallback


@pytest.mark.asyncio
async def test_compose_challenge_speed_labels_skip_branding(session, monkeypatch):
    """drafts.py's speed_labels re-compose ('grid-speeds.mp4') stays on the
    plain grid — branding is only for the primary review composition."""
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="spark",
                   status="rendered", video_path="/sd/a.mp4", metrics={"duration_ms": 1000}),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="spark",
                   status="rendered", video_path="/sd/b.mp4", metrics={"duration_ms": 2000}),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": "/sd/grid-speeds.mp4", "bytes": 1, "inputs": 2}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured.update(json)
            return FakeResp()

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)

    await orchestrator.compose_challenge(
        session, ch, entries, speed_labels=True, output_name="grid-speeds.mp4"
    )
    assert "branding" not in captured
    assert "speed_labels" in captured


# ── work-time derivation from task_events (outro TIME fix, 2026-07-12) ─────


async def _add_events(session, task_id, steps):
    """steps: list of (from_status, to_status, changed_by, at)."""
    from app.models.task import TaskEvent

    for from_status, to_status, changed_by, at in steps:
        session.add(TaskEvent(
            task_id=task_id, from_status=from_status, to_status=to_status,
            changed_by=changed_by, created_at=at,
        ))
    await session.commit()


@pytest.mark.asyncio
async def test_task_work_duration_from_events(session, make_board, make_task):
    """Span = first ->in_progress to FIRST agent in_progress->review."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="[Bench] t")
    t0 = datetime(2026, 7, 12, 12, 0, 0)
    await _add_events(session, task.id, [
        ("inbox", "in_progress", "agent", t0),
        ("in_progress", "review", "agent", t0 + timedelta(seconds=90)),
        # Review ping-pong afterwards must NOT count:
        ("review", "in_progress", "user", t0 + timedelta(minutes=30)),
        ("in_progress", "review", "agent", t0 + timedelta(minutes=45)),
        ("review", "done", "user", t0 + timedelta(hours=2)),
    ])
    assert await orchestrator.task_work_duration_ms(session, task.id) == 90_000


@pytest.mark.asyncio
async def test_task_work_duration_survives_redispatch_reset(session, make_board, make_task):
    """Real live shape (task 6c9517df, horror-forest run): a manual
    in_progress->inbox reset mid-run. The event span still measures start of
    work to the first agent review transition."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="[Bench] t")
    t0 = datetime(2026, 7, 12, 13, 58, 37)
    await _add_events(session, task.id, [
        ("inbox", "in_progress", "agent", t0),
        ("in_progress", "inbox", "user", t0 + timedelta(seconds=29)),   # manual reset
        ("in_progress", "review", "agent", t0 + timedelta(seconds=80)),
        ("review", "done", "user", t0 + timedelta(minutes=5)),
    ])
    assert await orchestrator.task_work_duration_ms(session, task.id) == 80_000


@pytest.mark.asyncio
async def test_task_work_duration_none_without_review_event(session, make_board, make_task):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="[Bench] t")
    await _add_events(session, task.id, [
        ("inbox", "in_progress", "agent", datetime(2026, 7, 12, 12, 0, 0)),
    ])
    assert await orchestrator.task_work_duration_ms(session, task.id) is None
    # And with no events at all:
    task2 = await make_task(board.id, title="[Bench] t2")
    assert await orchestrator.task_work_duration_ms(session, task2.id) is None


@pytest.mark.asyncio
async def test_on_task_done_stores_event_derived_duration(
    session, tmp_path, monkeypatch, make_board, make_task
):
    """on_task_done must store the event-derived WORK time, not
    completed_at - dispatched_at (which includes the review wait)."""
    from app.models.deliverable import TaskDeliverable

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    now = datetime.now(timezone.utc)
    task = await make_task(
        board.id, title="[Bench] one-shot", status="done",
        # Timestamp diff would say 2 hours — wrong (includes review wait):
        dispatched_at=now - timedelta(hours=2), completed_at=now,
    )
    t0 = datetime(2026, 7, 12, 12, 0, 0)
    await _add_events(session, task.id, [
        ("inbox", "in_progress", "agent", t0),
        ("in_progress", "review", "agent", t0 + timedelta(seconds=48)),
        ("review", "done", "user", t0 + timedelta(hours=2)),
    ])

    src = tmp_path / "agent-out" / "index.html"
    src.parent.mkdir(parents=True)
    src.write_text("<html><body>x</body></html>")
    session.add(TaskDeliverable(task_id=task.id, deliverable_type="file",
                                title="index.html", path=str(src)))

    ch, entries = await _seed(
        session,
        entry_specs=[{"model_label": "A", "source_kind": "agent",
                      "status": "generating", "task_id": task.id}],
    )
    monkeypatch.setattr(orchestrator, "record_entry",
                        AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"}))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    await orchestrator.on_task_done(session, task)
    await session.refresh(entries[0])
    assert entries[0].metrics["duration_ms"] == 48_000  # events, not 2h


@pytest.mark.asyncio
async def test_on_task_done_falls_back_to_timestamps_without_events(
    session, tmp_path, monkeypatch, make_board, make_task
):
    from app.models.deliverable import TaskDeliverable

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    now = datetime.now(timezone.utc)
    task = await make_task(
        board.id, title="[Bench] one-shot", status="done",
        dispatched_at=now - timedelta(seconds=42), completed_at=now,
    )
    src = tmp_path / "agent-out" / "index.html"
    src.parent.mkdir(parents=True)
    src.write_text("<html></html>")
    session.add(TaskDeliverable(task_id=task.id, deliverable_type="file",
                                title="index.html", path=str(src)))
    ch, entries = await _seed(
        session,
        entry_specs=[{"model_label": "A", "source_kind": "agent",
                      "status": "generating", "task_id": task.id}],
    )
    monkeypatch.setattr(orchestrator, "record_entry",
                        AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"}))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    await orchestrator.on_task_done(session, task)
    await session.refresh(entries[0])
    assert entries[0].metrics["duration_ms"] == 42_000


# ── outro COST column (2026-07-12) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_outro_cost_from_model_usage_events(session, monkeypatch, make_board, make_task, make_agent):
    """Agent entry with attributed model_usage_events -> summed '$x.xx';
    agent entry without attribution -> '—'; spark -> 'local'."""
    from app.models.model_usage import ModelUsageEvent

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(board_id=board.id, name="Grok-Agent", harness="grok")
    task = await make_task(board.id, title="[Bench] priced")

    for i, cost in enumerate((0.30, 0.12)):
        session.add(ModelUsageEvent(
            task_id=task.id, harness="host", model="grok-4.5",
            session_id="s1", message_uuid=f"m-{uuid.uuid4().hex}", cost_usd=cost,
            ts=datetime(2026, 7, 12, 12, 0, i), source_file="test.jsonl",
        ))
    await session.commit()

    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = [
        BenchEntry(challenge_id=ch.id, model_label="Grok 4.5", source_kind="agent",
                   agent_id=agent.id, task_id=task.id, status="rendered",
                   video_path="/sd/a.mp4", metrics={"duration_ms": 60000}),
        BenchEntry(challenge_id=ch.id, model_label="Qwen", source_kind="spark",
                   status="rendered", video_path="/sd/b.mp4",
                   metrics={"duration_ms": 90000}),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _fake_compose_client(monkeypatch)
    await orchestrator.compose_challenge(session, ch, entries)

    rows = {r["name"]: r for r in captured["branding"]["outro_rows"]}
    assert rows["Grok 4.5"]["cost"] == "$0.42"
    assert rows["Qwen"]["cost"] == "local"


@pytest.mark.asyncio
async def test_outro_cost_dash_without_attribution(session, monkeypatch, make_board, make_task, make_agent):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(board_id=board.id, name="Grok-Agent", harness="grok")
    task = await make_task(board.id, title="[Bench] unpriced")

    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="agent",
                   agent_id=agent.id, task_id=task.id, status="rendered",
                   video_path="/sd/a.mp4"),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="agent",
                   agent_id=agent.id, status="rendered", video_path="/sd/b.mp4"),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _fake_compose_client(monkeypatch)
    await orchestrator.compose_challenge(session, ch, entries)
    rows = {r["name"]: r for r in captured["branding"]["outro_rows"]}
    assert rows["A"]["cost"] == "—"  # task without usage rows
    assert rows["B"]["cost"] == "—"  # entry without task at all


@pytest.mark.asyncio
async def test_branding_time_derived_defensively_from_events(session, monkeypatch, make_board, make_task):
    """Entry without stored duration_ms but with task_events -> the payload
    builder derives the time on the fly."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="[Bench] t")
    t0 = datetime(2026, 7, 12, 12, 0, 0)
    await _add_events(session, task.id, [
        ("inbox", "in_progress", "agent", t0),
        ("in_progress", "review", "agent", t0 + timedelta(minutes=6)),
    ])
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="agent",
                   task_id=task.id, status="rendered", video_path="/sd/a.mp4"),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="spark",
                   status="rendered", video_path="/sd/b.mp4"),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured = _fake_compose_client(monkeypatch)
    await orchestrator.compose_challenge(session, ch, entries)
    rows = {r["name"]: r for r in captured["branding"]["outro_rows"]}
    assert rows["A"]["time"] == "6.0 min"  # from events
    assert rows["B"]["time"] == "—"        # spark without metrics -> dash


# ── recompose (compose-only rebuild, 2026-07-12) ───────────────────────────


@pytest.mark.asyncio
async def test_recompose_challenge_rebuilds_compose_only(session, monkeypatch):
    monkeypatch.setattr("app.database.engine", test_engine)
    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/a.mp4"},
            {"model_label": "B", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/b.mp4"},
        ],
    )
    ch.status = "review"
    session.add(ch)
    await session.commit()

    compose_mock = AsyncMock(return_value="/sd/branded-v2.mp4")
    record_mock = AsyncMock()
    monkeypatch.setattr(orchestrator, "compose_challenge", compose_mock)
    monkeypatch.setattr(orchestrator, "record_entry", record_mock)

    await orchestrator.recompose_challenge(ch.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as vs:
        updated = await vs.get(BenchChallenge, ch.id)
    assert updated.status == "review"
    assert updated.composed_video_path == "/sd/branded-v2.mp4"
    compose_mock.assert_awaited_once()
    record_mock.assert_not_awaited()  # compose ONLY — no re-record


@pytest.mark.asyncio
async def test_recompose_challenge_fails_without_recordings(session, monkeypatch):
    monkeypatch.setattr("app.database.engine", test_engine)
    ch, _ = await _seed(
        session,
        entry_specs=[{"model_label": "A", "source_kind": "spark",
                      "status": "rendered", "video_path": "/sd/a.mp4"}],
    )
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock())
    await orchestrator.recompose_challenge(ch.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as vs:
        updated = await vs.get(BenchChallenge, ch.id)
    assert updated.status == "failed"
    assert "recompose" in updated.error


# ── compose output versioning + cleanup (cache-busting, 2026-07-13) ────────


@pytest.mark.asyncio
async def test_compose_challenge_default_output_name_is_versioned(session, monkeypatch):
    """No explicit output_name -> a fresh 'grid-<hex>.mp4' every call, never
    the old fixed 'grid.mp4' — the browser must never see the same URL for
    two different videos (recompose/rerender cache-staleness incident)."""
    ch = BenchChallenge(title="T", prompt_text="p", mode="side_by_side")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = [
        BenchEntry(challenge_id=ch.id, model_label="A", source_kind="spark",
                   status="rendered", video_path="/sd/a.mp4"),
        BenchEntry(challenge_id=ch.id, model_label="B", source_kind="spark",
                   status="rendered", video_path="/sd/b.mp4"),
    ]
    for e in entries:
        session.add(e)
    await session.commit()

    captured_paths: list[str] = []

    class FakeResp:
        def __init__(self, output_path):
            self._output_path = output_path

        def raise_for_status(self):
            pass

        def json(self):
            return {"output_path": self._output_path, "bytes": 1, "inputs": 2}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured_paths.append(json["output_path"])
            return FakeResp(json["output_path"])

    monkeypatch.setattr(orchestrator.httpx, "AsyncClient", FakeClient)

    result_1 = await orchestrator.compose_challenge(session, ch, entries)
    result_2 = await orchestrator.compose_challenge(session, ch, entries)

    assert result_1 != result_2
    assert Path(result_1).name.startswith("grid-") and Path(result_1).name.endswith(".mp4")
    assert Path(result_1).name != "grid.mp4"
    assert Path(result_2).name != "grid.mp4"
    assert captured_paths[0] != captured_paths[1]


@pytest.mark.asyncio
async def test_recompose_challenge_cleans_up_old_video_file(session, monkeypatch, tmp_path):
    """After a successful recompose, the previous composed video is removed
    from disk — versioned filenames would otherwise orphan it forever."""
    monkeypatch.setattr("app.database.engine", test_engine)
    old_video = tmp_path / "grid-aaaaaaaa.mp4"
    old_video.write_bytes(b"old")
    new_video = tmp_path / "grid-bbbbbbbb.mp4"

    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/a.mp4"},
            {"model_label": "B", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/b.mp4"},
        ],
    )
    ch.status = "review"
    ch.composed_video_path = str(old_video)
    session.add(ch)
    await session.commit()

    compose_mock = AsyncMock(return_value=str(new_video))
    monkeypatch.setattr(orchestrator, "compose_challenge", compose_mock)

    await orchestrator.recompose_challenge(ch.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as vs:
        updated = await vs.get(BenchChallenge, ch.id)
    assert updated.composed_video_path == str(new_video)
    assert not old_video.exists()  # cleaned up


@pytest.mark.asyncio
async def test_rerender_challenge_cleans_up_old_video_file(session, monkeypatch, tmp_path):
    """rerender re-nulls composed_video_path before recomposing — the
    previous file must still be removed once the new one lands."""
    monkeypatch.setattr("app.database.engine", test_engine)
    old_video = tmp_path / "grid-old11111.mp4"
    old_video.write_bytes(b"old")
    new_video = tmp_path / "grid-new22222.mp4"

    ch, entries = await _seed(
        session,
        entry_specs=[
            {"model_label": "A", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/a.mp4", "artifact_path": "/tmp/a/index.html"},
            {"model_label": "B", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/b.mp4", "artifact_path": "/tmp/b/index.html"},
        ],
    )
    ch.status = "review"
    ch.composed_video_path = str(old_video)
    session.add(ch)
    await session.commit()

    _record_ok(monkeypatch)
    compose_mock = AsyncMock(return_value=str(new_video))
    monkeypatch.setattr(orchestrator, "compose_challenge", compose_mock)

    await orchestrator.rerender_challenge(ch.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as vs:
        updated = await vs.get(BenchChallenge, ch.id)
    assert updated.composed_video_path == str(new_video)
    assert not old_video.exists()  # cleaned up


@pytest.mark.asyncio
async def test_cleanup_old_compose_noop_when_same_path(tmp_path):
    """Same path (or None) -> never delete — guards against a compose that
    happens to return the identical name (shouldn't happen, but must be safe)."""
    video = tmp_path / "grid-xxxx.mp4"
    video.write_bytes(b"x")
    orchestrator._cleanup_old_compose(str(video), str(video))
    assert video.exists()
    orchestrator._cleanup_old_compose(None, str(video))
    assert video.exists()


@pytest.mark.asyncio
async def test_cleanup_old_compose_missing_file_is_safe(tmp_path):
    """Old path already gone (manual cleanup, double-run) -> no crash."""
    orchestrator._cleanup_old_compose(str(tmp_path / "does-not-exist.mp4"), "/sd/new.mp4")
