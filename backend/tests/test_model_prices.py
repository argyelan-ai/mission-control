"""Tests fuer model_prices CRUD + unmatched + recompute (admin-only) + Aggregat-Endpoints."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.models.model_usage import ModelPrice, ModelUsageEvent


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def price_payload():
    return {
        "model_pattern": "claude-sonnet-4-*",
        "input_per_mtok": 3.0,
        "output_per_mtok": 15.0,
        "cache_read_per_mtok": 0.3,
        "cache_write_per_mtok": 3.75,
        "priority": 80,
        "valid_from": "2026-01-01T00:00:00Z",
        "note": "Test price",
    }


# ── GET /model-prices ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_prices_admin_ok(auth_client: AsyncClient, session, price_payload):
    """Admin kann Preise auflisten."""
    price = ModelPrice(
        id=uuid.uuid4(),
        model_pattern="claude-opus-4-*",
        input_per_mtok=15.0,
        output_per_mtok=75.0,
        cache_read_per_mtok=1.5,
        cache_write_per_mtok=18.75,
        priority=90,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    session.add(price)
    await session.commit()

    resp = await auth_client.get("/api/v1/model-prices")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(p["model_pattern"] == "claude-opus-4-*" for p in data)


@pytest.mark.asyncio
async def test_list_prices_unauthenticated_fails(client: AsyncClient):
    """Kein Token → 401."""
    resp = await client.get("/api/v1/model-prices")
    assert resp.status_code in (401, 403)


# ── POST /model-prices ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_price_admin(auth_client: AsyncClient, price_payload):
    """Admin kann Preis erstellen."""
    resp = await auth_client.post("/api/v1/model-prices", json=price_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["model_pattern"] == "claude-sonnet-4-*"
    assert data["input_per_mtok"] == 3.0
    assert "id" in data


@pytest.mark.asyncio
async def test_create_price_unauthenticated_fails(client: AsyncClient, price_payload):
    """Kein Token → 401."""
    resp = await client.post("/api/v1/model-prices", json=price_payload)
    assert resp.status_code in (401, 403)


# ── PATCH /model-prices/{id} ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_price_admin(auth_client: AsyncClient, session):
    """Admin kann Preis updaten."""
    price = ModelPrice(
        id=uuid.uuid4(),
        model_pattern="qwen*",
        input_per_mtok=0.0,
        output_per_mtok=0.0,
        cache_read_per_mtok=0.0,
        cache_write_per_mtok=0.0,
        priority=10,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        note="local",
    )
    session.add(price)
    await session.commit()

    resp = await auth_client.patch(
        f"/api/v1/model-prices/{price.id}",
        json={"note": "updated note", "priority": 20},
    )
    assert resp.status_code == 200
    assert resp.json()["note"] == "updated note"
    assert resp.json()["priority"] == 20


@pytest.mark.asyncio
async def test_update_price_not_found(auth_client: AsyncClient):
    """404 wenn Preis nicht existiert."""
    resp = await auth_client.patch(
        f"/api/v1/model-prices/{uuid.uuid4()}",
        json={"note": "x"},
    )
    assert resp.status_code == 404


# ── DELETE /model-prices/{id} ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_price_admin(auth_client: AsyncClient, session):
    """Admin kann Preis loeschen."""
    price = ModelPrice(
        id=uuid.uuid4(),
        model_pattern="to-delete-*",
        input_per_mtok=0.0,
        output_per_mtok=0.0,
        cache_read_per_mtok=0.0,
        cache_write_per_mtok=0.0,
        priority=5,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    session.add(price)
    await session.commit()

    resp = await auth_client.delete(f"/api/v1/model-prices/{price.id}")
    assert resp.status_code == 204

    resp2 = await auth_client.get("/api/v1/model-prices")
    assert not any(p["id"] == str(price.id) for p in resp2.json())


# ── GET /model-prices/unmatched ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_unmatched_models(auth_client: AsyncClient, session):
    """Modelle in model_usage_events ohne passendes Preis-Pattern → unmatched."""
    # Seed: Ein Preis fuer claude-*, kein Preis fuer minimax-*
    price = ModelPrice(
        id=uuid.uuid4(),
        model_pattern="claude-*",
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.3,
        cache_write_per_mtok=3.75,
        priority=80,
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    session.add(price)

    # Dynamisch statt eingefroren — ein fixes Datum faellt irgendwann
    # aus dem days=30-Fenster der costs-Endpoints (Zeitbomben-Bug).
    now = datetime.now(timezone.utc) - timedelta(days=1)
    # Ein Event mit bekanntem Modell (claude) → NICHT unmatched
    e1 = ModelUsageEvent(
        id=uuid.uuid4(),
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        session_id="s1",
        message_uuid="um-001",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        ts=now,
        source_file="/f1.jsonl",
    )
    # Ein Event mit unbekanntem Modell (minimax) → unmatched
    e2 = ModelUsageEvent(
        id=uuid.uuid4(),
        harness="cli-bridge",
        model="minimax-m2.7",
        session_id="s2",
        message_uuid="um-002",
        input_tokens=28000,
        output_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        ts=now,
        source_file="/f2.jsonl",
    )
    session.add(e1)
    session.add(e2)
    await session.commit()

    resp = await auth_client.get("/api/v1/model-prices/unmatched")
    assert resp.status_code == 200
    data = resp.json()
    models = [m["model"] for m in data]
    assert "minimax-m2.7" in models
    assert "claude-sonnet-4-6" not in models
    # Event-Count und Token-Summen
    mm = next(m for m in data if m["model"] == "minimax-m2.7")
    assert mm["event_count"] >= 1
    assert mm["total_input_tokens"] >= 28000


# ── POST /model-prices/recompute ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_recompute_costs(auth_client: AsyncClient, session):
    """recompute berechnet cost_usd aller Events mit aktueller Preistabelle neu."""
    price = ModelPrice(
        id=uuid.uuid4(),
        model_pattern="claude-sonnet-4-*",
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.0,
        cache_write_per_mtok=0.0,
        priority=80,
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    session.add(price)

    # Dynamisch statt eingefroren — ein fixes Datum faellt irgendwann
    # aus dem days=30-Fenster der costs-Endpoints (Zeitbomben-Bug).
    now = datetime.now(timezone.utc) - timedelta(days=1)
    event = ModelUsageEvent(
        id=uuid.uuid4(),
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        session_id="s-rc",
        message_uuid="rc-001",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=None,  # Noch nicht berechnet
        ts=now,
        source_file="/f.jsonl",
    )
    session.add(event)
    await session.commit()

    resp = await auth_client.post("/api/v1/model-prices/recompute", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] >= 1

    # Event muss jetzt cost_usd=3.0 haben (1M Input * $3.0/Mtok)
    # expire_on_commit=False → Session cached den alten Stand.
    # expire_all() ist synchron (kein await noetig).
    from sqlmodel import select
    from app.models.model_usage import ModelUsageEvent as MUE
    session.expire_all()
    result = await session.exec(select(MUE).where(MUE.message_uuid == "rc-001"))
    e = result.one()
    assert e.cost_usd is not None
    assert abs(e.cost_usd - 3.0) < 0.001


# ── Aggregat-Endpoints ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_costs_by_model(auth_client: AsyncClient, session):
    """GET /intelligence/costs/by-model → pro Modell: events, tokens, cost."""
    # Dynamisch statt eingefroren — ein fixes Datum faellt irgendwann
    # aus dem days=30-Fenster der costs-Endpoints (Zeitbomben-Bug).
    now = datetime.now(timezone.utc) - timedelta(days=1)
    e1 = ModelUsageEvent(
        id=uuid.uuid4(),
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        session_id="s1",
        message_uuid="bm-001",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.05,
        ts=now,
        source_file="/f1.jsonl",
    )
    e2 = ModelUsageEvent(
        id=uuid.uuid4(),
        harness="cli-bridge",
        model="claude-sonnet-4-6",
        session_id="s2",
        message_uuid="bm-002",
        input_tokens=2000,
        output_tokens=1000,
        cache_read_tokens=100,
        cache_write_tokens=50,
        cost_usd=0.10,
        ts=now,
        source_file="/f2.jsonl",
    )
    e3 = ModelUsageEvent(
        id=uuid.uuid4(),
        harness="host",
        model="claude-opus-4-8",
        session_id="s3",
        message_uuid="bm-003",
        input_tokens=5000,
        output_tokens=2000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.50,
        ts=now,
        source_file="/f3.jsonl",
    )
    session.add(e1)
    session.add(e2)
    session.add(e3)
    await session.commit()

    resp = await auth_client.get("/api/v1/intelligence/costs/by-model?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)

    models = {row["model"]: row for row in data}
    assert "claude-sonnet-4-6" in models
    assert "claude-opus-4-8" in models

    sonnet = models["claude-sonnet-4-6"]
    assert sonnet["event_count"] == 2
    assert sonnet["input_tokens"] == 3000
    assert sonnet["output_tokens"] == 1500
    assert sonnet["cache_read_tokens"] == 100
    assert abs(sonnet["cost_usd"] - 0.15) < 0.001


@pytest.mark.asyncio
async def test_costs_timeseries(auth_client: AsyncClient, session):
    """GET /intelligence/costs/timeseries → pro Tag: tokens_in, tokens_out, cost."""
    # Dynamisch statt eingefroren — fixe Daten fallen irgendwann aus dem
    # days=30-Fenster des Endpoints (Zeitbomben-Bug).
    day2 = datetime.now(timezone.utc) - timedelta(days=1)
    day1 = day2 - timedelta(days=1)
    e1 = ModelUsageEvent(
        id=uuid.uuid4(), harness="cli-bridge", model="m1",
        session_id="s1", message_uuid="ts-001",
        input_tokens=1000, output_tokens=500,
        cache_read_tokens=0, cache_write_tokens=0,
        cost_usd=0.01, ts=day1, source_file="/f.jsonl",
    )
    e2 = ModelUsageEvent(
        id=uuid.uuid4(), harness="cli-bridge", model="m1",
        session_id="s2", message_uuid="ts-002",
        input_tokens=2000, output_tokens=1000,
        cache_read_tokens=0, cache_write_tokens=0,
        cost_usd=0.02, ts=day2, source_file="/f.jsonl",
    )
    session.add(e1)
    session.add(e2)
    await session.commit()

    resp = await auth_client.get("/api/v1/intelligence/costs/timeseries?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # Mindestens 2 Tage
    assert len(data) >= 2
    # Felder vorhanden
    for row in data:
        assert "date" in row
        assert "tokens_in" in row
        assert "tokens_out" in row
        assert "cost_usd" in row


@pytest.mark.asyncio
async def test_costs_by_task(auth_client: AsyncClient, session):
    """GET /intelligence/costs/by-task → teuerste Tasks (task_id not null)."""
    from app.models.task import Task
    from app.models.board import Board

    # Board + Task erstellen
    board = Board(name="Test Board", slug="test-board-cost")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    task = Task(
        title="Expensive Task",
        board_id=board.id,
        status="done",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Dynamisch statt eingefroren — ein fixes Datum faellt irgendwann
    # aus dem days=30-Fenster der costs-Endpoints (Zeitbomben-Bug).
    now = datetime.now(timezone.utc) - timedelta(days=1)
    e1 = ModelUsageEvent(
        id=uuid.uuid4(),
        task_id=task.id,
        harness="cli-bridge",
        model="claude-opus-4-8",
        session_id="s1",
        message_uuid="bt-001",
        input_tokens=10000,
        output_tokens=5000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=2.50,
        ts=now,
        source_file="/f.jsonl",
    )
    session.add(e1)
    await session.commit()

    resp = await auth_client.get("/api/v1/intelligence/costs/by-task?days=30&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1

    task_row = next((r for r in data if r["task_id"] == str(task.id)), None)
    assert task_row is not None
    assert task_row["task_title"] == "Expensive Task"
    assert abs(task_row["cost_usd"] - 2.50) < 0.001
