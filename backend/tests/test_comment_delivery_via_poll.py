"""User-Kommentar-Delivery via /api/v1/agent/me/poll.

Fuer Non-Gateway-Agents (cli-bridge, host) ist dies der einzige Weg,
dass User-Kommentare ankommen. Ohne diesen Fix waren z.B. Davinci, Boss
und Deployer stumm — die Nachfragen des Operators kamen nie beim Agent an.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_with_task(session: AsyncSession, status="in_progress"):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.auth import generate_agent_token
    import datetime as _dt

    board = Board(id=uuid.uuid4(), name="Test", slug="t")
    session.add(board)
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name="Davinci",
        board_id=board.id,
        agent_runtime="cli-bridge",
        status="idle",
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    session.add(agent)
    # Tasks in diesen Tests simulieren ein laufendes (working) Task: dispatched
    # UND bereits beim Agent angekommen (ack_at gesetzt). Ohne ack_at würde der
    # Poll korrekt als "needs prompt delivery" interpretieren → new_task.
    _now = _dt.datetime.now(tz=_dt.timezone.utc)
    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Test Task",
        status=status,
        assigned_agent_id=agent.id,
        dispatched_at=_now if status in ("in_progress", "blocked") else None,
        ack_at=_now if status in ("in_progress", "blocked") else None,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


async def _add_user_comment(session: AsyncSession, task_id: uuid.UUID, content: str):
    from app.models.task import TaskComment
    c = TaskComment(
        id=uuid.uuid4(),
        task_id=task_id,
        author_type="user",
        content=content,
        comment_type="comment",
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest.mark.asyncio
async def test_poll_returns_new_user_comments(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        await _add_user_comment(s, task.id, "wo sind die prompts?")

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "new_comments" in data
    assert len(data["new_comments"]) == 1
    assert data["new_comments"][0]["content"] == "wo sind die prompts?"
    assert data["new_comments"][0]["task_id"] == str(task.id)
    assert data["state"] == "working"


@pytest.mark.asyncio
async def test_poll_does_not_redeliver_same_comment(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        await _add_user_comment(s, task.id, "once")

    r1 = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert len(r1.json()["new_comments"]) == 1

    r2 = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert r2.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_ignores_agent_own_comments(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        # Ein checkpoint vom Agent selbst
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=agent.id,
            content="mein checkpoint",
            comment_type="checkpoint",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_delivers_multiple_comments_in_order(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        await _add_user_comment(s, task.id, "first")
        await _add_user_comment(s, task.id, "second")
        await _add_user_comment(s, task.id, "third")

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    contents = [c["content"] for c in resp.json()["new_comments"]]
    assert contents == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_poll_delivers_user_comment_on_done_task(client: AsyncClient):
    """User-Comment auf done Task wird zugestellt (Fix 2026-05-18).

    Der Operator hatte auf "MC Home Page fixen" (assigned=Boss, done) kommentiert und
    erwartet dass Boss reagiert. Frueher wurde der Comment im Filter geschluckt.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="done")
        await _add_user_comment(s, task.id, "@Boss bitte alternative version")

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    new_comments = resp.json()["new_comments"]
    assert len(new_comments) == 1
    assert new_comments[0]["content"] == "@Boss bitte alternative version"


@pytest.mark.asyncio
async def test_poll_does_not_deliver_comments_for_failed_tasks(client: AsyncClient):
    """failed Tasks sollen nicht ueber Comment reaktiviert werden — explizites Re-Open noetig."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="failed")
        await _add_user_comment(s, task.id, "nach failed")

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_does_not_deliver_comments_for_aborted_tasks(client: AsyncClient):
    """aborted Tasks sollen ebenfalls terminal bleiben."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="aborted")
        await _add_user_comment(s, task.id, "nach aborted")

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_idle_state_still_has_empty_new_comments_key(client: AsyncClient):
    """Wenn nichts zu tun: Response hat trotzdem new_comments Key (leere Liste)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        from app.models.board import Board
        from app.auth import generate_agent_token

        board = Board(id=uuid.uuid4(), name="Test", slug="t")
        s.add(board)
        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            id=uuid.uuid4(),
            name="Idle",
            board_id=board.id,
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            scopes=["tasks:read", "heartbeat"],
        )
        s.add(agent)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw_token}"})
    data = resp.json()
    assert data["state"] == "idle"
    assert data["new_comments"] == []


# ── System-Event-Delivery Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_delivers_subtask_completed_system_event(client: AsyncClient):
    """subtask_completed (author_type=agent) wird an Parent-Task-Agent ausgeliefert."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        # Komm von ANDEREM Agent (z.B. Worker im Subtask). Nicht vom Poller selbst.
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),  # anderer Agent
            comment_type="subtask_completed",
            content="**Subtask abgeschlossen:** Davinci ist fertig",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    assert len(data["new_comments"]) == 1
    nc = data["new_comments"][0]
    assert nc["comment_type"] == "subtask_completed"
    assert nc["source"] == "system"


@pytest.mark.asyncio
async def test_poll_delivers_blocker_system_event(client: AsyncClient):
    """blocker-Event wird ausgeliefert (Orchestrator-Parent erfaehrt, dass Child blockt)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),
            comment_type="blocker",
            content="Child-Task blockiert wegen X",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    ncs = resp.json()["new_comments"]
    assert len(ncs) == 1
    assert ncs[0]["source"] == "system"


@pytest.mark.asyncio
async def test_poll_delivers_feedback_comment_from_other_agent(client: AsyncClient):
    """feedback-Kommentare anderer Agents (z.B. Boss → Worker, Reviewer → Worker)
    sind actionable und MUESSEN ausgeliefert werden.

    Regressionstest fuer Bug 2026-04-23: Tester war blocked, Boss postete einen
    feedback-Comment ("Sidecar gefixt, retry") — der Worker hat den nie gesehen,
    weil _DELIVER_SYSTEM_COMMENT_TYPES feedback nicht enthielt. Erst nach
    Umwandlung in einen system-Comment kam er an.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="blocked")
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),  # Boss/Reviewer, nicht der Poller
            comment_type="feedback",
            content="Sidecar gefixt, bitte versuche erneut.",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    ncs = resp.json()["new_comments"]
    assert len(ncs) == 1, f"feedback-Comment muss ausgeliefert werden, ncs={ncs}"
    assert ncs[0]["comment_type"] == "feedback"
    assert ncs[0]["source"] == "system"
    assert ncs[0]["content"] == "Sidecar gefixt, bitte versuche erneut."


@pytest.mark.asyncio
async def test_poll_delivers_handoff_comment_from_another_agent(client: AsyncClient):
    """Bug 9 fix: handoff-Kommentare anderer Agents (Board Lead -> Worker
    Briefing auf bereits assigned Task) MUESSEN ausgeliefert werden.

    Live-Bug 2026-05-13: Boss postete als `mc comment` (default type=message)
    ein umfangreiches Briefing auf Sparky's Sub-Task. Sparky pollte normal,
    sah aber nichts — message-type wird als Routine gefiltert. Boss haette
    `mc comment --type handoff` (oder `mc delegate` fuer neuen Sub-Task)
    nutzen muessen. Bug 9 nimmt handoff in DELIVERABLE_SYSTEM_TYPES auf,
    sodass dieser Workflow funktioniert.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),  # Board Lead, nicht der Poller
            comment_type="handoff",
            content="Briefing: docker-compose fuer livekit-sidecar bauen, DoD: ...",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    ncs = resp.json()["new_comments"]
    assert len(ncs) == 1, f"handoff-Comment muss ausgeliefert werden, ncs={ncs}"
    assert ncs[0]["comment_type"] == "handoff"
    assert ncs[0]["source"] == "system"
    assert "Briefing" in ncs[0]["content"]


@pytest.mark.asyncio
async def test_poll_skips_own_handoff_comment(client: AsyncClient):
    """Echo-Loop-Schutz: eigener handoff darf nicht zurueckgespiegelt werden."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=agent.id,  # Poller selbst
            comment_type="handoff",
            content="ich selbst",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_post_message_on_foreign_assigned_task_returns_delivery_hint(client: AsyncClient):
    """Bug 9 fix: wenn Agent A einen comment_type='message' postet auf einem
    Task der Agent B zugewiesen ist, antwortet die API mit `delivery_hint`
    der erklaert dass der Worker nichts sieht. Soft-Warn, kein Fail.
    """
    from app.models.agent import Agent as _Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.auth import generate_agent_token
    import datetime as _dt

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Test", slug="t")
        s.add(board)

        # Poster (Board Lead Boss-Style)
        boss_raw, boss_hash = generate_agent_token()
        boss = _Agent(
            id=uuid.uuid4(), name="Boss", board_id=board.id, agent_runtime="host",
            agent_token_hash=boss_hash,
            scopes=["tasks:read", "tasks:write", "chat:write", "heartbeat"],
        )
        s.add(boss)

        # Worker (assigned)
        _worker_raw, worker_hash = generate_agent_token()
        worker = _Agent(
            id=uuid.uuid4(), name="Sparky", board_id=board.id, agent_runtime="cli-bridge",
            agent_token_hash=worker_hash,
            scopes=["tasks:read", "tasks:write", "heartbeat"],
        )
        s.add(worker)

        now = _dt.datetime.now(tz=_dt.timezone.utc)
        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="Sub", status="in_progress",
            assigned_agent_id=worker.id, dispatched_at=now, ack_at=now,
        )
        s.add(task)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
        headers={"Authorization": f"Bearer {boss_raw}"},
        json={"content": "Briefing-Text", "comment_type": "message"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "delivery_hint" in body, f"expected delivery_hint, got: {body}"
    hint = body["delivery_hint"].lower()
    assert "handoff" in hint or "delegate" in hint, (
        f"hint must mention handoff or delegate, got: {body['delivery_hint']}"
    )


@pytest.mark.asyncio
async def test_post_message_on_own_assigned_task_has_no_hint(client: AsyncClient):
    """Wenn Agent auf seinem eigenen assigned Task einen message-Comment postet
    (z.B. Checkpoint / Update an sich selbst), keine Hint."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        # task ist agent zugewiesen — agent ist auch Poster

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "mein Update", "comment_type": "message"},
    )
    assert resp.status_code == 201, resp.text
    assert "delivery_hint" not in resp.json()


@pytest.mark.asyncio
async def test_post_handoff_on_foreign_assigned_task_has_no_hint(client: AsyncClient):
    """Wenn Agent korrekt handoff nutzt — keine Hint noetig."""
    from app.models.agent import Agent as _Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.auth import generate_agent_token
    import datetime as _dt

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Test", slug="t")
        s.add(board)
        boss_raw, boss_hash = generate_agent_token()
        boss = _Agent(
            id=uuid.uuid4(), name="Boss", board_id=board.id, agent_runtime="host",
            agent_token_hash=boss_hash,
            scopes=["tasks:read", "tasks:write", "chat:write", "heartbeat"],
        )
        s.add(boss)
        worker = _Agent(
            id=uuid.uuid4(), name="Sparky", board_id=board.id, agent_runtime="cli-bridge",
            agent_token_hash=generate_agent_token()[1],
            scopes=["tasks:read", "tasks:write", "heartbeat"],
        )
        s.add(worker)
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="Sub", status="in_progress",
            assigned_agent_id=worker.id, dispatched_at=now, ack_at=now,
        )
        s.add(task)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/comments",
        headers={"Authorization": f"Bearer {boss_raw}"},
        json={"content": "Briefing", "comment_type": "handoff"},
    )
    assert resp.status_code == 201, resp.text
    assert "delivery_hint" not in resp.json()


@pytest.mark.asyncio
async def test_poll_skips_own_feedback_comment(client: AsyncClient):
    """Eigene feedback-Kommentare (z.B. Reviewer → Worker, aber wir sind der Reviewer)
    duerfen nicht zurueckgespiegelt werden — gleiche Echo-Loop-Vermeidung wie bei
    anderen system-Comment-Types."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=agent.id,  # Poller selbst
            comment_type="feedback",
            content="ich selbst habe Feedback gepostet",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_does_not_deliver_irrelevant_agent_comments(client: AsyncClient):
    """progress/checkpoint von anderen Agents ist KEIN actionable event — skippen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),
            comment_type="progress",
            content="Update von anderem Agent",
        ))
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),
            comment_type="checkpoint",
            content="Checkpoint von anderem Agent",
        ))
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_skips_own_subtask_completed(client: AsyncClient):
    """Agent darf seinen EIGENEN subtask_completed Event nicht zurueckbekommen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        c = TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=agent.id,   # Poller selbst!
            comment_type="subtask_completed",
            content="ich selbst habe was abgeschlossen",
        )
        s.add(c)
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["new_comments"] == []


@pytest.mark.asyncio
async def test_poll_delivers_mixed_user_and_system(client: AsyncClient):
    """User-Comment + System-Event in einem Poll → beide, chronologisch, mit source."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id,
            author_type="user",
            comment_type="message",
            content="mark first",
        ))
        await s.commit()
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=uuid.uuid4(),
            comment_type="subtask_completed",
            content="davinci done",
        ))
        await s.commit()

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    ncs = resp.json()["new_comments"]
    assert len(ncs) == 2
    assert ncs[0]["source"] == "user"
    assert ncs[1]["source"] == "system"
    assert ncs[0]["content"] == "mark first"
    assert ncs[1]["comment_type"] == "subtask_completed"


@pytest.mark.asyncio
async def test_poll_cursor_advances_past_non_deliverable_comments(client: AsyncClient):
    """Cursor geht weiter selbst wenn dazwischen non-deliverable comments liegen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        from app.models.task import TaskComment
        # user comment 1
        s.add(TaskComment(task_id=task.id, author_type="user", comment_type="message", content="a"))
        await s.commit()
        # noise zwischendrin (skipped)
        s.add(TaskComment(task_id=task.id, author_type="agent", author_agent_id=uuid.uuid4(),
                          comment_type="progress", content="noise"))
        await s.commit()
        # user comment 2
        s.add(TaskComment(task_id=task.id, author_type="user", comment_type="message", content="b"))
        await s.commit()

    r1 = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    contents = [c["content"] for c in r1.json()["new_comments"]]
    assert contents == ["a", "b"]

    r2 = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert r2.json()["new_comments"] == []
