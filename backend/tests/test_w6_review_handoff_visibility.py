"""W6 — kein stilles Liegenbleiben + Abschlusszeile nennt den echten Pruefer.

(A) `find_reviewer` liefert seit PR #504 bewusst `None`, wenn kein Reviewer-
    Agent auf dem Board existiert — richtig, siehe
    test_find_reviewer_board_lead_fallback.py. Aber alle drei Aufrufer von
    `handle_review_handoff` (agent_task_status.py, tasks.py,
    watchdog/task_monitor.py) verwarfen den Rueckgabewert: die Karte blieb
    unassigned in `review` liegen, ohne Meldung an irgendwen. Der Fix sitzt
    in `handle_review_handoff` selbst (der einen Stelle, die alle drei
    teilen) statt an jedem Call-Site einzeln — die drei Tests unten
    exercisen trotzdem jeden echten Aufrufer-Pfad, damit ein Refactor, der
    einen der drei umbaut, die Luecke nicht unbemerkt wieder aufreisst.

(B) PR #535 Runde 1/2 liess `_notify_lead_on_completion` die Pruefer-
    Identitaet aus dem juengsten `comment_type="review"`-Kommentar herleiten,
    statt den durchgereichten Namen zu nehmen — als Reaktion auf einen
    Vorfall, bei dem die Abschlusszeile "Approved von Hermes" nannte, obwohl
    Rex geprueft hatte. Runde 3 baut das wieder zurueck: Rex selbst hatte in
    Runde 1 nachgewiesen, dass alle drei Aufrufer von
    `_notify_lead_on_completion` (execute_review_decision, die generische
    PATCH-Fallback in agent_task_status.py, system_finalize_task_done) den
    tatsaechlich handelnden Agenten durchreichen, nie eine Zuweisung — die
    Praemisse der Heuristik existiert nicht. Runde 2 flickte stattdessen die
    Heuristik selbst (ein Runden-Anker ueber `review_decided_at`), obwohl
    schon die eigene Untersuchung keinen Fehlerpfad fand. Der Vorfall vom
    2026-09-13, der PR #535 ausgeloest hat, bleibt unerklaert — siehe den
    entsprechenden Absatz im PR. Die Tests unten pruefen jetzt das
    Gegenteil: die Abschlusszeile nennt IMMER den durchgereichten Namen,
    unabhaengig davon, wer zuletzt einen Review-Kommentar geschrieben hat.
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from app.models.thread import Message
from app.utils import utcnow

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────


async def _system_notify_comments(s: AsyncSession, task_id: uuid.UUID) -> list[TaskComment]:
    result = await s.exec(
        select(TaskComment).where(
            TaskComment.task_id == task_id,
            TaskComment.comment_type == "system_notify",
        )
    )
    return list(result.all())


async def _lead_dm_messages(s: AsyncSession, lead: Agent) -> list[Message]:
    from app.services.messaging import ensure_dm_thread
    thread = await ensure_dm_thread(s, lead)
    result = await s.exec(
        select(Message).where(Message.thread_id == thread.id).order_by(Message.seq)
    )
    return list(result.all())


# ── Teil A: sichtbarer Zustand bei allen drei Aufrufern ──────────────────


@pytest.mark.asyncio
async def test_no_reviewer_visible_via_agent_patch_endpoint(make_board, make_agent, auth_client):
    """Aufrufer 1: agent_task_status.py PATCH in_progress -> review."""
    board = await make_board(name="W6-Agent-Patch", slug=f"w6-a-{uuid.uuid4().hex[:6]}")
    lead = await make_agent(name="Lead", board_id=board.id, role="orchestrator", is_board_lead=True)
    dev = await make_agent(name="Dev", board_id=board.id, role="developer")
    # Kein Agent mit role="reviewer" oder "rex"/"review" im Namen.

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        d = await s.get(Agent, dev.id)
        d.agent_token_hash = token_hash
        d.scopes = []
        s.add(d)

        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="W6 Agent-Patch Task",
            status="in_progress", assigned_agent_id=dev.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

        # Evidence-Guard + ADR-023-Pflichtreflexion vor Review.
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="progress", content="Implementiert + getestet.",
        ))
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\nFeature X implementiert.\n\n"
                "## Was hat funktioniert\nTests liefen sauber durch.\n\n"
                "## Was war unklar\nNichts.\n\n"
                "## Lesson für Agent-Memory\nKeine neue Lesson."
            ),
        ))
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
    ):
        resp = await auth_client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        notices = await _system_notify_comments(s, task.id)
        assert any("Kein Reviewer gefunden" in c.content for c in notices), (
            "Kein sichtbarer system_notify-Kommentar bei fehlendem Reviewer "
            "(Aufrufer: agent_task_status.py)"
        )

        lead_fresh = await s.get(Agent, lead.id)
        lead_msgs = await _lead_dm_messages(s, lead_fresh)
        assert any("Kein Reviewer gefunden" in m.body for m in lead_msgs), (
            "Board Lead wurde nicht per DM ueber den fehlenden Reviewer informiert"
        )


@pytest.mark.asyncio
async def test_no_reviewer_visible_via_operator_patch_endpoint(make_board, make_agent, auth_client):
    """Aufrufer 2: tasks.py PATCH (Operator-Pfad) in_progress -> review."""
    board = await make_board(name="W6-Operator-Patch", slug=f"w6-o-{uuid.uuid4().hex[:6]}")
    await make_agent(name="Dev", board_id=board.id, role="developer")
    # Kein Reviewer-Agent auf dem Board.

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="W6 Operator-Patch Task",
            status="in_progress",
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "review"},
        )

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        notices = await _system_notify_comments(s, task.id)
        assert any("Kein Reviewer gefunden" in c.content for c in notices), (
            "Kein sichtbarer system_notify-Kommentar bei fehlendem Reviewer "
            "(Aufrufer: tasks.py Operator-PATCH)"
        )


@pytest.mark.asyncio
async def test_no_reviewer_visible_via_watchdog_phase_completion(make_board, make_agent, session):
    """Aufrufer 3: watchdog/task_monitor.py — Phase-Complete-Fallback ohne Board-Lead."""
    import fakeredis.aioredis
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from app.models.board import Project

    board = await make_board(name="W6-Watchdog", slug=f"w6-w-{uuid.uuid4().hex[:6]}")
    # Bewusst KEIN Board-Lead auf diesem Board -> legacy Rex-Handoff-Zweig.
    # Bewusst KEIN Reviewer-Agent -> handle_review_handoff liefert None.

    project = Project(
        id=uuid.uuid4(), board_id=board.id, name="W6 Projekt",
        status="active", project_type="feature",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)

    parent = Task(
        id=uuid.uuid4(), board_id=board.id, project_id=project.id,
        title="W6 Phase", status="in_progress", sort_order=1,
        parent_task_id=None,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    sub = Task(
        id=uuid.uuid4(), board_id=board.id, project_id=project.id,
        parent_task_id=parent.id, title="W6 Sub", status="done",
    )
    session.add(sub)
    await session.commit()

    fake_server = fakeredis.aioredis.FakeServer()
    fake_redis = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)

    async def _get_redis():
        return fake_redis

    monitor = TaskMonitorMixin()
    with (
        patch("app.services.watchdog.task_monitor.get_redis", _get_redis),
        patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock),
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
    ):
        await monitor._check_phase_completions(session)

    await session.refresh(parent)
    assert parent.status == "review"

    notices = await _system_notify_comments(session, parent.id)
    assert any("Kein Reviewer gefunden" in c.content for c in notices), (
        "Kein sichtbarer system_notify-Kommentar bei fehlendem Reviewer "
        "(Aufrufer: watchdog task_monitor.py, No-Board-Lead-Fallback)"
    )


# ── Teil B: Abschlusszeile nennt IMMER den durchgereichten Namen ─────────


@pytest.mark.asyncio
async def test_completion_line_names_the_approver_after_a_request_changes_round():
    """Konstellation (a) der Definition of Done: Rex lehnt in Runde 1 ab
    (request_changes, ein comment_type="review"-Kommentar), Hermes gibt in
    Runde 2 frei (Freigabe = ein zweiter comment_type="review"-Kommentar,
    author=Hermes). Die Abschlusszeile muss Hermes nennen.

    Reintroduziert man die in Runde 1/2 zurueckgebaute Herleitung aus dem
    Review-Kommentar, besteht dieser Test WEITERHIN — Hermes' eigener
    Kommentar ist ja der juengste. Die Herleitung faellt hier nicht auf,
    weil ihre Praemisse (Karte wird umgehaengt) laut Rex' eigener
    Untersuchung nie eintritt: der durchgereichte Name UND der juengste
    Kommentar-Autor sind im echten Ablauf immer dieselbe Person. Der
    eigentliche Beweis, dass die Herleitung weg ist, liefert die naechste
    Konstellation unten.
    """
    from app.services.task_lifecycle import _notify_lead_on_completion

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    hermes_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="W6-Konstellation-A", slug=f"w6-ka-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=hermes_id, name="Hermes", role="reviewer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Request-Changes dann Freigabe",
            status="done", assigned_agent_id=hermes_id, callback_agent_id=lead_id,
            review_decision="approved", review_decided_at=utcnow(),
        ))
        # Runde 1: Rex lehnt ab.
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=rex_id,
            comment_type="review", content="not ship-ready — Blocker in foo.py:12",
            created_at=utcnow() - dt.timedelta(hours=2),
        ))
        # Runde 2: Hermes gibt frei.
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=hermes_id,
            comment_type="review", content="LGTM, approved.",
        ))
        await s.commit()

        task = await s.get(Task, task_id)
        with patch("app.database.engine", test_engine):
            await _notify_lead_on_completion(s, task, board_id, "Hermes")

        lead = await s.get(Agent, lead_id)
        lead_msgs = await _lead_dm_messages(s, lead)
        assert len(lead_msgs) == 1
        assert "Approved von Hermes" in lead_msgs[0].body
        assert "Approved von Rex" not in lead_msgs[0].body


@pytest.mark.asyncio
async def test_completion_line_names_the_approver_without_a_new_review_comment():
    """Konstellation (b) der Definition of Done: Rex haelt in Runde 1 an
    (`hold` — schreibt einen comment_type="review"-Kommentar). Danach
    schliesst Hermes per PATCH review->done ab (agent_task_status.py's
    generischer Fallback, kein neuer TaskComment). `_notify_lead_on_completion`
    bekommt `reviewer_name="Hermes"` durchgereicht — genau der Agent, der
    die PATCH-Anfrage gestellt hat (agent_task_status.py:2369, `agent.name`).

    Das ist die Konstellation, an der die zurueckgebaute Herleitung
    tatsaechlich versagt hatte: der einzige vorhandene Review-Kommentar
    stammt von Rex (hold), `task.review_decided_at` steht noch auf Rex'
    Hold-Zeitstempel (dieser Test aendert ihn absichtlich nicht — er prueft
    `_notify_lead_on_completion` isoliert, unabhaengig davon, ob der
    aufrufende PATCH-Pfad `review_decided_at` selbst aktualisiert; siehe
    test_patch_review_to_done_after_hold_corrects_stale_review_decision
    fuer die Datenkorrektheit dieses Feldes).

    Sabotage-Probe: den in Runde 1/2 entfernten Lookup-Block (Auswahl des
    juengsten comment_type="review"-Kommentars in
    `_notify_lead_on_completion`, task_lifecycle.py) wieder einfuegen —
    findet Rex' Hold-Kommentar (liegt exakt auf `review_decided_at`, erfuellt
    also auch einen `>=`-Rundenfilter) und meldet faelschlich
    "Approved von Rex". Verifiziert lokal: mit reintroduziertem Block schlägt
    dieser Test fehl (assert "Approved von Hermes" ... ist dann False,
    "Approved von Rex" taucht stattdessen auf).
    """
    from app.services.task_lifecycle import _notify_lead_on_completion

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    hermes_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="W6-Konstellation-B", slug=f"w6-kb-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=hermes_id, name="Hermes", role="reviewer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        hold_at = utcnow()
        s.add(Task(
            id=task_id, board_id=board_id, title="Hold dann PATCH review->done",
            status="done", assigned_agent_id=rex_id, callback_agent_id=lead_id,
            review_decision="hold", review_decided_at=hold_at,
        ))
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=rex_id,
            comment_type="review", content="hold — brauche mehr Kontext.",
            created_at=hold_at,
        ))
        await s.commit()

        task = await s.get(Task, task_id)
        with patch("app.database.engine", test_engine):
            await _notify_lead_on_completion(s, task, board_id, "Hermes", reviewed=True)

        lead = await s.get(Agent, lead_id)
        lead_msgs = await _lead_dm_messages(s, lead)
        assert len(lead_msgs) == 1
        assert "Approved von Hermes" in lead_msgs[0].body, (
            f"Abschlusszeile nennt nicht den tatsaechlichen Freigeber Hermes: {lead_msgs[0].body!r}"
        )
        assert "Approved von Rex" not in lead_msgs[0].body, (
            "Rex (hold, Runde 1) wurde faelschlich als Freigeber genannt"
        )


@pytest.mark.asyncio
async def test_patch_review_to_done_after_hold_corrects_stale_review_decision(
    make_board, make_agent, auth_client,
):
    """Eigenstaendige Datenkorrektheit (Rex' Ein-Zeilen-Fix, unabhaengig von
    der Abschlusszeile): nach `hold` bleibt `task.review_decision="hold"`
    auf der Karte stehen. Reine Freigabe per PATCH review->done (ohne den
    dedizierten POST .../review-Endpunkt) muss diesen Stand trotzdem auf
    "approved" korrigieren — die alte Fallback-Bedingung
    (`task.review_decision is None`) griff nach einem `hold` nicht mehr,
    weil review_decision dann schon "hold" statt None war.

    Sabotage-Probe: die `and task.review_decision is None`-Bedingung in
    agent_task_status.py wieder einfuegen — review_decision bleibt "hold"
    und dieser Test schlaegt fehl.
    """
    from app.auth import generate_agent_token
    from app.services.task_lifecycle import execute_review_decision

    board = await make_board(name="W6-Stale-Decision", slug=f"w6-sd-{uuid.uuid4().hex[:6]}")
    rex = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    hermes = await make_agent(name="Hermes", board_id=board.id, role="reviewer")

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        h = await s.get(Agent, hermes.id)
        h.agent_token_hash = token_hash
        h.scopes = []
        s.add(h)

        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="W6 Hold dann PATCH-Approve",
            status="review", assigned_agent_id=rex.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        task_id = task.id

        rex_agent = await s.get(Agent, rex.id)
        with (
            patch("app.utils.create_tracked_task"),
            patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        ):
            await execute_review_decision(
                s, task, board.id, "hold", "hold — brauche mehr Kontext.",
                actor_agent=rex_agent,
            )
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pre_patch = await s.get(Task, task_id)
        assert pre_patch.review_decision == "hold"
        stale_decided_at = pre_patch.review_decided_at

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
        patch("app.routers.agent_task_status.handle_review_pr_creation", new_callable=AsyncMock),
    ):
        resp = await auth_client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after_patch = await s.get(Task, task_id)
        assert after_patch.status == "done"
        assert after_patch.review_decision == "approved", (
            f"review_decision blieb auf dem Hold-Stand: {after_patch.review_decision!r}"
        )
        assert after_patch.review_decided_at is not None
        assert after_patch.review_decided_at > stale_decided_at, (
            "review_decided_at wurde nicht auf den PATCH-Zeitpunkt aktualisiert"
        )


@pytest.mark.asyncio
async def test_no_reviewer_notice_survives_lead_dm_failure(make_board, make_agent, auth_client):
    """W2 (Rex review 09a860f6): the durable system_notify comment must
    outlive a failing best-effort DM to the Board Lead — `_notify_no_reviewer_
    found` commits the comment BEFORE attempting the DM, wrapped so a raise
    from `post_message` can't roll anything back or bubble up. Correctly
    built already (per Rex's own probe); this locks the behavior in with a
    test so a later refactor that moves the commit after the DM attempt
    doesn't regress silently.

    Sabotage-Probe: moving `await session.commit()` (task_lifecycle.py,
    `_notify_no_reviewer_found`) to AFTER the DM try/except block turns this
    red — 0 system_notify comments once `post_message` raises.
    """
    board = await make_board(name="W2 DM Failure Board", slug=f"w2-dm-fail-{uuid.uuid4().hex[:6]}")
    lead = await make_agent(name="Lead", board_id=board.id, role="orchestrator", is_board_lead=True)
    dev = await make_agent(name="Dev", board_id=board.id, role="developer")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            board_id=board.id, title="Probe DM-Ausfall",
            status="in_progress", assigned_agent_id=dev.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        task_id = task.id

    with patch("app.services.messaging.post_message", side_effect=RuntimeError("DM-Transport down")), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task_id}",
            json={"status": "review"},
        )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "review"
        notices = await _system_notify_comments(s, task_id)
        assert len(notices) == 1, (
            "Der durable system_notify-Kommentar ueberlebt einen DM-Ausfall nicht mehr"
        )
        assert "Kein Reviewer gefunden" in notices[0].content
