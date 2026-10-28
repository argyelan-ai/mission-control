"""Bench agent run — findings of the live run 28.10.2026.

1. The 2 h time limit stopped an omp run whose nearly finished index.html sat
   in the agent's worktree; the entry ended with no page at all. The stop now
   keeps the newest candidate page as an INCOMPLETE result (still a failed,
   unranked run).
2. The brief named no path; the collector only read registered deliverables.
   Both now agree on one path, and the collector also finds an unregistered
   page at exactly that path.
3. The bench task got a worktree of the shared ad-hoc scratch repo (foreign
   files). A bench task now starts in an empty folder of its own.
4. The automatic stop was recorded as the operator's stop.
5. The omp session's resolved effort is recorded on a time-limit stop too.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

pytest.importorskip("app.verticals.bench_studio")

from app.models.activity import ActivityEvent  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.bench import BenchEntry  # noqa: E402
from app.models.task import Task, TaskEvent  # noqa: E402
from app.verticals import hooks  # noqa: E402
from app.verticals.bench_studio import orchestrator as orch  # noqa: E402
from app.verticals.bench_studio import w1_common as c  # noqa: E402
from app.verticals.bench_studio import w1_receipt, w1_runner, w1_tests  # noqa: E402
from app.verticals.bench_studio import w1_schemas as s  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401
    PAGE, engines, factory, no_sleep, seed_fleet, world,
)

PARTIAL = "<!doctype html><html><body><canvas></canvas><script>/* half a pagoda"


@pytest.fixture
def deliv_root(tmp_path, monkeypatch):
    """The backend's view of the deliverables mount, under tmp_path."""
    root = tmp_path / "deliverables-mount"
    root.mkdir()

    async def backend_view(session, agent, agent_path):
        rel = agent_path.split("/deliverables/", 1)[1]
        return str(root / rel)

    monkeypatch.setattr(orch, "_backend_view", backend_view)
    return root


def bench_ws(agent_ws: Path, task_id) -> Path:
    return agent_ws / orch.BENCH_WORKSPACE_DIR / str(task_id)


async def _agent_test(session, fleet, *, agent_ws: Path | None = None, workspace: Path | None = None,
                      started_min_ago=None, task_id=None):
    """A running omp agent entry. ``agent_ws`` = the agent's workspace; the
    task works in its bench folder below it unless ``workspace`` says else."""
    ch = await w1_tests.create_test(
        session, picks=[{"runtime_id": fleet["slot"].id, "harness": "omp", "efforts": ["default"]}],
        preset="standard", trigger="runtimes", user_id=str(uuid.uuid4()))
    e = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).first()
    task_id = task_id or uuid.uuid4()
    if agent_ws is not None:
        agent = await session.get(Agent, fleet["omp_agent"].id)
        agent.workspace_path = str(agent_ws)
        session.add(agent)
        if workspace is None:
            workspace = bench_ws(agent_ws, task_id)
            workspace.mkdir(parents=True, exist_ok=True)
    task = Task(id=task_id, title="[Bench] x", board_id=fleet["board"].id, status="in_progress",
                assigned_agent_id=fleet["omp_agent"].id,
                auto_reason=f"bench_studio challenge {ch.id}",
                workspace_path=str(workspace) if workspace else None)
    session.add(task)
    await session.commit()
    e.status = "generating"
    e.task_id = task.id
    e.agent_id = fleet["omp_agent"].id
    ago = started_min_ago if started_min_ago is not None else c.AGENT_TIME_LIMIT_MIN + 5
    e.metrics = {**(e.metrics or {}), "agent_started_at": (c.now_utc() - timedelta(minutes=ago)).isoformat()}
    session.add(e)
    await session.commit()
    return ch, e, task


def _write(path: Path, text: str, age_s: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


# ── 1. time limit keeps the newest page, marked incomplete ───────────────


@pytest.mark.asyncio
async def test_time_limit_keeps_the_newest_page_of_the_worktree_as_incomplete(
        session, engines, world, deliv_root, tmp_path):
    fleet = await seed_fleet(session)
    tid = uuid.uuid4()
    ws = bench_ws(tmp_path / "agent-ws", tid)
    _write(ws / "draft" / "old.html", "<html>old</html>", age_s=600)
    _write(ws / "index.html", PARTIAL, age_s=5)
    _write(ws / ".git" / "x.html", "<html>git internals are never a page</html>")
    ch, e, _task = await _agent_test(session, fleet, agent_ws=tmp_path / "agent-ws", task_id=tid)

    ok = await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    assert ok is False
    await session.refresh(e)
    assert e.status == "failed" and e.metrics["error_code"] == "time_limit"
    assert e.metrics["incomplete_page"] is True
    assert e.metrics["incomplete_source"] == "workspace"
    assert e.artifact_path and Path(e.artifact_path).read_text() == PARTIAL
    assert Path(e.artifact_path).parent == orch.entry_dir(e)
    # Never ranked like a finished run.
    assert w1_receipt.done_runs([e]) == []
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    run = d.blocks[-1].contestants[0].runs[0]
    assert run.state == "failed" and run.incomplete_page is True
    assert run.error.code == "time_limit"


@pytest.mark.asyncio
async def test_time_limit_prefers_the_newer_page_in_the_deliverables_folder(
        session, engines, world, deliv_root, tmp_path):
    fleet = await seed_fleet(session)
    tid = uuid.uuid4()
    _write(bench_ws(tmp_path / "agent-ws", tid) / "index.html", "<html>older copy</html>", age_s=300)
    ch, e, task = await _agent_test(session, fleet, agent_ws=tmp_path / "agent-ws", task_id=tid)
    _write(deliv_root / str(task.id) / "index.html", PARTIAL, age_s=1)

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    await session.refresh(e)
    assert Path(e.artifact_path).read_text() == PARTIAL
    assert e.metrics["incomplete_source"] == "deliverables"


@pytest.mark.asyncio
async def test_time_limit_without_any_page_stays_a_plain_failure(session, engines, world, deliv_root, tmp_path):
    fleet = await seed_fleet(session)
    ch, e, _task = await _agent_test(session, fleet, agent_ws=tmp_path / "agent-ws")

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    await session.refresh(e)
    assert e.status == "failed" and e.metrics["error_code"] == "time_limit"
    assert e.artifact_path is None and not e.metrics.get("incomplete_page")
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert d.blocks[-1].contestants[0].runs[0].incomplete_page is False


@pytest.mark.asyncio
async def test_a_scratch_repo_worktree_is_never_searched(session, engines, world, deliv_root, tmp_path):
    """Only the task's own bench folder counts — a worktree of the shared
    scratch repo holds unrelated pages that are not this run's."""
    fleet = await seed_fleet(session)
    scratch = tmp_path / "agent-ws" / "projects" / "mc-workspace" / ".worktrees" / "bench"
    _write(scratch / "index.html", "<html>someone else's page</html>")
    ch, e, _task = await _agent_test(session, fleet, agent_ws=tmp_path / "agent-ws", workspace=scratch)

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    await session.refresh(e)
    assert e.artifact_path is None and not e.metrics.get("incomplete_page")


@pytest.mark.asyncio
async def test_a_symlinked_page_is_never_followed_out_of_the_agent_folder(
        session, engines, world, deliv_root, tmp_path):
    """Agent-written paths are untrusted: a link to a backend file must not
    end up copied and served as the agent's page."""
    fleet = await seed_fleet(session)
    secret = _write(tmp_path / "backend-only" / "secret.html", "<html>SECRET</html>")
    tid = uuid.uuid4()
    ws = bench_ws(tmp_path / "agent-ws", tid)
    ws.mkdir(parents=True)
    (ws / "index.html").symlink_to(secret)
    (deliv_root / str(tid)).symlink_to(secret.parent, target_is_directory=True)
    ch, e, task = await _agent_test(session, fleet, agent_ws=tmp_path / "agent-ws", task_id=tid)

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    await session.refresh(e)
    assert e.artifact_path is None, "a symlinked page was copied"

    # The collector's brief-path fallback refuses a linked file too.
    (deliv_root / str(tid)).unlink()
    (deliv_root / str(tid)).mkdir()
    (deliv_root / str(tid) / "index.html").symlink_to(secret)
    e.status = "generating"
    session.add(e)
    task.status = "done"
    session.add(task)
    await session.commit()
    await orch.on_task_done(session, task)
    await session.refresh(e)
    assert e.status == "failed" and e.artifact_path is None


@pytest.mark.asyncio
async def test_the_stop_happens_even_when_keeping_the_page_breaks(session, engines, world, deliv_root,
                                                                 tmp_path, monkeypatch):
    fleet = await seed_fleet(session)
    ch, e, task = await _agent_test(session, fleet, agent_ws=tmp_path / "agent-ws")

    async def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(orch, "salvage_incomplete_page", broken)
    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    async with factory() as s2:
        t = await s2.get(Task, task.id)
        assert t.run_control == "stopped" and t.status == "blocked"
        row = await s2.get(BenchEntry, e.id)
        assert row.metrics["error_code"] == "time_limit" and row.metrics.get("harvested_at")


@pytest.mark.asyncio
async def test_brief_path_and_collector_path_meet_for_every_runtime(session, engines):
    """No seam patched: the brief's path and the path the backend reads
    are the same file — container agents (slug folder) and host workers."""
    fleet = await seed_fleet(session)
    agent = await session.get(Agent, fleet["omp_agent"].id)
    tid = uuid.uuid4()
    agent_path, backend = await orch.deliverable_target(session, agent, tid)
    assert agent_path == f"/deliverables/{tid}/index.html"
    assert str(backend) == f"/deliverables/{agent.slug}/{tid}/index.html"
    agent.agent_runtime = "host"
    agent_path, backend = await orch.deliverable_target(session, agent, tid)
    assert agent_path == f"~/.mc/deliverables/{tid}/index.html"
    assert str(backend) == f"/deliverables/{tid}/index.html"


def test_legacy_rerender_never_promotes_an_incomplete_page():
    """The old "rerender" path flips failed entries with a page to generated
    — a salvaged page must not sneak into the ranking that way."""
    src = Path(orch.__file__).read_text()
    block = src.split("async def rerender_challenge", 1)[1].split("async def recompose_challenge", 1)[0]
    assert "incomplete_page" in block


# ── 4. the automatic stop is the system's, not the operator's ────────────


@pytest.mark.asyncio
async def test_time_limit_stop_is_recorded_as_system(session, engines, world, deliv_root, tmp_path):
    fleet = await seed_fleet(session)
    ch, e, task = await _agent_test(session, fleet, workspace=tmp_path / "ws")
    task.dispatched_at = c.now_utc()
    session.add(task)
    await session.commit()

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    async with factory() as s2:
        ev = (await s2.exec(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()
        stop = [x for x in ev if x.to_status == "blocked"]
        assert stop and stop[-1].changed_by == "system"
        assert stop[-1].actor_user_id is None
        act = (await s2.exec(select(ActivityEvent).where(ActivityEvent.event_type == "task.run_stopped"))).all()
        assert act and act[-1].detail["stopped_by"] == "system"
        assert str(ch.plan.get("created_by")) not in str(act[-1].detail)


# ── 5. the session's effort is recorded on a time-limit stop too ─────────


@pytest.mark.asyncio
async def test_time_limit_records_the_observed_session_effort(session, engines, world, deliv_root,
                                                              tmp_path, monkeypatch):
    from app.verticals.bench_studio import agent_effort

    async def observed(session_, entry, task):
        return {"source": "omp_session", "level": "high", "configured": None, "changed": False, "attempts": 1}

    monkeypatch.setattr(agent_effort, "observed_agent_effort", observed)
    fleet = await seed_fleet(session)
    ch, e, _task = await _agent_test(session, fleet, workspace=tmp_path / "ws")

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id, sleep=no_sleep)

    await session.refresh(e)
    assert e.metrics["effort_observed"]["level"] == "high"
    await session.refresh(ch)
    d = s.BenchTestDetail.model_validate(await w1_tests.test_detail(session, ch))
    assert d.blocks[-1].contestants[0].effort.observed["level"] == "high"


@pytest.mark.asyncio
async def test_a_running_agent_entry_learns_its_session_effort_while_it_runs(
        session, engines, world, deliv_root, tmp_path, monkeypatch):
    """Live card: "Default" alone hides the level the session really runs on."""
    from app.verticals.bench_studio import agent_effort

    async def observed(session_, entry, task):
        return {"source": "omp_session", "level": "high", "configured": None, "changed": False, "attempts": 1}

    monkeypatch.setattr(agent_effort, "observed_agent_effort", observed)
    fleet = await seed_fleet(session)
    ch, e, task = await _agent_test(session, fleet, workspace=tmp_path / "ws", started_min_ago=1)
    polls = {"n": 0}

    async def one_poll_then_done(_s):
        polls["n"] += 1
        async with factory() as s2:
            row = await s2.get(BenchEntry, e.id)
            row.status = "failed"
            row.metrics = {**(row.metrics or {}), "error_code": "agent_task_failed"}
            s2.add(row)
            await s2.commit()

    await w1_runner.run_agent_contestant(factory, ch.id, ch.plan["contestants"][0], e.id,
                                         sleep=one_poll_then_done)
    await session.refresh(e)
    assert polls["n"] == 1
    assert e.metrics["effort_observed"]["level"] == "high"


# ── 2. one path in the brief, the same path in the collector ─────────────


@pytest.mark.asyncio
async def test_brief_names_the_exact_deliverable_path_of_the_task(session, engines, deliv_root, monkeypatch):
    fleet = await seed_fleet(session)
    started = []
    monkeypatch.setattr("app.services.dispatch.auto_dispatch_task", lambda *a, **k: _noop(started))
    ch = await w1_tests.create_test(
        session, picks=[{"runtime_id": fleet["slot"].id, "harness": "omp", "efforts": ["default"]}],
        preset="standard", trigger="runtimes", user_id=None)
    e = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == ch.id))).first()
    await orch.dispatch_agent_entry(session, e, ch)
    task = await session.get(Task, e.task_id)
    want = f"/deliverables/{task.id}/index.html"
    assert want in task.description
    assert f"mc deliverable" in task.description and "120 min" in task.description
    # The brief checksum stays the same for every task ("same brief").
    assert ch.plan["contestants"][0]["brief_sha"] == c.sha256_text(
        orch.agent_brief(ch.prompt_text, orch.BRIEF_PATH_PLACEHOLDER, c.AGENT_TIME_LIMIT_MIN))


@pytest.mark.asyncio
async def test_host_agents_get_the_host_form_of_the_path(session, engines, deliv_root):
    fleet = await seed_fleet(session)
    agent = await session.get(Agent, fleet["omp_agent"].id)
    agent.agent_runtime = "host"
    session.add(agent)
    await session.commit()
    tid = uuid.uuid4()
    agent_path, _backend = await orch.deliverable_target(session, agent, tid)
    assert agent_path == f"~/.mc/deliverables/{tid}/index.html"


async def _noop(started):
    started.append(1)


@pytest.mark.asyncio
async def test_collector_finds_an_unregistered_page_at_the_brief_path(session, engines, world, deliv_root,
                                                                      tmp_path):
    fleet = await seed_fleet(session)
    ch, e, task = await _agent_test(session, fleet, workspace=tmp_path / "ws", started_min_ago=1)
    _write(deliv_root / str(task.id) / "index.html", PAGE)
    task.status = "done"
    session.add(task)
    await session.commit()

    await orch.on_task_done(session, task)

    await session.refresh(e)
    assert e.status == "generated", e.error
    assert Path(e.artifact_path).read_text() == PAGE
    assert not e.metrics.get("incomplete_page")


# ── 3. a bench task starts in an empty folder of its own ─────────────────


@pytest.mark.asyncio
async def test_bench_task_gets_an_empty_workspace_not_the_shared_scratch_repo(
        session, engines, tmp_path, monkeypatch):
    from app.services import git_service as gs
    from app.services.task_context_builder import setup_git_workspace_for_dispatch
    from app.verticals.bench_studio import register

    class _App:
        def include_router(self, *_a, **_k):
            pass

    register(_App())
    assert orch.bench_task_workspace in hooks.task_workspace_providers

    async def boom(*_a, **_k):
        raise AssertionError("a bench task must not touch the shared scratch repo")

    monkeypatch.setattr(gs.git_service, "ensure_workspace", boom)
    fleet = await seed_fleet(session)
    agent = await session.get(Agent, fleet["omp_agent"].id)
    agent.workspace_path = str(tmp_path / "agent-ws")
    session.add(agent)
    task = Task(title="[Bench] x", board_id=fleet["board"].id, status="inbox", assigned_agent_id=agent.id,
                auto_reason=f"bench_studio challenge {uuid.uuid4()}")
    session.add(task)
    await session.commit()

    assert await setup_git_workspace_for_dispatch(task, agent, session) is True
    ws = Path(task.workspace_path)
    assert ws.is_dir() and list(ws.iterdir()) == []
    assert ws.is_relative_to(Path(agent.workspace_path))
    assert str(task.id) in ws.name

    # The cli-bridge twin keeps the same folder (a re-dispatch keeps the work).
    (ws / "index.html").write_text(PARTIAL)
    from app.services.cli_bridge_runner import _resolve_workspace

    workspace, worktree, has_repo = await _resolve_workspace(task, agent, session)
    assert workspace == str(ws) and worktree is None and has_repo is False
    assert (ws / "index.html").read_text() == PARTIAL


@pytest.mark.asyncio
async def test_other_tasks_never_get_the_bench_folder(session, engines, tmp_path):
    fleet = await seed_fleet(session)
    agent = await session.get(Agent, fleet["omp_agent"].id)
    agent.workspace_path = str(tmp_path / "agent-ws")
    task = Task(title="[Bench] named like one, but an operator card", board_id=fleet["board"].id,
                status="inbox", auto_reason="something else")
    assert await orch.bench_task_workspace(session, task, agent) is None


@pytest.mark.asyncio
async def test_a_broken_workspace_provider_falls_back_to_the_normal_path(monkeypatch):
    async def broken(session, task, agent):
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "task_workspace_providers", [broken])
    assert await hooks.provide_task_workspace(None, object(), object()) is None
