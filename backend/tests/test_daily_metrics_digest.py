"""Tests for app.services.daily_metrics_digest — "Vier Messzahlen, taeglich".

RED-Phase (Lauf 3, Arbeiter "Tests"): das Modul existiert noch nicht.
Alle Tests hier muessen mit ModuleNotFoundError/ImportError/AttributeError
fehlschlagen, bis der Umsetzer app/services/daily_metrics_digest.py baut.

Fixture-Hinweise fuer den Umsetzer:
- Events/Tasks werden direkt ueber SQLModel-Instanzen + AsyncSession(test_engine)
  angelegt (Muster tests/test_task_events.py), nicht ueber die Factories aus
  conftest.py (die decken activity_events/task_events nicht ab).
- Agenten mit is_board_lead=True ueber make_agent(is_board_lead=True) erzeugt.
- Redis wird NICHT ueber die echte get_redis()/fakeredis-Fixture verwendet,
  sondern direkt gemockt: `monkeypatch.setattr(daily_metrics_digest_module,
  "get_redis", AsyncMock(return_value=fake_redis))` mit einem simplen
  In-Memory-Fake-Objekt (get/set als AsyncMock mit Dict-Backing) — schneller
  und expliziter als fakeredis fuer die Dedup-Assertions.
- send_report wird als AsyncMock gepatcht: `monkeypatch.setattr(
  "app.services.daily_metrics_digest.send_report", AsyncMock())`.
- test_background_starts_digest folgt dem Quelltext-Inspektions-Muster aus
  tests/test_background_services_flag.py (kein echtes .start() ausfuehren,
  nur pruefen dass app.background den Singleton kennt + aufruft).
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.utils import utcnow
from tests.conftest import test_engine


# ── Helpers ────────────────────────────────────────────────────────────

async def _add_task_event(task_id, *, changed_by="agent", created_at=None, reason=None):
    from app.models.task import TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        ev = TaskEvent(
            id=uuid.uuid4(),
            task_id=task_id,
            from_status="in_progress",
            to_status="review",
            changed_by=changed_by,
            reason=reason,
        )
        if created_at is not None:
            ev.created_at = created_at
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
        return ev


async def _add_task_comment(task_id, *, created_at=None):
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        c = TaskComment(
            id=uuid.uuid4(),
            task_id=task_id,
            author_type="agent",
            content="progress update",
        )
        if created_at is not None:
            c.created_at = created_at
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c


async def _add_activity_event(event_type, *, task_id=None, agent_id=None, created_at=None):
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        ev = ActivityEvent(
            id=uuid.uuid4(),
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            title=event_type,
        )
        if created_at is not None:
            ev.created_at = created_at
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
        return ev


class _FakeRedis:
    """Minimal In-Memory-Fake fuer get/set/delete mit ex=/nx= (TTL wird
    ignoriert). nx=True mimt echtes SET...NX: liefert None statt zu
    ueberschreiben, wenn der Key schon existiert (Nacharbeit Punkt 2: der
    Dedup-Key wird jetzt VOR dem Senden atomar reserviert)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


# ── M1: stale cards ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_cards_over_4h_listed_and_fresh_excluded(make_board, make_task, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    # compute_daily_metrics(session=None) opens its own session via
    # app.database.engine — point that at the SQLite test engine the
    # fixtures above write to (established pattern, e.g.
    # tests/test_agent_create_flow.py:176).
    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    now = utcnow()

    stale = await make_task(
        board.id, title="Stale Card", status="in_progress",
        updated_at=now - timedelta(hours=5),
    )
    fresh = await make_task(
        board.id, title="Fresh Card", status="in_progress",
        updated_at=now - timedelta(hours=1),
    )

    metrics = await compute_daily_metrics(None, now=now)

    stale_ids = {c["id"] for c in metrics["stale_cards"]}
    assert str(stale.id) in stale_ids
    assert str(fresh.id) not in stale_ids
    entry = next(c for c in metrics["stale_cards"] if c["id"] == str(stale.id))
    assert entry["title"] == "Stale Card"
    assert entry["status"] == "in_progress"
    assert entry["hours_still"] >= 4.9


# ── M2: reviews to lead ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reviews_to_lead_counted(make_board, make_task, make_agent, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    lead = await make_agent(name="Lead", is_board_lead=True)
    other = await make_agent(name="Not Lead", is_board_lead=False)
    task1 = await make_task(board.id, title="T1")
    task2 = await make_task(board.id, title="T2")

    await _add_activity_event(
        "task.review_handoff", task_id=task1.id, agent_id=lead.id,
    )
    await _add_activity_event(
        "task.review_handoff", task_id=task2.id, agent_id=other.id,
    )

    metrics = await compute_daily_metrics(None, now=utcnow())

    assert metrics["reviews_total_24h"] == 2
    assert metrics["reviews_to_lead_24h"] == 1


# ── M3: double dispatch vs healer repeat ──────────────────────────────────

@pytest.mark.asyncio
async def test_double_dispatch_vs_healer_repeat(make_board, make_task, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    task = await make_task(board.id, title="Flaky Card")
    now = utcnow()

    # Paar 1: auto_dispatched -> auto_dispatched binnen 15 min = double_dispatch
    await _add_activity_event(
        "task.auto_dispatched", task_id=task.id, created_at=now - timedelta(minutes=30),
    )
    await _add_activity_event(
        "task.auto_dispatched", task_id=task.id, created_at=now - timedelta(minutes=25),
    )
    # Paar 2: auto_dispatched -> undispatched_recovery binnen 15 min = healer_repeat
    await _add_activity_event(
        "task.undispatched_recovery", task_id=task.id, created_at=now - timedelta(minutes=10),
    )

    metrics = await compute_daily_metrics(None, now=now)

    assert metrics["double_dispatch_24h"] == 1
    assert metrics["healer_repeats_24h"] == 1


# ── M4: hand status changes grouped by reason ─────────────────────────────

@pytest.mark.asyncio
async def test_hand_status_changes_grouped_by_reason(make_board, make_task, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    task = await make_task(board.id, title="T")

    await _add_task_event(task.id, changed_by="user", reason="blocked_manually")
    await _add_task_event(task.id, changed_by="user", reason="blocked_manually")
    await _add_task_event(task.id, changed_by="user", reason="priority_bump")
    await _add_task_event(task.id, changed_by="agent", reason="ignored")

    metrics = await compute_daily_metrics(None, now=utcnow())

    assert metrics["hand_status_changes_24h"] == 3
    assert metrics["hand_status_changes_by_reason"] == {
        "blocked_manually": 2,
        "priority_bump": 1,
    }


# ── 24h-Fenster respektiert (Nacharbeit Punkt 4 / Pruefbericht T2) ────────
# Die urspruenglichen M2/M3/M4-Tests legten alle Events auf "jetzt" — der
# Cutoff war ungetestet (strich man ihn, blieben alle 8 Tests gruen). Diese
# drei Tests legen je ein Event ausserhalb (>24h) und eins innerhalb
# (<24h) des Fensters an und sind vor dem N4-Fix am Cutoff sabotierbar.

@pytest.mark.asyncio
async def test_hand_status_changes_respects_24h_window(make_board, make_task, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    task = await make_task(board.id, title="T")
    now = utcnow()

    await _add_task_event(
        task.id, changed_by="user", reason="too_old", created_at=now - timedelta(hours=25),
    )
    await _add_task_event(
        task.id, changed_by="user", reason="fresh_enough", created_at=now - timedelta(hours=23),
    )

    metrics = await compute_daily_metrics(None, now=now)

    assert metrics["hand_status_changes_24h"] == 1
    assert metrics["hand_status_changes_by_reason"] == {"fresh_enough": 1}


@pytest.mark.asyncio
async def test_reviews_to_lead_respects_24h_window(make_board, make_task, make_agent, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    lead = await make_agent(name="Lead", is_board_lead=True)
    task = await make_task(board.id, title="T")
    now = utcnow()

    await _add_activity_event(
        "task.review_handoff", task_id=task.id, agent_id=lead.id,
        created_at=now - timedelta(hours=25),
    )
    await _add_activity_event(
        "task.review_handoff", task_id=task.id, agent_id=lead.id,
        created_at=now - timedelta(hours=23),
    )

    metrics = await compute_daily_metrics(None, now=now)

    assert metrics["reviews_total_24h"] == 1
    assert metrics["reviews_to_lead_24h"] == 1


@pytest.mark.asyncio
async def test_dispatch_pairs_respects_24h_window(make_board, make_task, monkeypatch):
    from app.services.daily_metrics_digest import compute_daily_metrics

    monkeypatch.setattr("app.database.engine", test_engine)

    board = await make_board()
    task = await make_task(board.id, title="Flaky Card")
    now = utcnow()

    # Beide Events und damit auch das Paar liegen komplett ausserhalb des
    # 24h-Fensters -> darf weder als double_dispatch noch healer_repeat zaehlen.
    await _add_activity_event(
        "task.auto_dispatched", task_id=task.id,
        created_at=now - timedelta(hours=25, minutes=10),
    )
    await _add_activity_event(
        "task.auto_dispatched", task_id=task.id,
        created_at=now - timedelta(hours=25),
    )

    metrics = await compute_daily_metrics(None, now=now)

    assert metrics["double_dispatch_24h"] == 0
    assert metrics["healer_repeats_24h"] == 0


# ── format_digest ──────────────────────────────────────────────────────

def test_format_digest_is_short_ascii():
    """Nacharbeit Punkt 5 (Pruefbericht T1): die Nullwerte "3"/"2"/"1"/"4"
    aus der urspruenglichen Probe standen schon trivial im Digest-Geruest
    (Datum, "M3:", "4 h still") — jetzt eindeutige, nicht ueberlappende
    Zahlen je Messzahl, gegen die konkrete Zeile geprueft, nicht nur "in
    text"."""
    from app.services.daily_metrics_digest import format_digest

    metrics = {
        "stale_cards": [
            {"id": "x", "title": "Some Card " * 6, "status": "in_progress", "hours_still": 5.2},
        ],
        "reviews_to_lead_24h": 9,
        "reviews_total_24h": 13,
        "double_dispatch_24h": 6,
        "healer_repeats_24h": 8,
        "hand_status_changes_24h": 11,
        "hand_status_changes_by_reason": {"blocked_manually": 7, "priority_bump": 4},
    }

    text = format_digest(metrics)

    lines = text.splitlines()
    assert len(lines) <= 14
    assert text.isascii()
    assert "5.2" in text or "5,2" in text  # stale hours
    assert "M2: 9 von 13 Reviews beim Lead" in text
    assert "M3: 6 Doppel-Dispatch, 8 Healer-Wiederholungen" in text
    assert "M4: 11 Hand-Statuswechsel" in text


def test_format_digest_umlaut_title_becomes_ascii():
    """Nacharbeit Punkt 1 (Blocker B1): ein Kartentitel mit Umlauten darf
    format_digest nicht scheitern lassen (das alte `assert text.isascii()`
    warf live bei 482/1134 Kartentiteln)."""
    from app.services.daily_metrics_digest import format_digest

    metrics = {
        "stale_cards": [
            {
                "id": "x",
                "title": "seitwärts groß",
                "status": "in_progress",
                "hours_still": 29.0,
            },
        ],
        "reviews_to_lead_24h": 0,
        "reviews_total_24h": 0,
        "double_dispatch_24h": 0,
        "healer_repeats_24h": 0,
        "hand_status_changes_24h": 0,
        "hand_status_changes_by_reason": {},
    }

    text = format_digest(metrics)

    assert text.isascii()
    assert "seitwaerts gross" in text


# ── DailyMetricsDigest scheduling ────────────────────────────────────────

@pytest.mark.asyncio
async def test_maybe_send_once_per_day_and_respects_hour(monkeypatch):
    import app.services.daily_metrics_digest as mod

    fake_redis = _FakeRedis()
    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=fake_redis))
    monkeypatch.setattr(mod, "send_report", AsyncMock(return_value=(True, [])))
    monkeypatch.setattr(
        mod, "compute_daily_metrics",
        AsyncMock(return_value={
            "stale_cards": [], "reviews_to_lead_24h": 0, "reviews_total_24h": 0,
            "double_dispatch_24h": 0, "healer_repeats_24h": 0,
            "hand_status_changes_24h": 0, "hand_status_changes_by_reason": {},
        }),
    )
    monkeypatch.setattr(mod.settings, "daily_metrics_hour", 7)
    monkeypatch.setattr(mod.settings, "daily_metrics_enabled", True)

    digest = mod.DailyMetricsDigest()

    before_hour = utcnow().replace(hour=6, minute=0, second=0, microsecond=0)
    await digest._maybe_send(before_hour)
    mod.send_report.assert_not_called()

    after_hour = before_hour.replace(hour=7, minute=30)
    await digest._maybe_send(after_hour)
    mod.send_report.assert_called_once()

    # Zweiter Aufruf am selben Kalendertag -> kein weiterer Versand.
    later_same_day = after_hour.replace(hour=20)
    await digest._maybe_send(later_same_day)
    mod.send_report.assert_called_once()


@pytest.mark.asyncio
async def test_disabled_flag_sends_nothing(monkeypatch):
    import app.services.daily_metrics_digest as mod

    fake_redis = _FakeRedis()
    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=fake_redis))
    monkeypatch.setattr(mod, "send_report", AsyncMock(return_value=(True, [])))
    monkeypatch.setattr(
        mod, "compute_daily_metrics",
        AsyncMock(return_value={
            "stale_cards": [], "reviews_to_lead_24h": 0, "reviews_total_24h": 0,
            "double_dispatch_24h": 0, "healer_repeats_24h": 0,
            "hand_status_changes_24h": 0, "hand_status_changes_by_reason": {},
        }),
    )
    monkeypatch.setattr(mod.settings, "daily_metrics_hour", 7)
    monkeypatch.setattr(mod.settings, "daily_metrics_enabled", False)

    digest = mod.DailyMetricsDigest()
    after_hour = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    await digest._maybe_send(after_hour)

    mod.send_report.assert_not_called()


def test_background_starts_digest():
    """app.background wiring: daily_metrics_digest.start() neben intelligence.start()
    (Muster tests/test_background_services_flag.py — Quelltext-Inspektion statt
    echtem Start, weil kein Postgres/Redis-Service in CI laeuft)."""
    import inspect

    import app.background as bg_mod
    from app.services.daily_metrics_digest import daily_metrics_digest

    assert bg_mod.daily_metrics_digest is daily_metrics_digest
    src = inspect.getsource(bg_mod.start_background_services)
    assert "await daily_metrics_digest.start()" in src

    stop_src = inspect.getsource(bg_mod.stop_background_services)
    assert "daily_metrics_digest" in stop_src
