"""Head runs in the token harvester (metrics gap from the E0 baseline, §3.3).

A head writes its transcripts into its own run folder:
  - omp:    <heads_root>/<run_id>/omp-sessions/*.jsonl   (mc-head passes --session-dir)
  - claude: <heads_root>/<run_id>/claude-config/projects/**/*.jsonl (CLAUDE_CONFIG_DIR)
Attribution comes from the run's spec.json, never from cwd heuristics:
task_id, head_run_id, harness "head-<harness>", locality local|cloud.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlmodel import select

from app.models.model_usage import ModelUsageEvent
from tests.heads_backend_helpers import make_run
from tests.test_token_harvester import _make_line, _make_omp_line

pytestmark = pytest.mark.asyncio


def _omp_session(run_dir: Path, lines: list[str], name: str = "2026-09-26T09-11-15-859Z_0001.jsonl") -> Path:
    d = run_dir / "omp-sessions"
    d.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"type": "session", "version": 3, "id": "s1", "cwd": str(run_dir / "wt")})
    path = d / name
    path.write_text("\n".join([header, *lines]) + "\n")
    return path


def _claude_transcript(run_dir: Path, lines: list[str], rel: str = "projects/-wt/s1.jsonl") -> Path:
    path = run_dir / "claude-config" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


async def _harvest(session, root: Path) -> dict:
    from app.services.token_harvester import run_harvest

    return await run_harvest(
        session, agent_base_paths=[], boss_base_paths=[], agent_slug_map={},
        task_workspace_map={}, heads_root=str(root),
    )


async def _events(session) -> list[ModelUsageEvent]:
    return list((await session.exec(select(ModelUsageEvent).order_by(ModelUsageEvent.ts))).all())


async def test_omp_head_run_is_recorded_with_task_run_and_locality(session, tmp_path, make_board, make_task):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Head job")
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(task.id), harness="omp", model="glm-local", locality="local")
    _omp_session(root / run_id, [
        _make_omp_line(short_id="a1", response_id="chatcmpl-1", model="glm-local",
                       timestamp="2026-09-26T09:00:00.000Z", input_tokens=1000, output_tokens=10),
        _make_omp_line(short_id="a2", response_id="chatcmpl-2", model="glm-local",
                       timestamp="2026-09-26T09:00:05.000Z", input_tokens=1500, output_tokens=20),
    ])

    stats = await _harvest(session, root)

    assert stats["new_events"] == 2
    events = await _events(session)
    assert len(events) == 2
    for e in events:
        assert e.task_id == task.id
        assert e.head_run_id == run_id
        assert e.harness == "head-omp"
        assert e.locality == "local"
        assert e.model == "glm-local"
        assert e.agent_id is None
    # omp reports the full context per call — the usual delta split applies
    assert [e.input_tokens for e in events] == [1000, 500]
    assert [e.cache_read_tokens for e in events] == [0, 1000]


async def test_claude_head_run_incl_subagents_is_recorded(session, tmp_path, make_board, make_task):
    # v1 starts claude only on local runtimes (pairs.pair_status), so a real
    # claude head row is "local" today.
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Claude head job")
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(task.id), harness="claude", model="claude-opus-5", locality="local")
    _claude_transcript(root / run_id, [_make_line(uuid_=f"h-{run_id}-1", model="claude-opus-5")])
    _claude_transcript(root / run_id, [_make_line(uuid_=f"h-{run_id}-2", model="claude-opus-5")],
                       rel="projects/-wt/s1/subagents/agent-x.jsonl")

    await _harvest(session, root)

    events = await _events(session)
    assert {e.message_uuid for e in events} == {f"h-{run_id}-1", f"h-{run_id}-2"}
    assert {(e.harness, e.locality, e.head_run_id, e.task_id) for e in events} == {
        ("head-claude", "local", run_id, task.id)
    }


async def test_cloud_locality_from_the_spec_is_passed_through(session, tmp_path, make_board, make_task):
    # Cloud pairs come later (ADR-086); the harvester stores what spec.json says.
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Cloud pair")
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(task.id), harness="claude", model="claude-opus-5", locality="cloud")
    _claude_transcript(root / run_id, [_make_line(uuid_=f"c-{run_id}", model="claude-opus-5")])

    await _harvest(session, root)

    (event,) = await _events(session)
    assert event.locality == "cloud"


async def test_second_harvest_adds_nothing(session, tmp_path, make_board, make_task):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Twice")
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(task.id), locality="local")
    path = _omp_session(root / run_id, [_make_omp_line(short_id="x1", response_id="chatcmpl-x1")])

    assert (await _harvest(session, root))["new_events"] == 1
    assert (await _harvest(session, root))["new_events"] == 0
    # a changed file (mtime moves) is re-read from its offset — still no duplicate
    path.write_text(path.read_text())
    assert (await _harvest(session, root))["new_events"] == 0
    assert len(await _events(session)) == 1


async def test_locality_falls_back_to_the_runtime_for_older_specs(session, tmp_path, make_board, make_task):
    from app.models.host import Host
    from app.models.runtime import Runtime

    box = Host(slug=f"box-{uuid.uuid4().hex[:6]}", display_name="BOX", kind="ssh", ssh_host="192.0.2.10")
    session.add(box)
    await session.commit()
    await session.refresh(box)
    slug = f"slot-{uuid.uuid4().hex[:6]}"
    session.add(Runtime(slug=slug, display_name="Local slot", runtime_type="openai_compatible",
                        endpoint="http://192.0.2.10:8000/v1", model_identifier="glm", host_id=box.id))
    await session.commit()
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Old spec")
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(task.id), runtime_slug=slug)  # no "locality" key
    _omp_session(root / run_id, [_make_omp_line(short_id="o1", response_id=f"chatcmpl-{run_id}")])

    await _harvest(session, root)

    (event,) = await _events(session)
    assert event.locality == "local"


async def test_unknown_task_and_foreign_folders_are_safe(session, tmp_path):
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(uuid.uuid4()), locality="local")  # task deleted meanwhile
    _omp_session(root / run_id, [_make_omp_line(short_id="u1", response_id=f"chatcmpl-u-{run_id}")])
    # folders next to runs that are not runs: clones, spool, a run without spec.json
    for name in ("clones/owner--demo/omp-sessions", "spool", f"{uuid.uuid4()}/omp-sessions"):
        (root / name).mkdir(parents=True)
    (root / "clones/owner--demo/omp-sessions/x.jsonl").write_text(
        _make_omp_line(short_id="c1", response_id="chatcmpl-clone") + "\n")

    stats = await _harvest(session, root)

    events = await _events(session)
    assert [e.head_run_id for e in events] == [run_id]
    assert events[0].task_id is None  # never a dangling FK
    assert stats["source_errors"] == 0


async def test_heads_source_failure_does_not_block_other_sources(session, tmp_path, monkeypatch):
    from app.services import token_harvester

    async def _boom(*args, **kwargs):
        raise RuntimeError("heads source exploded")

    monkeypatch.setattr(token_harvester, "_harvest_heads", _boom)
    agents = tmp_path / "agents"
    rex = agents / "rex" / "claude-config" / "projects" / "p"
    rex.mkdir(parents=True)
    (rex / "s.jsonl").write_text(_make_line(uuid_=f"agent-{uuid.uuid4()}") + "\n")
    root = tmp_path / "heads"
    root.mkdir()

    stats = await token_harvester.run_harvest(
        session, agent_base_paths=[str(agents)], boss_base_paths=[], agent_slug_map={},
        task_workspace_map={}, heads_root=str(root),
    )

    assert stats["source_errors"] == 1
    assert stats["new_events"] == 1


async def test_head_rows_are_liveness_evidence_for_their_task(session, tmp_path, make_board, make_task):
    """Deliberate: a card a head is working on counts as active for the
    stuck-block and the silent-card watchdog (task_evidence)."""
    from app.services.task_evidence import latest_model_event_at
    from app.utils import ensure_aware

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Held by a head")
    root = tmp_path / "heads"
    run_id = make_run(root, task_id=str(task.id), locality="local")
    _omp_session(root / run_id, [_make_omp_line(short_id="l1", response_id=f"chatcmpl-l-{run_id}")])
    assert await latest_model_event_at(session, task.id) is None

    await _harvest(session, root)

    (event,) = await _events(session)
    assert await latest_model_event_at(session, task.id) == ensure_aware(event.ts)


def _usage(harness: str, cost: float, agent_id=None, locality=None, head_run_id=None) -> ModelUsageEvent:
    from datetime import datetime, timezone

    return ModelUsageEvent(
        id=uuid.uuid4(), harness=harness, agent_id=agent_id, model="m", session_id="s",
        message_uuid=f"cost-{uuid.uuid4()}", input_tokens=100, output_tokens=10,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=cost,
        ts=datetime.now(timezone.utc), source_file="/f.jsonl",
        locality=locality, head_run_id=head_run_id,
    )


async def test_insights_costs_show_heads_apart_from_unattributed(auth_client, session):
    session.add(_usage("host", 1.0))  # boss line without cwd match
    session.add(_usage("head-omp", 0.25, locality="local", head_run_id=str(uuid.uuid4())))
    session.add(_usage("head-claude", 0.5, locality="local", head_run_id=str(uuid.uuid4())))
    await session.commit()

    resp = await auth_client.get("/api/v1/intelligence/costs?days=30&include_sessions=true")

    assert resp.status_code == 200
    rows = {r["agent_name"]: r for r in resp.json()["agents"]}
    assert rows["Heads"]["event_count"] == 2
    assert abs(rows["Heads"]["cost_usd"] - 0.75) < 1e-6
    assert rows["Unattributed"]["event_count"] == 1
    # distinct ids: the Insights table keys its rows by agent_id
    assert rows["Heads"]["agent_id"] != rows["Unattributed"]["agent_id"]
    assert {s["agent_name"] for s in resp.json()["sessions"]} >= {"Heads", "Unattributed"}
