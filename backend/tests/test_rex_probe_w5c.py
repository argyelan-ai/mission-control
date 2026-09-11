"""REX-Review-Proben zu PR #512 (W5-C), 11.09.2026.

Schwerpunkt laut Auftrag: der neue board-agnostische Endpoint
`POST /api/v1/agent/tasks/{task_id}/comments`. Ein Endpoint, der die
Board-Grenze nicht mehr im Pfad traegt, ist die Stelle, an der
Mandantentrennung leise verloren geht. Geprueft wird NICHT, ob die
403-Zeile dasteht, sondern ob sie greift — und zwar per Request.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _mk_board(name):
    from app.models.board import Board
    bid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=bid, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}"))
        await s.commit()
    return bid


async def _mk_agent(board_id, name, scopes, *, lead=False, current_task_id=None):
    from app.models.agent import Agent
    from app.auth import generate_agent_token
    aid = uuid.uuid4()
    token, thash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(id=aid, name=name, role="orchestrator" if lead else "researcher",
                    board_id=board_id, agent_token_hash=thash, is_board_lead=lead,
                    scopes=scopes, current_task_id=current_task_id,
                    provision_status="provisioned"))
        await s.commit()
    return aid, token


async def _mk_task(board_id, title, status="in_progress", assigned=None):
    from app.models.task import Task
    tid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(id=tid, board_id=board_id, title=title, status=status,
                   assigned_agent_id=assigned))
        await s.commit()
    return tid


async def _comment_count(task_id=None):
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        q = select(func.count()).select_from(TaskComment)
        if task_id is not None:
            q = q.where(TaskComment.task_id == task_id)
        return (await s.exec(q)).one()


URL = "/api/v1/agent/tasks/{tid}/comments"
MANAGE = ["tasks:read", "tasks:write", "tasks:create", "tasks:manage", "chat:write"]
NO_MANAGE = ["tasks:read", "tasks:write", "tasks:create", "chat:write"]


@pytest.mark.asyncio
async def test_probe_cross_board_comment_is_refused(client, fake_redis):
    """SCHWERPUNKT: Lead auf Board X kommentiert eine Karte auf Board Y.
    Muss 403 sein UND nichts schreiben."""
    board_x = await _mk_board("BoardX")
    board_y = await _mk_board("BoardY")
    _, lead_token = await _mk_agent(board_x, "LeadX", MANAGE, lead=True)
    foreign = await _mk_task(board_y, "Karte auf fremdem Board")

    before_total = await _comment_count()
    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            URL.format(tid=foreign),
            json={"content": "Ich schreibe ueber die Board-Grenze", "comment_type": "progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    after_foreign = await _comment_count(foreign)
    after_total = await _comment_count()
    print(f"\n[XB] cross-board -> http={resp.status_code} detail={resp.json().get('detail')}")
    print(f"[XB] Kommentare auf fremder Karte={after_foreign}, gesamt {before_total}->{after_total}")
    assert resp.status_code == 403
    assert after_foreign == 0, "403 hat trotzdem geschrieben"
    assert after_total == before_total


@pytest.mark.asyncio
async def test_probe_same_board_lead_may_comment(client, fake_redis):
    """Gegenprobe: derselbe Aufruf auf dem EIGENEN Board muss durchgehen —
    sonst waere das 403 oben nur ein kaputter Endpoint, kein Schutz."""
    board = await _mk_board("BoardSame")
    _, lead_token = await _mk_agent(board, "Lead", MANAGE, lead=True)
    own = await _mk_task(board, "Karte auf eigenem Board")

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            URL.format(tid=own),
            json={"content": "Regulaerer Kommentar", "comment_type": "progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    print(f"\n[SAME] same-board -> http={resp.status_code}, Kommentare={await _comment_count(own)}")
    assert resp.status_code == 201
    assert await _comment_count(own) == 1


@pytest.mark.asyncio
async def test_probe_without_tasks_manage_403_and_nothing_written_anywhere(client, fake_redis):
    """Ohne tasks:manage -> 403, und WEDER die genannte Karte NOCH die
    eigene aktive Karte bekommt etwas (kein stiller Fallback)."""
    board = await _mk_board("BoardScope")
    own_task = await _mk_task(board, "Eigene aktive Karte")
    _, worker_token = await _mk_agent(board, "Worker", NO_MANAGE,
                                      current_task_id=own_task)
    other = await _mk_task(board, "Andere Karte")

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            URL.format(tid=other),
            json={"content": "Worker ohne manage", "comment_type": "progress"},
            headers={"Authorization": f"Bearer {worker_token}"},
        )
    c_other, c_own = await _comment_count(other), await _comment_count(own_task)
    print(f"\n[SCOPE] ohne tasks:manage -> http={resp.status_code} detail={resp.json().get('detail')}")
    print(f"[SCOPE] Kommentare: genannte Karte={c_other}, eigene aktive Karte={c_own}")
    assert resp.status_code == 403
    assert c_other == 0 and c_own == 0


@pytest.mark.asyncio
async def test_probe_scope_is_checked_before_task_existence_is_disclosed(client, fake_redis):
    """Reihenfolge-Probe: ein Agent ohne tasks:manage darf nicht ueber den
    Statuscode erfahren, ob eine fremde Task-ID existiert. Scope-Check
    (Dependency) muss VOR dem Existenz-Lookup greifen -> 403, nicht 404."""
    board = await _mk_board("BoardOrder")
    _, worker_token = await _mk_agent(board, "Worker", NO_MANAGE)
    ghost = uuid.uuid4()  # existiert nicht

    resp = await client.post(
        URL.format(tid=ghost),
        json={"content": "x", "comment_type": "progress"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    print(f"\n[ORDER] ohne manage + nicht existente Karte -> http={resp.status_code}")
    assert resp.status_code == 403, (
        "404 hier wuerde die Existenz fremder Karten preisgeben, bevor der "
        "Scope geprueft ist"
    )


@pytest.mark.asyncio
async def test_probe_unknown_task_is_rejected_not_skipped(client, fake_redis):
    """Marks Frage: gibt es einen Pfad, auf dem `task` nicht gefunden wird
    und die Board-Pruefung UEBERSPRUNGEN statt abgelehnt wird?"""
    board = await _mk_board("BoardGhost")
    _, lead_token = await _mk_agent(board, "Lead", MANAGE, lead=True)
    ghost = uuid.uuid4()

    before = await _comment_count()
    resp = await client.post(
        URL.format(tid=ghost),
        json={"content": "x", "comment_type": "progress"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    print(f"\n[GHOST] unbekannte Karte -> http={resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 404
    assert await _comment_count() == before


@pytest.mark.asyncio
async def test_probe_handoff_via_task_id_wakes_assigned_worker(client, fake_redis):
    """`--task-id` + comment_type=handoff: landet auf der genannten Karte
    UND erreicht den zugewiesenen Worker ueber seinen eigenen /me/poll."""
    from app.models.task import TaskComment
    board = await _mk_board("BoardWake")
    _, lead_token = await _mk_agent(board, "Lead", MANAGE, lead=True)
    worker_id, worker_token = await _mk_agent(board, "Worker", NO_MANAGE)
    card = await _mk_task(board, "Worker-Karte", assigned=worker_id)

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            URL.format(tid=card),
            json={"content": "Bitte erst Fix 3b", "comment_type": "handoff"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(TaskComment).where(TaskComment.task_id == card))).all()
    print(f"\n[WAKE] Kommentar auf genannter Karte: {len(rows)} "
          f"type={rows[0].comment_type if rows else '-'}")
    assert len(rows) == 1 and rows[0].comment_type == "handoff"

    poll = await client.get("/api/v1/agent/me/poll",
                            headers={"Authorization": f"Bearer {worker_token}"})
    body = poll.json() if poll.status_code == 200 else {}
    blob = str(body)
    print(f"[WAKE] worker poll http={poll.status_code} state={body.get('state')}")
    print(f"[WAKE] Handoff-Text im Poll enthalten: {'Bitte erst Fix 3b' in blob}")
    assert "Bitte erst Fix 3b" in blob, (
        "Der Handoff erreicht den zugewiesenen Worker nicht ueber seinen Poll"
    )


# ── mc ask ohne aktiven Task: kommt die Frage nachweislich bei Mark an? ──

@pytest.mark.asyncio
async def test_probe_lead_ask_without_task_reaches_mark_on_dm_thread(client, fake_redis):
    """Lead ohne aktive Karte stellt eine Frage. Beweis nicht ueber den
    Statuscode, sondern ueber die Zeile, die Mark sieht: Message liegt auf
    einem Thread der Art `dm`, der diesem Agenten gehoert, und der Thread
    steht auf awaiting."""
    from app.models.thread import Thread, Message

    board = await _mk_board("BoardAsk")
    lead_id, lead_token = await _mk_agent(board, "Lead", MANAGE, lead=True)

    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Soll ich PR 512 mergen?", "blocking": False},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    print(f"\n[ASK] http={resp.status_code} body={resp.text[:200]}")
    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        threads = (await s.exec(select(Thread).where(Thread.agent_id == lead_id))).all()
        print(f"[ASK] Threads des Agenten: {[(t.kind, str(t.id)[:8]) for t in threads]}")
        assert threads, "kein Thread angelegt"
        dm = [t for t in threads if t.kind == "dm"]
        assert dm, f"Frage liegt nicht auf einem dm-Thread: {[t.kind for t in threads]}"
        msgs = (await s.exec(select(Message).where(Message.thread_id == dm[0].id))).all()
        print(f"[ASK] Messages auf dem DM-Thread: {len(msgs)}")
        for m in msgs:
            print(f"[ASK]   type={m.message_type} body={m.body[:60]!r} "
                  f"question_meta={m.question_meta}")
        assert any("PR 512" in (m.body or "") for m in msgs), \
            "die Frage steht nicht im DM-Thread"
        q = [m for m in msgs if "PR 512" in (m.body or "")][0]
        assert q.message_type == "question", f"als {q.message_type} abgelegt, nicht als Frage"
        assert (q.question_meta or {}).get("awaiting") is True, \
            f"Frage steht nicht auf awaiting: {q.question_meta}"


@pytest.mark.asyncio
async def test_probe_worker_ask_without_task_still_409(client, fake_redis):
    """Regressions-Pin: ein Worker OHNE tasks:manage bleibt ohne aktive
    Karte stumm — die Oeffnung gilt nur fuer Leads."""
    board = await _mk_board("BoardAskW")
    _, worker_token = await _mk_agent(board, "Worker", NO_MANAGE)
    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "darf ich?", "blocking": False},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    print(f"\n[ASKW] Worker ohne manage -> http={resp.status_code}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_probe_blocking_ask_without_task_is_rejected(client, fake_redis):
    """--blocking ohne Karte: es gibt nichts zu pausieren -> 409."""
    board = await _mk_board("BoardAskB")
    _, lead_token = await _mk_agent(board, "Lead", MANAGE, lead=True)
    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "warten?", "blocking": True},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    print(f"\n[ASKB] blocking ohne Karte -> http={resp.status_code} detail={resp.json().get('detail')}")
    assert resp.status_code == 409
