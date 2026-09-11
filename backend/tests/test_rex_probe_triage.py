"""REX-Review-Proben zu PR #513 (Reflexions-Triage).

Kernverdacht: `_handle_reflection_verdict` waehlt die zu uebernehmende
Reflexion per

    .where(comment_type == "reflection").order_by(created_at.desc()).first()

OHNE den Filter `author_type != "system"`. Genau diesen Filter hatte der
geloeschte `_load_reflections_for_task` ausdruecklich ("W4.2: skip
auto-generated summaries ... would re-introduce the noise we are eliminating
in W4"). Da `auto_memory.py:167-176` bei JEDEM Task-Abschluss selbst einen
`reflection`-Kommentar mit author_type='system' nachschiebt ("**Task
erledigt:** ..."), ist die NEUESTE Reflexion beim Triage-Zeitpunkt in aller
Regel die System-Telemetrie — nicht die Reflexion des Agenten.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.memory import BoardMemory
from tests.conftest import test_engine
from tests.test_reflection_enforcement import (
    _make_agent_with_token, _agent_headers, make_full_reflection_content,
)

AGENT_LESSON = (
    "## Was wurde gemacht\nAgenten-Reflexion mit echtem Inhalt.\n\n"
    "## Was hat funktioniert\nDie Sache lief.\n\n"
    "## Was war unklar\nNichts wesentliches.\n\n"
    "## Lesson für Agent-Memory\n"
    "MERKSATZ-DES-AGENTEN: bei Regressionsvergleichen die Mengen vergleichen, "
    "nicht die Zaehler — backend/tests, comm -23 fail_branch.txt fail_main.txt."
)

SYSTEM_TELEMETRY = (
    "**Task erledigt:** Irgendeine Karte\n"
    "**Agent:** Cody\n"
    "**Dauer:** 4m\n"
    "**Prioritaet:** high\n\n"
    "**Letzte Kommentare:**\n- irgendein Kommentarauszug ohne Erkenntniswert"
)

VERDICT_ADOPT = (
    "Urteil: uebernehmen\n"
    "Quelle: Karte 9f1c97ba, Kommentar des Entwicklers"
)


async def _add_comment(task_id, *, content, author_type, author_agent_id=None):
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        c = TaskComment(
            id=uuid.uuid4(), task_id=task_id, author_type=author_type,
            author_agent_id=author_agent_id, comment_type="reflection",
            content=content,
        )
        s.add(c)
        await s.commit()
    return c


async def _lessons(board_id):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(
            select(BoardMemory)
            .where(BoardMemory.board_id == board_id)
            .where(BoardMemory.memory_type == "lesson")
        )).all())


@pytest.mark.asyncio
async def test_probe_verdict_must_not_adopt_the_system_telemetry_comment(
    client, fake_redis, make_board, make_task,
):
    """BEFUND. Reihenfolge wie im echten Ablauf:
      1. Agent postet seine Reflexion
      2. Task geht auf done -> auto_memory schiebt die System-Telemetrie
         als zweiten `reflection`-Kommentar nach
      3. Lead triagiert mit 'uebernehmen'
    Erwartung: die Lesson traegt den Inhalt des AGENTEN, nicht die Telemetrie.
    """
    board = await make_board(slug=f"tri-{uuid.uuid4().hex[:6]}")
    cody, _ = await _make_agent_with_token(name="Cody", board_id=board.id)
    lead, lead_token = await _make_agent_with_token(
        name="Boss", board_id=board.id, is_board_lead=True, role="orchestrator",
    )
    task = await make_task(board_id=board.id, status="done", assigned_agent_id=cody.id)

    await _add_comment(task.id, content=AGENT_LESSON, author_type="agent",
                       author_agent_id=cody.id)
    await _add_comment(task.id, content=SYSTEM_TELEMETRY, author_type="system")

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                json={"content": VERDICT_ADOPT, "comment_type": "reflection_verdict"},
                headers=_agent_headers(lead_token),
            )
    assert resp.status_code in (200, 201), resp.text

    lessons = await _lessons(board.id)
    print(f"\n[SYS] Lessons angelegt: {len(lessons)}")
    for m in lessons:
        print(f"[SYS]   agent_id={m.agent_id} content={m.content[:90]!r}")
    assert len(lessons) == 1, "genau eine Lesson erwartet"
    got = lessons[0]

    assert "Task erledigt" not in got.content, (
        "Die Lesson enthaelt die auto-generierte System-Telemetrie statt der "
        "Agenten-Reflexion. Der geloeschte _load_reflections_for_task filterte "
        "author_type='system' genau deshalb heraus (W4.2)."
    )
    assert "MERKSATZ-DES-AGENTEN" in got.content, (
        "Die Lesson enthaelt nicht die Reflexion des Agenten"
    )
    assert got.agent_id == cody.id, (
        f"agent_id={got.agent_id} — bei der System-Telemetrie ist "
        "author_agent_id NULL, die Lesson landet dann in keiner Agenten-Ebene"
    )


@pytest.mark.asyncio
async def test_probe_agent_reflection_alone_is_adopted_correctly(
    client, fake_redis, make_board, make_task,
):
    """Gegenprobe: gibt es NUR die Agenten-Reflexion, funktioniert die
    Uebernahme wie vorgesehen. Zeigt, dass der Befund oben an der Auswahl
    haengt und nicht am Mechanismus."""
    board = await make_board(slug=f"tri-{uuid.uuid4().hex[:6]}")
    cody, _ = await _make_agent_with_token(name="Cody", board_id=board.id)
    lead, lead_token = await _make_agent_with_token(
        name="Boss", board_id=board.id, is_board_lead=True, role="orchestrator",
    )
    task = await make_task(board_id=board.id, status="done", assigned_agent_id=cody.id)
    await _add_comment(task.id, content=AGENT_LESSON, author_type="agent",
                       author_agent_id=cody.id)

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                json={"content": VERDICT_ADOPT, "comment_type": "reflection_verdict"},
                headers=_agent_headers(lead_token),
            )
    assert resp.status_code in (200, 201), resp.text
    lessons = await _lessons(board.id)
    print(f"\n[OK] Lessons={len(lessons)} content={lessons[0].content[:80]!r}")
    assert len(lessons) == 1
    assert "MERKSATZ-DES-AGENTEN" in lessons[0].content
    assert "**Quelle:**" in lessons[0].content


@pytest.mark.asyncio
async def test_probe_reflection_alone_writes_no_memory(
    client, fake_redis, make_board, make_task,
):
    """DoD: Reflexion OHNE Lead-Urteil -> kein Eintrag in board_memory."""
    board = await make_board(slug=f"tri-{uuid.uuid4().hex[:6]}")
    cody, cody_token = await _make_agent_with_token(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, status="in_progress",
                           assigned_agent_id=cody.id)

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                json={"content": AGENT_LESSON, "comment_type": "reflection"},
                headers=_agent_headers(cody_token),
            )
    assert resp.status_code in (200, 201), resp.text
    lessons = await _lessons(board.id)
    print(f"\n[NOVERDICT] Lessons nach reiner Reflexion: {len(lessons)}")
    assert len(lessons) == 0


@pytest.mark.asyncio
async def test_probe_non_lead_may_not_triage(client, fake_redis, make_board, make_task):
    """Nur der Lead darf triagieren — sonst waere die Triage umgehbar."""
    board = await make_board(slug=f"tri-{uuid.uuid4().hex[:6]}")
    cody, cody_token = await _make_agent_with_token(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, status="done", assigned_agent_id=cody.id)
    await _add_comment(task.id, content=AGENT_LESSON, author_type="agent",
                       author_agent_id=cody.id)

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": VERDICT_ADOPT, "comment_type": "reflection_verdict"},
            headers=_agent_headers(cody_token),
        )
    print(f"\n[NONLEAD] http={resp.status_code} detail={resp.json().get('detail','')[:80]}")
    assert resp.status_code == 403
    assert len(await _lessons(board.id)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("quelle", ["-", ".", "x"])
async def test_probe_weak_source_is_accepted(
    client, fake_redis, make_board, make_task, quelle,
):
    """Die Quellenpflicht prueft nur auf 'nicht leer'. Ein Bindestrich
    genuegt — dokumentierend, nicht zwingend ein Befund."""
    board = await make_board(slug=f"tri-{uuid.uuid4().hex[:6]}")
    cody, _ = await _make_agent_with_token(name="Cody", board_id=board.id)
    lead, lead_token = await _make_agent_with_token(
        name="Boss", board_id=board.id, is_board_lead=True, role="orchestrator",
    )
    task = await make_task(board_id=board.id, status="done", assigned_agent_id=cody.id)
    await _add_comment(task.id, content=AGENT_LESSON, author_type="agent",
                       author_agent_id=cody.id)

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
                json={"content": f"Urteil: uebernehmen\nQuelle: {quelle}",
                      "comment_type": "reflection_verdict"},
                headers=_agent_headers(lead_token),
            )
    print(f"\n[WEAK '{quelle}'] http={resp.status_code} Lessons={len(await _lessons(board.id))}")
