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
