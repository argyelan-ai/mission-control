"""bench_studio router — /api/v1/bench/* (operator JWT).

Also proves ADR-044 discovery end-to-end: the routes exist on app.main.app
purely because the vertical directory is present (core never imports it).
"""
import uuid
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel import select

from app.models.bench import BenchChallenge, BenchEntry
from app.verticals.bench_studio import orchestrator


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    """Background entrypoints open their own session on the prod engine —
    never let them run inside tests."""
    monkeypatch.setattr(orchestrator, "start_challenge", AsyncMock())
    monkeypatch.setattr(orchestrator, "rerender_challenge", AsyncMock())
    monkeypatch.setattr(orchestrator, "recompose_challenge", AsyncMock())
    monkeypatch.setattr(orchestrator, "retry_entry", AsyncMock())


def _create_body(**over):
    body = {
        "title": "Bouncing balls",
        "prompt_text": "100 bouncing balls, one index.html",
        "mode": "side_by_side",
        "models": [
            {"label": "DeepSeek", "source_kind": "spark", "spark_model": "deepseek-x"},
            {"label": "Claude", "source_kind": "agent",
             "agent_id": str(uuid.uuid4())},
        ],
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_create_challenge_freezes_prompt_and_fans_out(auth_client):
    resp = await auth_client.post("/api/v1/bench/challenges", json=_create_body())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "generating"
    assert data["prompt_text"] == "100 bouncing balls, one index.html"
    assert len(data["entries"]) == 2
    assert {e["status"] for e in data["entries"]} == {"pending"}
    orchestrator.start_challenge.assert_called_once()  # create_task schedules; may not be awaited yet


@pytest.mark.asyncio
async def test_create_challenge_persists_display_tag(auth_client):
    """Optional per-entry display_tag is persisted (blank -> NULL)."""
    body = _create_body()
    body["models"][0]["display_tag"] = "OMP · DGX SPARK"
    body["models"][1]["display_tag"] = "   "  # whitespace-only -> normalized to NULL
    resp = await auth_client.post("/api/v1/bench/challenges", json=body)
    assert resp.status_code == 201, resp.text
    tags = {e["model_label"]: e["display_tag"] for e in resp.json()["entries"]}
    assert tags["DeepSeek"] == "OMP · DGX SPARK"
    assert tags["Claude"] is None


@pytest.mark.asyncio
async def test_create_challenge_requires_prompt(auth_client):
    resp = await auth_client.post(
        "/api/v1/bench/challenges", json=_create_body(prompt_text=None)
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_challenge_agent_without_id_400(auth_client):
    body = _create_body()
    body["models"][1].pop("agent_id")
    resp = await auth_client.post("/api/v1/bench/challenges", json=body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_challenge_from_template_freezes_copy(auth_client, session):
    from app.models.prompt_template import PromptTemplate

    tpl = PromptTemplate(title="Balls", body="frozen body", tags=["3d"])
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)

    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_create_body(prompt_text=None, prompt_template_id=str(tpl.id)),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["prompt_text"] == "frozen body"
    assert resp.json()["prompt_template_id"] == str(tpl.id)


@pytest.mark.asyncio
async def test_create_challenge_edited_text_wins_over_template_body(auth_client, session):
    """When template_id is set AND prompt_text is provided (user edited),
    the edited text should be used, not the template body. Template ID is kept for provenance."""
    from app.models.prompt_template import PromptTemplate

    tpl = PromptTemplate(title="Balls", body="original template body", tags=["3d"])
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)

    edited_text = "My edited version of the prompt"
    resp = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_create_body(
            prompt_text=edited_text,
            prompt_template_id=str(tpl.id)
        ),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["prompt_text"] == edited_text, "Edited text should win over template body"
    assert data["prompt_template_id"] == str(tpl.id), "Template ID should be preserved for provenance"


@pytest.mark.asyncio
async def test_series_numbering_increments_per_label(auth_client):
    r1 = await auth_client.post(
        "/api/v1/bench/challenges", json=_create_body(series_label="Spark Bench")
    )
    r2 = await auth_client.post(
        "/api/v1/bench/challenges",
        json=_create_body(title="Round 2", series_label="Spark Bench"),
    )
    assert r1.json()["series_no"] == 1
    assert r2.json()["series_no"] == 2


@pytest.mark.asyncio
async def test_list_and_detail(auth_client, session, monkeypatch):
    ch = BenchChallenge(title="T", prompt_text="p", status="review")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    session.add(BenchEntry(challenge_id=ch.id, model_label="A",
                           source_kind="spark", status="rendered",
                           video_path="/sd/a.mp4"))
    await session.commit()

    listing = await auth_client.get("/api/v1/bench/challenges")
    assert listing.status_code == 200
    assert any(c["id"] == str(ch.id) for c in listing.json())

    # Monkeypatch reconcile_challenge and assert it was awaited by detail call
    monkeypatch.setattr(orchestrator, "reconcile_challenge", AsyncMock())
    detail = await auth_client.get(f"/api/v1/bench/challenges/{ch.id}")
    assert detail.status_code == 200
    assert detail.json()["entries"][0]["model_label"] == "A"
    orchestrator.reconcile_challenge.assert_called_once()


@pytest.mark.asyncio
async def test_detail_404(auth_client):
    resp = await auth_client.get(f"/api/v1/bench/challenges/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_draft_endpoint(auth_client, session, monkeypatch, make_board):
    from app.services.x_publisher import DraftValidation
    from app.verticals.bench_studio import drafts, routers

    monkeypatch.setattr(
        drafts.x_publisher, "validate_media",
        lambda paths: DraftValidation(ok=True), raising=False,
    )
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    ch = BenchChallenge(title="T", prompt_text="p", status="review",
                        composed_video_path="/sd/grid.mp4")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    session.add(BenchEntry(challenge_id=ch.id, model_label="A",
                           source_kind="spark", status="rendered",
                           video_path="/sd/a.mp4"))
    await session.commit()

    resp = await auth_client.post(
        f"/api/v1/bench/challenges/{ch.id}/draft",
        json={"tweet_text": "hello bench", "include_speed_labels": False,
              "board_id": str(board.id)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["challenge_status"] == "drafted"
    assert uuid.UUID(body["approval_id"])


@pytest.mark.asyncio
async def test_rerender_endpoint_gates_status(auth_client, session):
    ch = BenchChallenge(title="T", prompt_text="p", status="generating")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/rerender")
    assert resp.status_code == 409

    ch.status = "review"
    session.add(ch)
    await session.commit()
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/rerender")
    assert resp.status_code == 200
    orchestrator.rerender_challenge.assert_called_once()  # create_task schedules; may not be awaited yet


@pytest.mark.asyncio
async def test_rerender_allowed_from_composing(auth_client, session):
    """Challenges stuck in 'composing' (e.g. after a backend crash) must be
    recoverable via rerender — gate must allow rendering and composing, not
    just review/drafted/failed."""
    ch = BenchChallenge(title="T", prompt_text="p", status="composing")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/rerender")
    assert resp.status_code == 200
    orchestrator.rerender_challenge.assert_called()


@pytest.mark.asyncio
async def test_entry_retry_endpoint_requires_failed(auth_client, session):
    ch = BenchChallenge(title="T", prompt_text="p", status="review")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entry = BenchEntry(challenge_id=ch.id, model_label="A",
                       source_kind="spark", status="rendered")
    session.add(entry)
    await session.commit()
    await session.refresh(entry)

    resp = await auth_client.post(f"/api/v1/bench/entries/{entry.id}/retry")
    assert resp.status_code == 409

    entry.status = "failed"
    session.add(entry)
    await session.commit()
    resp = await auth_client.post(f"/api/v1/bench/entries/{entry.id}/retry")
    assert resp.status_code == 200
    orchestrator.retry_entry.assert_called_once()  # create_task schedules; may not be awaited yet


@pytest.mark.asyncio
async def test_unauthenticated_401(client):
    resp = await client.get("/api/v1/bench/challenges")
    assert resp.status_code == 401


# ── Operator lifecycle: stop / archive / delete (2026-07-12) ──────────────


async def _seed_challenge(session, *, status="review", entries=()):
    ch = BenchChallenge(title="T", prompt_text="p", status=status)
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    rows = []
    for spec in entries:
        e = BenchEntry(challenge_id=ch.id, **spec)
        session.add(e)
        rows.append(e)
    await session.commit()
    for e in rows:
        await session.refresh(e)
    return ch, rows


@pytest.mark.asyncio
async def test_stop_challenge_409_when_not_running(auth_client, session):
    ch, _ = await _seed_challenge(session, status="review")
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/stop")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_stop_challenge_fails_open_entries_keeps_terminal(auth_client, session):
    ch, entries = await _seed_challenge(
        session,
        status="generating",
        entries=[
            {"model_label": "A", "source_kind": "spark", "status": "generating"},
            {"model_label": "B", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/b.mp4"},
        ],
    )
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/stop")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "failed"
    assert data["error"] == "stopped by operator"
    by_label = {e["model_label"]: e for e in data["entries"]}
    assert by_label["A"]["status"] == "failed"
    assert by_label["A"]["error"] == "stopped by operator"
    # Rendered entry keeps its state:
    assert by_label["B"]["status"] == "rendered"
    assert by_label["B"]["error"] is None


@pytest.mark.asyncio
async def test_stop_challenge_stops_open_fleet_task(auth_client, session, make_board, make_task):
    """Agent entry mid-generation -> its fleet task is stopped through the
    same mechanism as the Tasks-UI stop button (run_control='stopped')."""
    from app.models.task import Task
    from datetime import datetime, timezone

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(
        board.id, title="[Bench] running", status="in_progress",
        dispatched_at=datetime.now(timezone.utc),
    )
    ch, _ = await _seed_challenge(
        session,
        status="generating",
        entries=[
            {"model_label": "A", "source_kind": "agent", "status": "generating",
             "task_id": task.id},
        ],
    )
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/stop")
    assert resp.status_code == 200, resp.text

    stopped = await session.get(Task, task.id)
    await session.refresh(stopped)
    assert stopped.run_control == "stopped"
    assert stopped.status == "blocked"


@pytest.mark.asyncio
async def test_archive_unarchive_and_list_filtering(auth_client, session):
    ch, _ = await _seed_challenge(session, status="review")

    # Running challenges cannot be archived:
    running, _ = await _seed_challenge(session, status="generating")
    resp = await auth_client.post(f"/api/v1/bench/challenges/{running.id}/archive")
    assert resp.status_code == 409

    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_at"] is not None

    # Default listing hides it; include_archived returns it:
    listing = await auth_client.get("/api/v1/bench/challenges")
    assert all(c["id"] != str(ch.id) for c in listing.json())
    listing_all = await auth_client.get("/api/v1/bench/challenges?include_archived=true")
    assert any(c["id"] == str(ch.id) for c in listing_all.json())

    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/unarchive")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None
    listing = await auth_client.get("/api/v1/bench/challenges")
    assert any(c["id"] == str(ch.id) for c in listing.json())


@pytest.mark.asyncio
async def test_delete_challenge_409_while_running(auth_client, session):
    ch, _ = await _seed_challenge(session, status="rendering")
    resp = await auth_client.delete(f"/api/v1/bench/challenges/{ch.id}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_challenge_removes_rows_and_artifact_dir(
    auth_client, session, tmp_path, monkeypatch
):
    from sqlmodel import select as _select

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    ch, _ = await _seed_challenge(
        session,
        status="failed",
        entries=[{"model_label": "A", "source_kind": "spark", "status": "failed"}],
    )
    art_dir = tmp_path / f"bench-{ch.id}"
    art_dir.mkdir(parents=True)
    (art_dir / "grid.mp4").write_bytes(b"x")

    ch_id = ch.id  # capture before expire_all (expired attrs can't lazy-load async)
    resp = await auth_client.delete(f"/api/v1/bench/challenges/{ch_id}")
    assert resp.status_code == 204

    # The app deleted through its own session — drop this session's identity
    # map before re-reading, otherwise the cached row masks the delete.
    session.expire_all()
    assert await session.get(BenchChallenge, ch_id) is None
    remaining = (
        await session.exec(_select(BenchEntry).where(BenchEntry.challenge_id == ch_id))
    ).all()
    assert remaining == []
    assert not art_dir.exists()
    # The shared root itself must survive:
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_delete_challenge_artifacts_never_leaves_root(tmp_path, monkeypatch):
    """Containment guard: a challenge_dir that resolves outside the shared
    root (or to the root itself) is never deleted."""
    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    outside = tmp_path.parent / "outside-marker"
    outside.mkdir(exist_ok=True)

    # Point challenge_dir at the root itself -> refused.
    monkeypatch.setattr(orchestrator, "challenge_dir", lambda _id: tmp_path)
    orchestrator.delete_challenge_artifacts(uuid.uuid4())
    assert tmp_path.exists()

    # Point it outside the root -> refused.
    monkeypatch.setattr(orchestrator, "challenge_dir", lambda _id: outside)
    orchestrator.delete_challenge_artifacts(uuid.uuid4())
    assert outside.exists()


# ── edit + recompose (2026-07-12) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_challenge_title(auth_client, session):
    ch, _ = await _seed_challenge(session, status="review")
    resp = await auth_client.patch(
        f"/api/v1/bench/challenges/{ch.id}", json={"title": "Better Title"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Better Title"


@pytest.mark.asyncio
async def test_patch_challenge_409_while_running(auth_client, session):
    ch, _ = await _seed_challenge(session, status="composing")
    resp = await auth_client.patch(
        f"/api/v1/bench/challenges/{ch.id}", json={"title": "Nope"}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_entry_label_and_tag(auth_client, session):
    ch, entries = await _seed_challenge(
        session, status="review",
        entries=[{"model_label": "Old", "source_kind": "spark", "status": "rendered",
                  "video_path": "/sd/a.mp4", "display_tag": "OLD TAG"}],
    )
    entry = entries[0]
    resp = await auth_client.patch(
        f"/api/v1/bench/entries/{entry.id}",
        json={"model_label": "Qwen 3.6", "display_tag": "OMP · DGX SPARK"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model_label"] == "Qwen 3.6"
    assert data["display_tag"] == "OMP · DGX SPARK"

    # Empty display_tag clears the override (harness default applies again):
    resp = await auth_client.patch(
        f"/api/v1/bench/entries/{entry.id}", json={"display_tag": ""}
    )
    assert resp.status_code == 200
    assert resp.json()["display_tag"] is None
    # Omitting a field leaves it untouched:
    assert resp.json()["model_label"] == "Qwen 3.6"


@pytest.mark.asyncio
async def test_patch_entry_409_while_running(auth_client, session):
    ch, entries = await _seed_challenge(
        session, status="rendering",
        entries=[{"model_label": "A", "source_kind": "spark", "status": "rendered"}],
    )
    resp = await auth_client.patch(
        f"/api/v1/bench/entries/{entries[0].id}", json={"model_label": "B"}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_recompose_endpoint(auth_client, session):
    ch, _ = await _seed_challenge(
        session, status="review",
        entries=[
            {"model_label": "A", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/a.mp4"},
            {"model_label": "B", "source_kind": "spark", "status": "rendered",
             "video_path": "/sd/b.mp4"},
        ],
    )
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/recompose")
    assert resp.status_code == 200, resp.text
    orchestrator.recompose_challenge.assert_called_once()


@pytest.mark.asyncio
async def test_recompose_endpoint_guards(auth_client, session):
    # Mid-run -> 409:
    running, _ = await _seed_challenge(session, status="composing")
    resp = await auth_client.post(f"/api/v1/bench/challenges/{running.id}/recompose")
    assert resp.status_code == 409

    # Not enough recordings -> 422:
    ch, _ = await _seed_challenge(
        session, status="review",
        entries=[{"model_label": "A", "source_kind": "spark", "status": "rendered",
                  "video_path": "/sd/a.mp4"}],
    )
    resp = await auth_client.post(f"/api/v1/bench/challenges/{ch.id}/recompose")
    assert resp.status_code == 422
    orchestrator.recompose_challenge.assert_not_called()
