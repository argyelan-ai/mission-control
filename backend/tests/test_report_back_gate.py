"""Tests fuer Report-Back Hard-Gate + Auto-Draft.

Szenario-Matrix (Bezug: feat/report-back-hard-gate):

| # | Szenario                               | Verhalten                              |
|---|----------------------------------------|----------------------------------------|
| 1 | Happy-path: telegram → done            | OK                                     |
| 2 | Ohne report_back_required              | done ohne telegram: OK                 |
| 3 | Gate greift (required, kein telegram)  | done → 422                             |
| 4 | Retry nach 422                         | 422 → telegram → done: OK              |
| 5 | Idempotenz mehrfach telegram           | Flag bleibt True                       |
| 6 | Failure → Auto-Draft                   | failed ohne telegram: Draft gesendet   |
| 7 | Failure nach telegram (schon sent)     | kein zweiter Draft                     |
| 9 | Review-Pfad mit Report                 | Dev: telegram+review, Rex: done OK     |
|10 | Review-Pfad ohne Report                | Rex: done → 422                        |
|11 | Bot unconfigured bei failed            | Draft skipped, failed trotzdem durch   |
|12 | Subtask (parent_task_id gesetzt)       | Kein Gate — Subtask kein Reports-Case  |
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ────────────────────────────────────────────────────────────────────
# Fixtures / Helpers
# ────────────────────────────────────────────────────────────────────

async def _setup_root_task_with_report_required(
    *,
    report_back_required: bool = True,
    task_status: str = "in_progress",
    parent_id: uuid.UUID | None = None,
):
    """Erstellt Board + Developer + Root-Task (optional als Subtask) + gibt Token zurueck."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="RB Board", slug=f"rb-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Reporter", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=task_id,
            emoji="🔍",
        ))
        s.add(Task(
            id=task_id,
            board_id=board_id,
            title="Research Task",
            status=task_status,
            assigned_agent_id=agent_id,
            owner_agent_id=agent_id,
            report_back_required=report_back_required,
            parent_task_id=parent_id,
        ))
        await s.commit()

    return {
        "board_id": board_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "token": token_raw,
    }


def _mock_configured_reports_service(send_return={"ok": True, "result": {"message_id": 1}}):
    """Hilft beim Patchen — gibt einen Mock zurueck der bei `configured` True ist
    und `send` mit dem gegebenen Return-Wert beantwortet."""
    mock = MagicMock()
    mock.configured = True
    mock.send = AsyncMock(return_value=send_return)
    return mock


async def _add_reflection_comment(task_id: uuid.UUID, agent_id: uuid.UUID):
    """Posted eine Pflicht-Reflexion damit der Closing-Transition-Gate passiert."""
    from app.models.task import TaskComment
    content = (
        "## Was wurde gemacht\nTask fertig.\n\n"
        "## Was hat funktioniert\nAlles gut.\n\n"
        "## Was war unklar\nNichts.\n\n"
        "## Lesson fuer Agent-Memory\nKeine."
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=agent_id,
            content=content,
            comment_type="reflection",
        ))
        await s.commit()


# ────────────────────────────────────────────────────────────────────
# Szenario 1: Happy-Path
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_telegram_then_done(client, fake_redis):
    """1. telegram senden → Flag gesetzt → done OK."""
    data = await _setup_root_task_with_report_required()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r1 = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🔍 Reporter · Done ✅"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r1.status_code == 200

    # Flag muss in DB gesetzt sein
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is True

    # Reflection noetig vor done
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    # done geht jetzt durch
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r2 = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r2.status_code == 200, f"Expected 200, got {r2.status_code}: {r2.text}"
    assert r2.json()["status"] == "done"


# ────────────────────────────────────────────────────────────────────
# Szenario 2: Ohne report_back_required
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_without_report_back_required_passes(client, fake_redis):
    """2. kein report_back_required → done ohne telegram ist OK, kein Gate."""
    data = await _setup_root_task_with_report_required(report_back_required=False)
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────────────────────────
# Szenario 3: Gate greift
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_blocks_done_when_required_and_not_sent(client, fake_redis):
    """3. done mit Reflection aber ohne telegram → 422 Gate-Message."""
    data = await _setup_root_task_with_report_required()
    # Reflection ist da, aber Flag nicht gesetzt — Gate muss greifen
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r.status_code == 422, r.text
    assert "mc telegram" in r.json()["detail"]


# ────────────────────────────────────────────────────────────────────
# Szenario 4: Retry nach 422
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_after_422_works(client, fake_redis):
    """4. done (→422) → telegram → done → OK."""
    data = await _setup_root_task_with_report_required()
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r1 = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r1.status_code == 422

        mock_reports = _mock_configured_reports_service()
        with patch("app.services.telegram_reports.telegram_reports", mock_reports):
            r2 = await client.post(
                "/api/v1/agent/telegram/send",
                json={"text": "report"},
                headers={"Authorization": f"Bearer {data['token']}"},
            )
            assert r2.status_code == 200

        r3 = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r3.status_code == 200, r3.text


# ────────────────────────────────────────────────────────────────────
# Szenario 5: Idempotenz
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_telegram_calls_idempotent(client, fake_redis):
    """5. telegram × 3 → Flag bleibt true, kein Side-Effect."""
    data = await _setup_root_task_with_report_required()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        for _ in range(3):
            r = await client.post(
                "/api/v1/agent/telegram/send",
                json={"text": "chunk"},
                headers={"Authorization": f"Bearer {data['token']}"},
            )
            assert r.status_code == 200

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is True

    # Service wurde 3x aufgerufen (kein early-return bei bereits gesetztem Flag)
    assert mock_reports.send.await_count == 3


# ────────────────────────────────────────────────────────────────────
# Szenario 6: Failure → Auto-Draft
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_triggers_auto_draft(client, fake_redis):
    """6. failed ohne telegram → Auto-Draft gesendet, Flag gesetzt, failed durch."""
    data = await _setup_root_task_with_report_required()

    # Reflection-Comment hinzufuegen damit Draft Inhalt hat
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=data["task_id"],
            author_type="agent",
            author_agent_id=data["agent_id"],
            content="**Was ich versucht habe:** Scraping blockiert durch Cloudflare.",
            comment_type="reflection",
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports), \
         patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={
                "status": "failed",
                "blocker_type": "technical_problem",
                "blocker_question": "Wie scrapen wenn Cloudflare blockt?",
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200
    mock_reports.send.assert_awaited()  # Auto-Draft wurde gesendet

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is True
        assert t.status == "failed"


# ────────────────────────────────────────────────────────────────────
# Szenario 7: Failure mit bereits gesendetem Telegram
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_after_telegram_skips_auto_draft(client, fake_redis):
    """7. telegram gesendet → failed → kein zweiter Draft (Flag schon true)."""
    data = await _setup_root_task_with_report_required()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "manueller report"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    # Reset call count — nach dem telegram-Send
    mock_reports.send.reset_mock()

    with patch("app.services.telegram_reports.telegram_reports", mock_reports), \
         patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={
                "status": "failed",
                "blocker_type": "other",
                "blocker_question": "n/a",
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200
    # Kein zweiter send-Call — Flag war schon true, Auto-Draft-Block uebersprungen
    mock_reports.send.assert_not_awaited()


# ────────────────────────────────────────────────────────────────────
# Szenario 11: Bot unconfigured bei failed
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_with_unconfigured_bot_still_transitions(client, fake_redis):
    """11. failed + Bot nicht konfiguriert → Auto-Draft skipped, failed trotzdem durch, Flag nicht gesetzt."""
    data = await _setup_root_task_with_report_required()

    unconfigured_mock = MagicMock()
    unconfigured_mock.configured = False
    unconfigured_mock.send = AsyncMock()  # wird nicht aufgerufen

    with patch("app.services.telegram_reports.telegram_reports", unconfigured_mock), \
         patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={
                "status": "failed",
                "blocker_type": "other",
                "blocker_question": "test",
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200  # Failed trotzdem durch — Task darf nicht haengen bleiben
    unconfigured_mock.send.assert_not_awaited()

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.status == "failed"
        assert t.report_sent_to_telegram is False  # Flag NICHT gesetzt


# ────────────────────────────────────────────────────────────────────
# Szenario 12: Subtask (parent_task_id gesetzt)
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subtask_done_without_telegram_passes(client, fake_redis):
    """12. Subtask (parent_task_id gesetzt) → Gate greift NICHT, done OK ohne telegram."""
    # Erstmal Parent erstellen
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    parent_id = uuid.uuid4()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Sub Board", slug=f"sub-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="SubWorker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=subtask_id,
        ))
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent",
            status="in_progress", report_back_required=True,
        ))
        s.add(Task(
            id=subtask_id, board_id=board_id, title="Subtask",
            status="in_progress", assigned_agent_id=agent_id,
            parent_task_id=parent_id,
            report_back_required=True,  # selbst wenn true — Subtask hat kein Gate
        ))
        await s.commit()

    await _add_reflection_comment(subtask_id, agent_id)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{subtask_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {token_raw}"},
        )

    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────────────────────────
# Szenario 10: Review-Pfad ohne Report → Gate greift
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_path_without_report_blocks_rex_done(client, fake_redis):
    """10. Dev → review (kein telegram), Rex → done → 422.

    Gate ist status-basiert, nicht agent-basiert: egal wer done setzt,
    wenn Flag nicht gesetzt ist, blockiert das Backend.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="ReviewBoard", slug=f"rv-{uuid.uuid4().hex[:6]}",
                    require_review_before_done=True))
        dev_token, dev_hash = generate_agent_token()
        rex_token, rex_hash = generate_agent_token()
        s.add(Agent(
            id=dev_id, name="Cody", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
            current_task_id=task_id,
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=rex_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Reviewed Feature",
            status="review",  # Dev hat schon auf review gesetzt ohne telegram
            assigned_agent_id=rex_id, owner_agent_id=dev_id,
            report_back_required=True,
        ))
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        # Rex versucht done zu setzen
        r = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {rex_token}"},
        )
    assert r.status_code == 422
    assert "mc telegram" in r.json()["detail"]


# ────────────────────────────────────────────────────────────────────
# Direct-Unit-Tests fuer render_and_send_failure_draft
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_draft_includes_reflection_and_escapes_html():
    """Der Auto-Draft enthaelt Reflection + HTML-Escape bei user content."""
    from app.services.report_auto_draft import _render_draft

    text = _render_draft(
        agent_name="Researcher <weird>",
        agent_emoji="🔍",
        task_title="Title with <tags> & symbols",
        task_id="abc12345-6789-0000-0000-000000000000",
        reflection="Problem war <script>alert()</script> im Code.",
        recent_comments=["Comment 1 & stuff", "Comment 2"],
    )

    # HTML-Escape: & wird zu &amp;, < wird zu &lt;
    assert "&lt;weird&gt;" in text
    assert "&lt;tags&gt;" in text
    assert "&amp; symbols" in text
    assert "&lt;script&gt;" in text  # Reflection-Content escaped

    # Header-Struktur
    assert text.startswith("🔍 <b>Researcher &lt;weird&gt;</b>")
    assert "❌" in text
    assert "<b>Reflexion des Agenten</b>" in text
    assert "<b>Letzte Kommentare</b>" in text
    assert "abc12345" in text  # Kurz-ID


@pytest.mark.asyncio
async def test_worker_without_current_task_id_can_set_flag_via_task_id(client, fake_redis):
    """C1-Regression: Subagent-Dispatch-Worker (kein current_task_id) kann das Flag
    setzen indem er task_id im /telegram/send Body mitschickt.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="NoCurrentTask", slug=f"nct-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Subworker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=None,  # Subagent-Dispatch hat keinen Agent-Level-Tracker
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Worker-Task", status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
            report_back_required=True,
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "worker report", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {token_raw}"},
        )

    assert r.status_code == 200, r.text

    # Flag muss gesetzt sein auch ohne current_task_id
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t.report_sent_to_telegram is True


@pytest.mark.asyncio
async def test_task_id_ownership_check_rejects_foreign_agent(client, fake_redis):
    """Ownership-Check: Agent darf nur Flag auf seinen eigenen Tasks setzen."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    foreign_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Own", slug=f"own-{uuid.uuid4().hex[:6]}"))
        _, owner_hash = generate_agent_token()
        foreign_token, foreign_hash = generate_agent_token()
        s.add(Agent(
            id=owner_id, name="Owner", role="developer",
            board_id=board_id, agent_token_hash=owner_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
        ))
        s.add(Agent(
            id=foreign_id, name="Foreign", role="developer",
            board_id=board_id, agent_token_hash=foreign_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="OwnedTask", status="in_progress",
            assigned_agent_id=owner_id, owner_agent_id=owner_id,
            report_back_required=True,
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "hijack attempt", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {foreign_token}"},
        )

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_review_approve_blocked_when_report_not_sent(client, fake_redis):
    """C2-Regression: POST /review mit decision=approve respektiert den Gate."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="ReviewGate", slug=f"rvg-{uuid.uuid4().hex[:6]}"))
        _, dev_hash = generate_agent_token()
        rex_token, rex_hash = generate_agent_token()
        s.add(Agent(
            id=dev_id, name="DevGuy", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=rex_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Feature",
            status="review",
            assigned_agent_id=rex_id, owner_agent_id=dev_id,
            report_back_required=True,
            # Kein Report gesendet — Flag bleibt False
        ))
        await s.commit()

    # /review approve sollte 422 werfen
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/review",
            json={"decision": "approve", "comment": "Looks good, approved."},
            headers={"Authorization": f"Bearer {rex_token}"},
        )

    assert r.status_code == 422, r.text
    assert "mc telegram" in r.json()["detail"]


@pytest.mark.asyncio
async def test_discord_channel_task_bypasses_gate(client, fake_redis):
    """C3-Regression: Tasks mit report_back_channel='discord' werden nicht blockiert."""
    data = await _setup_root_task_with_report_required()
    # Channel auf discord umstellen
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        t.report_back_channel = "discord"
        s.add(t)
        await s.commit()

    await _add_reflection_comment(data["task_id"], data["agent_id"])

    # done ohne mc telegram MUSS durchgehen — Discord-Delivery ist anderer Kanal
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_atomic_claim_update_prevents_double_send():
    """C4-Regression: Atomic UPDATE-Claim garantiert exakt einen rowcount=1
    bei simuliertem Race. Direkter DB-Level-Test statt HTTP (SQLite-Limits
    bei concurrent HTTP).
    """
    from sqlalchemy import update as _sa_update
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Race", slug=f"race-{uuid.uuid4().hex[:6]}"))
        _, h = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="A", role="developer",
            board_id=board_id, agent_token_hash=h,
            scopes=["chat:write"],             provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Race Task",
            status="in_progress", assigned_agent_id=agent_id,
            report_back_required=True,
            report_sent_to_telegram=False,
        ))
        await s.commit()

        # Erster Claim — sollte rowcount=1 ergeben
        r1 = await s.exec(
            _sa_update(Task)
            .where(Task.id == task_id, Task.report_sent_to_telegram == False)  # noqa: E712
            .values(report_sent_to_telegram=True)
        )
        await s.commit()
        assert r1.rowcount == 1, "Erster Claim muss erfolgreich sein"

        # Zweiter Claim (simuliert parallelen Request) — sollte rowcount=0 ergeben
        r2 = await s.exec(
            _sa_update(Task)
            .where(Task.id == task_id, Task.report_sent_to_telegram == False)  # noqa: E712
            .values(report_sent_to_telegram=True)
        )
        await s.commit()
        assert r2.rowcount == 0, (
            "Zweiter Claim muss rowcount=0 ergeben (Flag schon gesetzt) — "
            "sonst ist der Atomic-Claim-Schutz kaputt und wir senden doppelt"
        )


@pytest.mark.asyncio
async def test_telegram_send_http_exception_rolls_back_claim(client, fake_redis):
    """B1-Regression: httpx/network Exception in telegram_reports.send() → Flag
    wird zurueckgerollt, Agent kann retryen (sonst permanenter Lock).
    """
    data = await _setup_root_task_with_report_required()

    # Mock: configured=True aber send() wirft httpx.ConnectTimeout
    import httpx
    failing_mock = MagicMock()
    failing_mock.configured = True
    failing_mock.send = AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    with patch("app.services.telegram_reports.telegram_reports", failing_mock):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "test", "task_id": str(data["task_id"])},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 503
    assert "Retry" in r.json()["detail"] or "fehlgeschlagen" in r.json()["detail"]

    # Kritisch: Flag NICHT gesetzt, Agent kann retryen
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is False, (
            "Flag muss bei Exception zurueckgerollt werden — sonst kann Agent nie retryen"
        )


@pytest.mark.asyncio
async def test_board_lead_cannot_set_flag_on_foreign_board_task(client, fake_redis):
    """H1-Regression: Board Lead von Board A darf Flag nicht auf Task von Board B setzen."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_a = uuid.uuid4()
    board_b = uuid.uuid4()
    lead_id = uuid.uuid4()
    foreign_task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_a, name="A", slug=f"a-{uuid.uuid4().hex[:6]}"))
        s.add(Board(id=board_b, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        lead_token, lead_hash = generate_agent_token()
        s.add(Agent(
            id=lead_id, name="LeadA", role="lead",
            board_id=board_a, agent_token_hash=lead_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=foreign_task_id, board_id=board_b, title="Board B Task",
            status="in_progress",
            # Nicht assigned an lead, nicht owner — Lead gehört zu anderem Board
            report_back_required=True,
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "cross-board leak attempt", "task_id": str(foreign_task_id)},
            headers={"Authorization": f"Bearer {lead_token}"},
        )

    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_auto_draft_handles_missing_reflection_and_comments():
    """Wenn keine Reflection + keine Kommentare, Draft ist trotzdem valide."""
    from app.services.report_auto_draft import _render_draft

    text = _render_draft(
        agent_name="Bot",
        agent_emoji="🤖",
        task_title="Task",
        task_id="abc12345",
        reflection=None,
        recent_comments=[],
    )

    # Muss mindestens Header + Footer haben
    assert "🤖" in text
    assert "Task ❌" in text
    assert "abc12345" in text
    # Reflection/Comments Sektionen fehlen
    assert "<b>Reflexion des Agenten</b>" not in text
    assert "<b>Letzte Kommentare</b>" not in text
