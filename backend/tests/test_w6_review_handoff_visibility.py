"""W6 — kein stilles Liegenbleiben + Abschlusszeile nennt den echten Pruefer.

Zwei Luecken rund um den Review-Handoff:

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

(B) Die Abschlusszeile ("**Review:** Approved von X") in
    `_notify_lead_on_completion` bekam den Namen bisher als reine Zeichen-
    kette von weit entfernten Aufrufern durchgereicht. Reviewer und
    task_lifecycle.py:_notify_lead_on_completion selbst zieht die Identitaet
    jetzt aus dem tatsaechlichen Review-Kommentar (comment_type="review"),
    nicht aus dem durchgereichten Namen — robust auch wenn eine Karte
    zwischen Routing und Entscheid umgehaengt wird (geroutet an A, geprueft
    von B).
"""
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


# ── Teil B: Abschlusszeile nennt den echten Pruefer, nicht die Zuweisung ──


@pytest.mark.asyncio
async def test_completion_line_names_actual_reviewer_not_passed_name():
    """Karte geroutet an 'Hermes' (urspruengliche Zuweisung/Name-Parameter),
    tatsaechlich geprueft/freigegeben von 'Rex' (Autor des Review-Kommentars).
    Die Abschlusszeile muss Rex nennen, nicht Hermes.
    """
    from app.services.task_lifecycle import _notify_lead_on_completion

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    hermes_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="W6-Reviewer-Identitaet", slug=f"w6-ri-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=hermes_id, name="Hermes", role="reviewer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Umgehaengte Review-Karte",
            status="done", assigned_agent_id=rex_id, callback_agent_id=lead_id,
        ))
        # Der tatsaechliche Entscheid: Rex hat approved, NICHT Hermes.
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=rex_id,
            comment_type="review", content="LGTM, approved.",
        ))
        await s.commit()

        task = await s.get(Task, task_id)
        # Simuliert einen Aufrufer, der (wie im Vorfall) noch den urspruenglich
        # gerouteten Namen durchreicht.
        with patch("app.database.engine", test_engine):
            await _notify_lead_on_completion(s, task, board_id, "Hermes")

        lead = await s.get(Agent, lead_id)
        lead_msgs = await _lead_dm_messages(s, lead)
        assert len(lead_msgs) == 1
        assert "Approved von Rex" in lead_msgs[0].body, (
            f"Abschlusszeile nennt nicht den tatsaechlichen Pruefer Rex: {lead_msgs[0].body!r}"
        )
        assert "Approved von Hermes" not in lead_msgs[0].body


@pytest.mark.asyncio
async def test_completion_line_falls_back_to_passed_name_without_review_comment():
    """Kein Review-Kommentar vorhanden (z.B. system_finalize_task_done ohne
    Reviewer) -> der durchgereichte Name bleibt die einzige Quelle. Deckt
    ab, dass die neue Herleitung bestehende Faelle (siehe
    test_root_task_callback.py::test_notify_lead_on_completion_...) nicht
    bricht.
    """
    from app.services.task_lifecycle import _notify_lead_on_completion

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="W6-Fallback", slug=f"w6-fb-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=lead_id, name="Lead", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True, scopes=["tasks:read"],
        ))
        s.add(Agent(
            id=worker_id, name="Worker", role="developer",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="System-Finalize ohne Review",
            status="done", assigned_agent_id=worker_id, callback_agent_id=lead_id,
        ))
        await s.commit()

        task = await s.get(Task, task_id)
        with patch("app.database.engine", test_engine):
            await _notify_lead_on_completion(s, task, board_id, "System")

        lead = await s.get(Agent, lead_id)
        lead_msgs = await _lead_dm_messages(s, lead)
        assert len(lead_msgs) == 1
        assert "Approved von System" in lead_msgs[0].body
