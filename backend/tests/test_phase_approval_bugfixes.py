"""Regression-Tests fuer 2 Bugs entdeckt am 2026-04-22:

Bug 1: create_phase_approval_task erzeugte Duplikate
  — Push-Pfad (agent_scoped) hatte Idempotenz, Watchdog-Sweep + die Funktion
    selbst nicht. Zwei parallele Aufrufe erzeugten zwei Phase-Approvals die
    beide Boss bearbeiten musste.

Bug 2: handle_phase_approval_decision setzte Parent IMMER auf review
  — Auf Trust-by-Default-Boards (mc-dev: require_review_before_done=false)
    bleibt review liegen weil kein Reviewer kommt. Parent hängt ewig.
    Fix: Parent bleibt in_progress, Orchestrator schliesst via Hard-Gate ab.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_board_lead_parent(require_review: bool = False):
    """Board + Board Lead + Root-Task im Status in_progress."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    parent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name="PhaseTest", slug=f"pt-{uuid.uuid4().hex[:6]}",
            require_review_before_done=require_review,
        ))
        s.add(Agent(
            id=lead_id, name="Boss", role="orchestrator",
            board_id=board_id, agent_token_hash=generate_agent_token()[1],
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent",
            status="in_progress",
            assigned_agent_id=lead_id, owner_agent_id=lead_id,
        ))
        await s.commit()

    return {"board_id": board_id, "lead_id": lead_id, "parent_id": parent_id}


# ────────────────────────────────────────────────────────────────────
# Bug 1: Idempotenz
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_phase_approval_task_is_idempotent():
    """Zweiter Aufruf fuer denselben Parent gibt den existierenden zurueck, erstellt keinen neuen."""
    from app.services.task_lifecycle import create_phase_approval_task
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        parent = await s.get(Task, data["parent_id"])

        # Erster Call — erzeugt einen
        a1 = await create_phase_approval_task(s, parent, lead)
        assert a1 is not None
        assert a1.delegation_type == "phase_approval"

        # Zweiter Call — MUSS denselben zurueckgeben, kein Duplikat
        a2 = await create_phase_approval_task(s, parent, lead)
        assert a2 is not None
        assert a2.id == a1.id, "Idempotenz verletzt — zweiter Approval-Task erstellt"

        # Genau 1 Phase-Approval-Task in DB
        approvals = (await s.exec(
            select(Task).where(
                Task.parent_task_id == parent.id,
                Task.delegation_type == "phase_approval",
            )
        )).all()
        assert len(approvals) == 1


# ────────────────────────────────────────────────────────────────────
# Bug 2: Trust-by-Default Board
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_approved_trust_by_default_keeps_parent_in_progress():
    """Auf Trust-by-Default-Board (require_review_before_done=false) bleibt
    Parent nach phase_approved auf in_progress — nicht auf review.
    """
    from app.services.task_lifecycle import (
        create_phase_approval_task, handle_phase_approval_decision,
    )
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        parent = await s.get(Task, data["parent_id"])
        approval = await create_phase_approval_task(s, parent, lead)

        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
            result = await handle_phase_approval_decision(
                s, approval, lead, "phase_approved", "looks good",
            )

        assert result["decision"] == "approved"
        assert result["parent_promoted"] is False, \
            "Parent darf auf Trust-by-Default NICHT auf review promoted werden"

        await s.refresh(parent)
        assert parent.status == "in_progress", \
            f"Parent muss in_progress bleiben, ist aber '{parent.status}'"


@pytest.mark.asyncio
async def test_phase_approved_with_required_review_promotes_to_review():
    """Auf Boards mit require_review_before_done=true: alter Pfad — Parent geht auf review."""
    from app.services.task_lifecycle import (
        create_phase_approval_task, handle_phase_approval_decision,
    )
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent(require_review=True)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        parent = await s.get(Task, data["parent_id"])
        approval = await create_phase_approval_task(s, parent, lead)

        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
            result = await handle_phase_approval_decision(
                s, approval, lead, "phase_approved", "review me",
            )

        assert result["parent_promoted"] is True
        await s.refresh(parent)
        assert parent.status == "review"


# ────────────────────────────────────────────────────────────────────
# Bug 3: Orchestrator-Nudge nach phase_approved (entdeckt 2026-04-22)
#
# Folgebug zu Bug 2 Fix: Parent bleibt in_progress, aber Orchestrator
# uebersieht das (Approval-Task ist done, in seiner Sicht erscheint
# nichts mehr). Fix: aktiver Re-Dispatch-Nudge + Watchdog-Safety-Net
# ────────────────────────────────────────────────────────────────────




















@pytest.mark.asyncio
async def test_send_orchestrator_close_nudge_host_runtime_posts_system_comment():
    """Host-Runtime Boss (kein gateway_agent_id, agent_runtime='host'): Nudge geht
    NICHT via rpc.chat_send sondern als TaskComment(comment_type='system') auf
    den Parent. Der wird ueber /agent/me/poll → poll.sh → tmux paste-buffer in
    Boss's Claude-Session zugestellt.
    """
    from app.services.task_lifecycle import send_orchestrator_close_nudge, ORCH_CLOSE_REMINDER_MARKER
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

        # Phase 29 / Gateway-Sunset: kein rpc-Modul mehr im task_lifecycle —
        # die Funktion postet ausschliesslich einen TaskComment + Poll-Pfad.
        sent = await send_orchestrator_close_nudge(
            s, parent, lead, reason="phase_approved",
        )

        assert sent is True, "Host-Runtime-Pfad muss einen Nudge zustellen"

        # System-Comment auf Parent erstellt mit Marker + Hard-Gate-Sequenz
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == parent.id)
        )).all()
        assert len(comments) == 1
        c = comments[0]
        assert c.comment_type == "system"
        assert c.author_type == "system"
        assert c.author_agent_id is None  # kein Echo-Loop bei _is_deliverable_for
        assert ORCH_CLOSE_REMINDER_MARKER in c.content
        assert "mc telegram" in c.content
        assert "mc done" in c.content
        assert str(parent.id) in c.content


@pytest.mark.asyncio
async def test_send_orchestrator_close_nudge_host_runtime_idempotent_within_window():
    """Zweiter Aufruf innerhalb 10 Min postet keinen zweiten System-Comment
    (verhindert Spam in Boss's tmux-Session bei Watchdog-Re-Fires)."""
    from app.services.task_lifecycle import send_orchestrator_close_nudge
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

        sent1 = await send_orchestrator_close_nudge(s, parent, lead, reason="phase_approved")
        sent2 = await send_orchestrator_close_nudge(s, parent, lead, reason="stuck_safety_net")

        assert sent1 is True
        assert sent2 is False, "Zweiter Aufruf im Dedup-Window darf NICHT erneut posten"

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == parent.id)
        )).all()
        assert len(comments) == 1, f"Erwartet 1 Comment, gefunden {len(comments)}"


@pytest.mark.asyncio
async def test_send_orchestrator_close_nudge_host_runtime_discord_skips_telegram_hint():
    """Host-Runtime + report_back_channel='discord': Reminder kommt trotzdem,
    aber OHNE `mc telegram`-Hinweis (Hard-Gate gilt nur fuer telegram-routed)."""
    from app.services.task_lifecycle import send_orchestrator_close_nudge
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        parent.report_back_channel = "discord"
        s.add(parent)
        await s.commit()

        sent = await send_orchestrator_close_nudge(s, parent, lead, reason="phase_approved")

        assert sent is True
        c = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == parent.id)
        )).first()
        assert c is not None
        assert "mc done" in c.content
        assert "mc telegram" not in c.content, \
            "Discord-routed Tasks duerfen den telegram-Hinweis nicht enthalten"










@pytest.mark.asyncio
async def test_check_phase_completions_skips_recreation_when_done_approval_exists():
    """Bug 4 (Live-Test 2026-04-22): Wenn bereits ein DONE phase_approval existiert
    und das Child-Set seit Approval unveraendert ist, darf _check_phase_completions
    KEINEN neuen Approval-Task erstellen. stuck_orchestrator_close kuemmert sich.
    """
    from datetime import timedelta
    from app.services.watchdog.core import WatchdogService
    from app.models.agent import Agent
    from app.models.task import Task
    from app.utils import utcnow, strip_tz
    from unittest.mock import MagicMock

    data = await _setup_board_lead_parent(require_review=False)

    # Realistic stuck scenario: Subtask wurde BEFORE approval modifiziert → Phase
    # ist stabil approved, keine neue Phase-Arbeit danach.
    sub_done_at = strip_tz(utcnow() - timedelta(hours=1))
    approval_done_at = strip_tz(utcnow() - timedelta(minutes=50))  # NACH subtask

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            board_id=data["board_id"], title="Subtask 1", status="done",
            parent_task_id=data["parent_id"], assigned_agent_id=data["lead_id"],
            updated_at=sub_done_at,
        ))
        s.add(Task(
            board_id=data["board_id"], title="Old Approval (done)", status="done",
            parent_task_id=data["parent_id"], delegation_type="phase_approval",
            assigned_agent_id=data["lead_id"],
            completed_at=approval_done_at, updated_at=approval_done_at,
        ))
        await s.commit()

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    with patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.watchdog.task_monitor.get_redis", new_callable=AsyncMock) as mock_get_redis, \
         patch("app.services.auto_memory.record_phase_completion", new_callable=MagicMock), \
         patch("app.services.watchdog.core._create_background_task"):
        mock_get_redis.return_value = mock_redis

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            monitor = WatchdogService()
            await monitor._check_phase_completions(s)

    # Verify: GENAU 1 Phase-Approval (der alte done) — kein neuer
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Task)
            .where(Task.parent_task_id == data["parent_id"])
            .where(Task.delegation_type == "phase_approval")
        )).all()
        assert len(approvals) == 1, \
            f"Erwarte 1 Approval (alter done), bekam {len(approvals)}: {[(a.id, a.status) for a in approvals]}"
        assert approvals[0].status == "done"

    # Dedup-Key wurde gesetzt damit naechster Watchdog-Tick nicht wieder versucht
    mock_redis.set.assert_called()


@pytest.mark.asyncio
async def test_check_phase_completions_creates_new_approval_after_rewrite():
    """Bug 5 (Rewrite-Edge-Case, identifiziert 2026-04-22):
    Boss macht phase_rewrite_request → Approval1=done, Subtasks zurueck auf inbox →
    re-worked → wieder done. Das aktuelle Child-Set ist NEU seit Approval1 (subtask
    updated_at > approval.completed_at). _check_phase_completions darf nicht faelsch-
    licherweise skippen — neuer Approval muss entstehen (via create_phase_approval_task
    Push-Pfad ODER Watchdog-Durchlauf).
    """
    from datetime import timedelta
    from unittest.mock import MagicMock
    from app.services.watchdog.core import WatchdogService
    from app.models.task import Task
    from app.utils import utcnow, strip_tz

    data = await _setup_board_lead_parent(require_review=False)

    # Approval1 wurde done VOR aktueller Subtask-Bearbeitung (Rewrite-Szenario)
    approval_done_at = strip_tz(utcnow() - timedelta(hours=2))
    # Subtasks wurden nach Rewrite aktualisiert → neuer als approval
    subtask_recent = strip_tz(utcnow() - timedelta(minutes=5))

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            board_id=data["board_id"], title="Subtask 1 (re-worked)", status="done",
            parent_task_id=data["parent_id"], assigned_agent_id=data["lead_id"],
            updated_at=subtask_recent,
        ))
        s.add(Task(
            board_id=data["board_id"], title="Approval1 (done via rewrite)", status="done",
            parent_task_id=data["parent_id"], delegation_type="phase_approval",
            assigned_agent_id=data["lead_id"],
            completed_at=approval_done_at, updated_at=approval_done_at,
        ))
        await s.commit()

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    with patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.watchdog.task_monitor.get_redis", new_callable=AsyncMock) as mock_get_redis, \
         patch("app.services.auto_memory.record_phase_completion", new_callable=MagicMock), \
         patch("app.services.watchdog.core._create_background_task"):
        mock_get_redis.return_value = mock_redis

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            monitor = WatchdogService()
            await monitor._check_phase_completions(s)

    # Verify: NEUER Approval wurde erstellt (2 insgesamt jetzt — alter done + neuer inbox)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Task)
            .where(Task.parent_task_id == data["parent_id"])
            .where(Task.delegation_type == "phase_approval")
            .order_by(Task.created_at)
        )).all()
        assert len(approvals) == 2, \
            f"Erwarte 2 Approvals (alter done + neuer inbox), bekam {len(approvals)}"
        assert approvals[0].status == "done"
        assert approvals[1].status == "inbox", \
            f"Neuer Approval sollte in inbox sein, ist {approvals[1].status}"


@pytest.mark.asyncio
async def test_orchestrator_close_nudge_escalates_to_mark_after_threshold():
    """Eskalation-Tests: nach N ergebnislosen Close-Nudges wird der Operator via Reports-Bot
    benachrichtigt.
    """
    from app.services.task_lifecycle import (
        send_orchestrator_close_nudge,
        ORCH_CLOSE_ESCALATION_THRESHOLD,
    )
    from app.models.agent import Agent
    from app.models.task import Task

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

    # Mock redis: counter incrementiert sich, get gibt None (nicht eskaliert), set OK.
    # Wir nutzen echtes fakeredis-mock via dict-Semantik.
    redis_state = {}

    async def _fake_get(key):
        return redis_state.get(key)

    async def _fake_set(key, value, ex=None):
        redis_state[key] = value
        return True

    async def _fake_incr(key):
        current = int(redis_state.get(key, 0))
        redis_state[key] = str(current + 1)
        return current + 1

    async def _fake_expire(key, ttl):
        return True

    mock_redis = AsyncMock()
    mock_redis.get.side_effect = _fake_get
    mock_redis.set.side_effect = _fake_set
    mock_redis.incr.side_effect = _fake_incr
    mock_redis.expire.side_effect = _fake_expire

    mock_reports = AsyncMock()
    mock_reports.configured = True
    mock_reports.send.return_value = {"ok": True, "result": {"message_id": 1}}

    async def _mock_get_redis():
        return mock_redis

    with patch("app.services.task_lifecycle.get_redis", side_effect=_mock_get_redis), \
         patch("app.redis_client.get_redis", side_effect=_mock_get_redis), \
         patch("app.services.task_lifecycle.telegram_reports", mock_reports):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            # Threshold-1 Nudges: noch keine Eskalation
            parent = await s.get(Task, data["parent_id"])
            lead = await s.get(Agent, data["lead_id"])

            for i in range(ORCH_CLOSE_ESCALATION_THRESHOLD - 1):
                # Unique dedup-window pro call umgehen durch expliziten Reset der Comment-Suche —
                # hier reichen wir einfach ein sehr kleines dedup_window und neuer reason
                # alternativ: loesche Comments zwischen den Posts damit Idempotenz nicht greift
                from app.models.task import TaskComment
                (await s.exec(
                    select(TaskComment).where(TaskComment.task_id == parent.id)
                )).all()
                # Clear reminders: setze alle existing system-comments auf sehr alt (vor dedup)
                all_c = (await s.exec(select(TaskComment).where(TaskComment.task_id == parent.id))).all()
                for c in all_c:
                    from datetime import timedelta
                    from app.utils import utcnow, strip_tz
                    c.created_at = strip_tz(utcnow() - timedelta(hours=1))
                    s.add(c)
                await s.commit()

                await send_orchestrator_close_nudge(s, parent, lead, reason="phase_approved")

            # So far: Reports-Bot NICHT aufgerufen
            assert mock_reports.send.await_count == 0, \
                f"Unter Threshold, kein Operator-Ping erwartet; got {mock_reports.send.await_count}"

            # Letzter Nudge -> threshold erreicht -> Operator wird benachrichtigt
            all_c = (await s.exec(select(TaskComment).where(TaskComment.task_id == parent.id))).all()
            for c in all_c:
                from datetime import timedelta
                from app.utils import utcnow, strip_tz
                c.created_at = strip_tz(utcnow() - timedelta(hours=1))
                s.add(c)
            await s.commit()

            await send_orchestrator_close_nudge(s, parent, lead, reason="stuck_safety_net")

            assert mock_reports.send.await_count == 1, \
                f"Erwartet genau 1 Eskalation-Send, got {mock_reports.send.await_count}"
            sent_text = mock_reports.send.await_args.args[0]
            assert "Eskalation" in sent_text or "eskalation" in sent_text.lower()
            assert str(parent.id) in sent_text


@pytest.mark.asyncio
async def test_orchestrator_close_escalation_is_idempotent():
    """Zweite Welle von Nudges nach Eskalation darf den Operator nicht nochmal spammen
    (bis Redis-TTL ablaeuft)."""
    from app.services.task_lifecycle import (
        send_orchestrator_close_nudge,
        ORCH_CLOSE_ESCALATION_THRESHOLD,
    )
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from datetime import timedelta
    from app.utils import utcnow, strip_tz

    data = await _setup_board_lead_parent(require_review=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead = await s.get(Agent, data["lead_id"])
        lead.agent_runtime = "host"
        s.add(lead)
        parent = await s.get(Task, data["parent_id"])
        parent.report_back_required = True
        s.add(parent)
        await s.commit()

    redis_state = {}

    async def _fake_get(key):
        return redis_state.get(key)

    async def _fake_set(key, value, ex=None):
        redis_state[key] = value
        return True

    async def _fake_incr(key):
        current = int(redis_state.get(key, 0))
        redis_state[key] = str(current + 1)
        return current + 1

    async def _fake_expire(key, ttl):
        return True

    mock_redis = AsyncMock()
    mock_redis.get.side_effect = _fake_get
    mock_redis.set.side_effect = _fake_set
    mock_redis.incr.side_effect = _fake_incr
    mock_redis.expire.side_effect = _fake_expire

    mock_reports = AsyncMock()
    mock_reports.configured = True
    mock_reports.send.return_value = {"ok": True, "result": {"message_id": 1}}

    async def _mock_get_redis():
        return mock_redis

    with patch("app.services.task_lifecycle.get_redis", side_effect=_mock_get_redis), \
         patch("app.redis_client.get_redis", side_effect=_mock_get_redis), \
         patch("app.services.task_lifecycle.telegram_reports", mock_reports):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            parent = await s.get(Task, data["parent_id"])
            lead = await s.get(Agent, data["lead_id"])

            # 5 Nudges — weit ueber dem threshold
            for i in range(5):
                all_c = (await s.exec(select(TaskComment).where(TaskComment.task_id == parent.id))).all()
                for c in all_c:
                    c.created_at = strip_tz(utcnow() - timedelta(hours=1))
                    s.add(c)
                await s.commit()
                await send_orchestrator_close_nudge(s, parent, lead, reason="stuck_safety_net")

            # Nur EINE Eskalation, egal wie oft genudged wurde
            assert mock_reports.send.await_count == 1, \
                f"Erwartet genau 1 Eskalation (idempotent), got {mock_reports.send.await_count}"


# Import am Ende damit select-Import fuer den ersten Test verfuegbar ist
from sqlmodel import select  # noqa: E402
