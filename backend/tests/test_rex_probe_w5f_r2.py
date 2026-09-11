"""REX-Proben Runde 2 zu PR #510 — prueft die NEUEN Bedingungen.

Drei Pruefungen nachtraeglich in einen Zweig einzuziehen ist die Gelegenheit,
eine vierte zu vergessen. Diese Datei sucht genau danach — und prueft die
neue Watchdog-Bedingung `candidate.assigned_agent_id == subtask.callback_agent_id`
in beide Richtungen: schliesst sie den Missbrauch aus UND laesst sie den
legitimen Fall durch?
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine
from tests.test_rex_probe_w5f import _mk_board, _mk_agent, _mk_task


async def _finish_subtask_and_run_watchdog(sub_id):
    """Subtask auf done setzen und den ECHTEN Watchdog laufen lassen."""
    from app.models.task import Task
    from app.routers.agent_task_status import _handle_callback_resume
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = await s.get(Task, sub_id)
        sub.status = "done"
        s.add(sub)
        await s.commit()
        with patch("app.routers.agent_task_status.dispatch_callback_to_parent",
                   new_callable=AsyncMock):
            with patch("app.services.activity.emit_event", new_callable=AsyncMock):
                await _handle_callback_resume(s, sub)


async def _status(task_id):
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.get(Task, task_id)).status


# ── B2, Gegenrichtung: der LEGITIME Fallback muss weiter funktionieren ──

@pytest.mark.asyncio
async def test_probe_r2_legitimate_own_card_fallback_still_resumes(client, fake_redis):
    """Regressions-Wache zur neuen Bedingung: die EIGENE blockierte Karte des
    delegierenden Agenten (blocked_by_task_id vergessen) muss weiterhin
    geweckt werden. Sonst haette der B2-Fix den Watchdog stillgelegt."""
    from app.models.task import Task
    board = await _mk_board("R2Legit")
    boss_id, _ = await _mk_agent(board, "Boss", lead=True)
    own = await _mk_task(board, "Eigene Karte des Delegierenden", "blocked",
                         assigned_agent_id=boss_id, blocked_by=None)
    sub_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=sub_id, board_id=board, title="Subtask", status="in_progress",
                   parent_task_id=own, callback_agent_id=boss_id))
        await s.commit()

    before = await _status(own)
    await _finish_subtask_and_run_watchdog(sub_id)
    after = await _status(own)
    print(f"\n[LEGIT] eigene Karte: {before} -> {after}")
    assert after == "in_progress", (
        "Der legitime Fallback wurde durch die neue Bedingung mit stillgelegt — "
        "eine eigene blockierte Karte wird nicht mehr geweckt."
    )


@pytest.mark.asyncio
async def test_probe_r2_foreign_card_is_not_resumed(client, fake_redis):
    """B2 ueber den ECHTEN Watchdog-Weg: fremde Karte bleibt blocked."""
    from app.models.task import Task
    board = await _mk_board("R2Foreign")
    boss_id, _ = await _mk_agent(board, "Boss", lead=True)
    victim_id, _ = await _mk_agent(board, "Victim", lead=False)
    foreign = await _mk_task(board, "Fremde Karte", "blocked",
                             assigned_agent_id=victim_id, blocked_by=None)
    sub_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=sub_id, board_id=board, title="Subtask", status="in_progress",
                   parent_task_id=foreign, callback_agent_id=boss_id))
        await s.commit()

    await _finish_subtask_and_run_watchdog(sub_id)
    after = await _status(foreign)
    print(f"[FOREIGN-WD] fremde Karte nach Watchdog: {after}")
    assert after == "blocked"


@pytest.mark.asyncio
async def test_probe_r2_reassigned_parent_no_longer_resumes(client, fake_redis):
    """DOKUMENTIEREND (Warnung, kein Blocker): die neue Bedingung vergleicht
    den AKTUELLEN Zuweisungsstand. Wird eine legitim blockierte Elternkarte
    waehrend der Wartezeit umgehaengt (Lead-Reassignment), feuert der
    Fallback nicht mehr — die Karte bleibt blocked stehen."""
    from app.models.task import Task
    board = await _mk_board("R2Reassign")
    boss_id, _ = await _mk_agent(board, "Boss", lead=True)
    other_id, _ = await _mk_agent(board, "Nachfolger", lead=False)
    parent = await _mk_task(board, "Karte, die umgehaengt wurde", "blocked",
                            assigned_agent_id=other_id, blocked_by=None)
    sub_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=sub_id, board_id=board, title="Subtask", status="in_progress",
                   parent_task_id=parent, callback_agent_id=boss_id))
        await s.commit()

    await _finish_subtask_and_run_watchdog(sub_id)
    after = await _status(parent)
    print(f"[REASSIGN] umgehaengte Elternkarte: {after} "
          f"(callback=Boss, assigned=Nachfolger)")
    assert after == "blocked", "unerwartet geweckt"


# ── Suche nach der vierten vergessenen Pruefung ──────────────────────────

@pytest.mark.asyncio
async def test_probe_r2_worker_with_active_task_cannot_point_at_foreign_card(client, fake_redis):
    """B3-Luecke? Ein Worker MIT eigener aktiver Karte nennt per --parent eine
    fremde Karte. Das Ownership-Gate darf auch hier greifen."""
    from app.models.task import Task
    board = await _mk_board("R2W")
    own = await _mk_task(board, "Eigene aktive Karte", "in_progress")
    worker_id, worker_token = await _mk_agent(board, "Worker", lead=False,
                                              current_task_id=own)
    target_id, _ = await _mk_agent(board, "Target", lead=False)
    foreign = await _mk_task(board, "Fremde Karte", "in_progress")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        before = (await s.exec(select(func.count()).select_from(Task))).one()
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(target_id),
                      "parent_task_id": str(foreign)},
                headers={"Authorization": f"Bearer {worker_token}"},
            )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after = (await s.exec(select(func.count()).select_from(Task))).one()
    print(f"\n[W-ACTIVE] Worker mit eigener Karte -> fremder --parent: "
          f"http={resp.status_code} tasks {before}->{after}")
    assert resp.status_code == 409
    assert after == before


@pytest.mark.asyncio
async def test_probe_r2_worker_may_point_at_own_card(client, fake_redis):
    """Gegenprobe zu B3: die EIGENE zugewiesene Karte per --parent zu nennen
    muss weiter gehen (genau der Fall, fuer den --parent gebaut wurde:
    current_task_id ist stale/leer)."""
    from app.models.task import Task
    board = await _mk_board("R2WOwn")
    worker_id, worker_token = await _mk_agent(board, "Worker", lead=False)
    own = await _mk_task(board, "Meine Karte", "in_progress",
                         assigned_agent_id=worker_id)
    target_id, _ = await _mk_agent(board, "Target", lead=False)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(target_id),
                      "parent_task_id": str(own)},
                headers={"Authorization": f"Bearer {worker_token}"},
            )
    print(f"[W-OWN] Worker -> eigene Karte als --parent: http={resp.status_code}")
    assert resp.status_code == 201, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = await s.get(Task, uuid.UUID(resp.json()["subtask_id"]))
        assert sub.parent_task_id == own


@pytest.mark.asyncio
async def test_probe_r2_lead_on_foreign_card_writes_no_comment(client, fake_redis):
    """Der Lead darf per --parent an einer fremden Karte delegieren
    (Lead-Privileg), darf aber nicht in sie hineinschreiben."""
    from app.models.task import TaskComment
    board = await _mk_board("R2LeadC")
    _, lead_token = await _mk_agent(board, "Boss", lead=True)
    victim_id, _ = await _mk_agent(board, "Victim", lead=False)
    target_id, _ = await _mk_agent(board, "Target", lead=False)
    foreign = await _mk_task(board, "Karte von Victim", "in_progress",
                             assigned_agent_id=victim_id)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(target_id),
                      "parent_task_id": str(foreign)},
                headers={"Authorization": f"Bearer {lead_token}"},
            )
    assert resp.status_code == 201, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(TaskComment).where(
            TaskComment.task_id == foreign))).all()
    print(f"[LEAD-FOREIGN] http=201, Kommentare in fremder Karte: {len(rows)}")
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_probe_r2_cross_board_still_blocked_for_non_lead_too(client, fake_redis):
    """B1 wurde im --parent-Zweig ergaenzt. Gegenprobe fuer einen NICHT-Lead:
    auch er darf nicht ueber die Board-Grenze."""
    from app.models.task import Task
    board_a = await _mk_board("R2BA")
    board_b = await _mk_board("R2BB")
    _, worker_token = await _mk_agent(board_a, "WorkerA", lead=False)
    bworker_id, _ = await _mk_agent(board_b, "WorkerB", lead=False)
    parent_b = await _mk_task(board_b, "Karte Board B", "in_progress")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        before = (await s.exec(select(func.count()).select_from(Task))).one()
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board_b}/delegate",
                json={"title": "Sub", "description": "x",
                      "assigned_agent_id": str(bworker_id),
                      "parent_task_id": str(parent_b)},
                headers={"Authorization": f"Bearer {worker_token}"},
            )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after = (await s.exec(select(func.count()).select_from(Task))).one()
    print(f"[XB-WORKER] Nicht-Lead cross-board: http={resp.status_code} "
          f"tasks {before}->{after}")
    assert resp.status_code == 403
    assert after == before
