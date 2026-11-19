"""bench_studio W0.8 — orphans heal in the background, the detail GET is read-only.

Before: reconcile_challenge ran only inside GET /challenges/{id} (and wrote
from a GET). Challenges whose agent task was deleted (task_id SET NULL) sat at
`generating` in the list forever unless someone opened them — 3 such rows
since 19.10. Now a background sweep inside the vertical heals them; the list
shows the result without anyone opening the challenge.
"""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals import hooks
from app.verticals.bench_studio import orchestrator
from tests.conftest import test_engine


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """The sweep opens its own session on app.database.engine — point it at
    the test DB. Never let a render reach the sidecar."""
    monkeypatch.setattr("app.database.engine", test_engine)
    monkeypatch.setattr(orchestrator, "record_entry", AsyncMock())
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))


async def _seed_orphan(session, *, title="Pagoda - OMP", archived=False):
    """The live shape of the 3 stuck 19.10. rows: single mode, one agent
    entry at generating, task deleted (task_id NULL)."""
    from app.utils import utcnow

    ch = BenchChallenge(
        title=title, prompt_text="p", mode="single", status="generating",
        archived_at=utcnow() if archived else None,
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    e = BenchEntry(
        challenge_id=ch.id, model_label="omp-alpha", source_kind="agent",
        status="generating", task_id=None,
    )
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return ch, e


async def _fresh(model, pk):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(model, pk)


@pytest.mark.asyncio
async def test_sweep_heals_deleted_task_orphans(session):
    """All three stuck rows heal in one sweep, no GET involved."""
    seeded = [await _seed_orphan(session) for _ in range(3)]

    healed = await orchestrator.reconcile_open_challenges()

    assert healed == 3
    for ch, e in seeded:
        entry = await _fresh(BenchEntry, e.id)
        challenge = await _fresh(BenchChallenge, ch.id)
        assert entry.status == "failed"
        assert entry.error == "agent task deleted"
        assert challenge.status == "failed"


@pytest.mark.asyncio
async def test_sweep_marks_failed_and_aborted_tasks(session, make_board, make_task):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    failed = await make_task(board.id, title="[Bench] a", status="failed")
    aborted = await make_task(board.id, title="[Bench] b", status="aborted")
    ch = BenchChallenge(title="T", prompt_text="p", status="generating")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    e1 = BenchEntry(challenge_id=ch.id, model_label="A", source_kind="agent",
                    status="generating", task_id=failed.id)
    e2 = BenchEntry(challenge_id=ch.id, model_label="B", source_kind="agent",
                    status="generating", task_id=aborted.id)
    session.add(e1)
    session.add(e2)
    await session.commit()

    await orchestrator.reconcile_open_challenges()

    assert (await _fresh(BenchEntry, e1.id)).error == "agent task failed"
    assert (await _fresh(BenchEntry, e2.id)).error == "agent task aborted"
    assert (await _fresh(BenchChallenge, ch.id)).status == "failed"


@pytest.mark.asyncio
async def test_sweep_leaves_running_and_finished_challenges_alone(
    session, make_board, make_task
):
    """in_progress tasks keep generating; non-generating challenges are not
    even looked at (no render is triggered for a finished challenge)."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    running = await make_task(board.id, title="[Bench] r", status="in_progress")
    ch = BenchChallenge(title="Live", prompt_text="p", status="generating")
    done = BenchChallenge(title="Done", prompt_text="p", status="review")
    session.add(ch)
    session.add(done)
    await session.commit()
    await session.refresh(ch)
    await session.refresh(done)
    e = BenchEntry(challenge_id=ch.id, model_label="A", source_kind="agent",
                   status="generating", task_id=running.id)
    # A generating-looking entry under a finished challenge must stay as is.
    stale = BenchEntry(challenge_id=done.id, model_label="B", source_kind="agent",
                       status="generating", task_id=None)
    session.add(e)
    session.add(stale)
    await session.commit()

    healed = await orchestrator.reconcile_open_challenges()

    assert healed == 0
    assert (await _fresh(BenchEntry, e.id)).status == "generating"
    assert (await _fresh(BenchChallenge, ch.id)).status == "generating"
    assert (await _fresh(BenchEntry, stale.id)).status == "generating"
    assert (await _fresh(BenchChallenge, done.id)).status == "review"


@pytest.mark.asyncio
async def test_sweep_heals_archived_orphans_too(session):
    """Archived only hides a row; its state must still be honest when the
    operator shows the archive."""
    ch, e = await _seed_orphan(session, archived=True)
    await orchestrator.reconcile_open_challenges()
    assert (await _fresh(BenchEntry, e.id)).error == "agent task deleted"
    assert (await _fresh(BenchChallenge, ch.id)).status == "failed"


@pytest.mark.asyncio
async def test_sweep_one_broken_challenge_does_not_stop_the_rest(session, monkeypatch):
    first, _ = await _seed_orphan(session, title="first")
    second, second_entry = await _seed_orphan(session, title="second")
    real = orchestrator.reconcile_challenge

    async def flaky(s, challenge, entries):
        if challenge.id == first.id:
            raise RuntimeError("injected")
        await real(s, challenge, entries)

    monkeypatch.setattr(orchestrator, "reconcile_challenge", flaky)

    await orchestrator.reconcile_open_challenges()

    assert (await _fresh(BenchEntry, second_entry.id)).status == "failed"
    assert (await _fresh(BenchChallenge, first.id)).status == "generating"


@pytest.mark.asyncio
async def test_list_shows_healed_orphan_without_opening_it(auth_client, session):
    """The acceptance: after the background sweep the LIST alone carries the
    aborted state + reason (the UI maps it to "Abgebrochen – Auftrag
    gelöscht"); the detail page was never requested."""
    ch, _ = await _seed_orphan(session)
    await orchestrator.reconcile_open_challenges()

    listing = await auth_client.get("/api/v1/bench/challenges")
    assert listing.status_code == 200
    row = next(c for c in listing.json() if c["id"] == str(ch.id))
    assert row["status"] == "failed"
    assert [e["error"] for e in row["entries"]] == ["agent task deleted"]


@pytest.mark.asyncio
async def test_detail_get_is_read_only(auth_client, session, monkeypatch):
    """GET /challenges/{id} no longer writes: an orphan stays as stored until
    the sweep runs (and reconcile is never called from the request)."""
    ch, e = await _seed_orphan(session)
    spy = AsyncMock()
    monkeypatch.setattr(orchestrator, "reconcile_challenge", spy)

    resp = await auth_client.get(f"/api/v1/bench/challenges/{ch.id}")

    assert resp.status_code == 200
    spy.assert_not_called()
    assert resp.json()["status"] == "generating"
    assert (await _fresh(BenchEntry, e.id)).status == "generating"


@pytest.mark.asyncio
async def test_list_get_is_read_only(auth_client, session, monkeypatch):
    ch, e = await _seed_orphan(session)
    spy = AsyncMock()
    monkeypatch.setattr(orchestrator, "reconcile_challenge", spy)

    resp = await auth_client.get("/api/v1/bench/challenges")

    assert resp.status_code == 200
    spy.assert_not_called()
    assert (await _fresh(BenchEntry, e.id)).status == "generating"


@pytest.mark.asyncio
async def test_reconcile_loop_sweeps_repeatedly_and_survives_errors(monkeypatch):
    calls = 0

    async def sweep():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db hiccup")  # the loop must keep going
        return 0

    monkeypatch.setattr(orchestrator, "reconcile_open_challenges", sweep)
    monkeypatch.setattr(orchestrator, "RECONCILE_INITIAL_DELAY_S", 0)
    monkeypatch.setattr(orchestrator, "RECONCILE_INTERVAL_S", 0)

    task = asyncio.create_task(orchestrator.reconcile_loop())
    for _ in range(100):
        await asyncio.sleep(0)
        if calls >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 3


def test_register_adds_the_sweep_as_a_background_loop():
    """Strippable (ADR-044): the vertical hands its loop to core through the
    hook registry; core never imports the vertical."""
    from fastapi import FastAPI

    from app.verticals import bench_studio

    before = list(hooks.background_loops)
    try:
        bench_studio.register(FastAPI())
        bench_studio.register(FastAPI())  # idempotent
        assert hooks.background_loops.count(orchestrator.reconcile_loop) == 1
    finally:
        hooks.background_loops[:] = before


@pytest.mark.asyncio
async def test_start_background_loops_runs_each_hook_and_tolerates_a_bad_one(monkeypatch):
    started = asyncio.Event()

    async def good():
        started.set()
        await asyncio.sleep(3600)

    def bad():
        raise RuntimeError("broken factory")

    monkeypatch.setattr(hooks, "background_loops", [bad, good])
    tasks = hooks.start_background_loops()
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert len(tasks) == 1
    finally:
        await hooks.stop_background_loops(tasks)
    assert all(t.done() for t in tasks)


@pytest.mark.asyncio
async def test_api_lifespan_starts_and_cancels_vertical_loops(monkeypatch):
    """The real app.main lifespan starts every hooks.background_loops entry
    and cancels it on shutdown (otherwise the sweep never runs in prod)."""
    from tests.test_lifespan_jarvis_cancel import _run_lifespan_shutdown

    created: list[asyncio.Task] = []
    real_start = hooks.start_background_loops

    def spy_start():
        tasks = real_start()
        created.extend(tasks)
        return tasks

    async def fake_loop():
        await asyncio.sleep(3600)

    monkeypatch.setattr(hooks, "background_loops", [fake_loop])
    monkeypatch.setattr(hooks, "start_background_loops", spy_start)
    await _run_lifespan_shutdown(monkeypatch)
    assert [t.get_name() for t in created] == ["vertical:fake_loop"]
    assert all(t.cancelled() for t in created)
