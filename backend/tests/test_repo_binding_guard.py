"""Repo-Bindungs-Waechter (2026-09): eine Delegation, die konkrete
Datei-Fundstellen nennt, aber kein Repo bindet, wird abgelehnt.

Vorfall: drei Karten ohne `mc delegate --repo`, deren Beschreibungen Fundstellen
wie `backend/app/routers/x.py:123` nannten; der Worker landete im gemeinsamen
Ad-hoc-Klon `marknx/mc-workspace` statt im System-Repo, und die Arbeit war
wertlos. Die Regel steht als Merksatz in den Docs — dieser Test macht sie
technisch verbindlich.

Sabotage in BEIDE Richtungen, wie von der Karte verlangt:
  - verbotener Fall (Fundstelle, keine Bindung)  → 422, kein Task angelegt
  - erlaubter Fall (Recherche-Karte ohne Fundstelle) → 201
  - erlaubter Fall (mit Repo/Projekt gebunden)   → 201
  - erlaubter Fall (bewusste Markierung)         → 201 + dokumentierte Ausnahme

Der Mutation-Guard am Ende pinnt die ZWEI Bedingungen der Regel fest: fällt
eine davon weg, wird der Waechter entweder zahnlos oder er schlägt auf
Alltagskarten falsch an.
"""

import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.repo import Repo
from app.models.task import Task

from tests.conftest import test_engine


async def _mk(objs: list):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in objs:
            s.add(o)
        await s.commit()
        for o in objs:
            await s.refresh(o)


async def _agent(board_id, **kw) -> tuple[Agent, str]:
    token, token_hash = generate_agent_token()
    kw.setdefault("role", "orchestrator")
    agent = Agent(
        id=uuid.uuid4(),
        name=f"Agent-{uuid.uuid4().hex[:5]}",
        board_id=board_id,
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "tasks:create"],
        provision_status="provisioned",
        **kw,
    )
    await _mk([agent])
    return agent, token


def _delegate_patches():
    return (
        patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock),
        patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock),
        patch(
            "app.services.operations.check_dispatch_allowed",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    )


async def _open_card(board_id, agent) -> Task:
    """A card the delegating lead owns/works on, to hang the delegation under.

    Since task f8c9cdb9 `mc delegate` no longer opens a parentless card: with
    no active task the caller must name a parent, otherwise the server refuses
    with 409 before creating anything. These tests are about the repo-binding
    guard, not about the parent rule, so they run from a lead with a real
    active card — the shape the guard normally sees in production.
    """
    parent = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        title="Aktive Karte des Leads",
        status="in_progress",
        assigned_agent_id=agent.id,
    )
    await _mk([parent])
    agent.current_task_id = parent.id
    await _mk([agent])
    return parent


# ── Verbotener Fall: Fundstelle ohne Bindung ─────────────────────────────


@pytest.mark.asyncio
async def test_delegate_with_file_reference_and_no_repo_is_rejected(client, fake_redis):
    """Der Vorfall selbst: Fundstelle in der Beschreibung, kein Repo, kein
    Projekt, keine Markierung → 422 und KEIN Task-Fragment."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="researcher")
    parent = await _open_card(board.id, lead)

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Fix am Delegationspfad",
                "description": (
                    "Der Fehler sitzt in backend/app/routers/agent_scoped.py:1289 "
                    "und muss dort behoben werden."
                ),
                "assigned_agent_id": str(worker.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 422, resp.text
    # Die Meldung muss die Fundstelle nennen — sonst weiss der Aufrufer nicht,
    # was die Bindung verlangt.
    assert "agent_scoped.py:1289" in resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        leftovers = (await s.exec(select(Task).where(Task.board_id == board.id))).all()
    assert [t.id for t in leftovers] == [parent.id], (
        "abgelehnte Delegation darf keine Karte hinterlassen"
    )


@pytest.mark.asyncio
async def test_agent_create_task_with_file_reference_and_no_repo_is_rejected(client, fake_redis):
    """Derselbe Waechter am zweiten Anlege-Pfad (POST /tasks) — sonst waere
    die Regel ueber den anderen Endpunkt umgehbar."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    agent, token = await _agent(board.id, is_board_lead=True)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks",
        json={
            "title": "Karte mit Fundstelle",
            "description": "Siehe frontend-v2/src/components/chat/Composer.tsx:610.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    assert "Composer.tsx:610" in resp.text


# ── Erlaubte Faelle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegate_with_file_reference_and_repo_passes(client, fake_redis):
    """Mit explizitem Repo ist die Bindung da — die Fundstelle bleibt erlaubt."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    repo = Repo(
        full_name=f"acme/tool-{uuid.uuid4().hex[:5]}",
        url="https://example.invalid/acme/tool",
        is_active=True,
    )
    await _mk([board, repo])
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="developer")
    await _open_card(board.id, lead)

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Fix am Delegationspfad",
                "description": "Der Fehler sitzt in backend/app/routers/agent_scoped.py:1289.",
                "assigned_agent_id": str(worker.id),
                "repo_id": str(repo.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_delegate_research_card_without_file_reference_passes(client, fake_redis):
    """Recherchekarte ohne Fundstelle — der Alltag darf NICHT blockiert werden.
    Genau eine Regel, die den Alltag blockiert, wird umgangen statt befolgt."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="researcher")
    await _open_card(board.id, lead)

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Recherche: Vergleich von Message-Queues",
                "description": (
                    "Vergleiche RabbitMQ, NATS und Redis Streams. Liefere eine "
                    "Entscheidungsvorlage mit Vor- und Nachteilen."
                ),
                "assigned_agent_id": str(worker.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_delegate_waiver_records_reason_on_card(client, fake_redis):
    """Die bewusste Markierung geht durch UND bleibt auffindbar — ohne den
    Kommentar waere `--no-repo-reason` ein stiller Bypass."""
    from app.models.task import TaskComment

    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="researcher")
    await _open_card(board.id, lead)

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Doku zur Fundstelle",
                "description": "Beschreibe backend/app/routers/agent_scoped.py:1289 im Handbuch.",
                "assigned_agent_id": str(worker.id),
                "no_repo_reason": "Reine Doku, kein Code im Repo",
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text
    subtask_id = uuid.UUID(resp.json()["subtask_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (
            await s.exec(select(TaskComment).where(TaskComment.task_id == subtask_id))
        ).all()
    waiver = [c for c in comments if c.comment_type == "repo_binding_waiver"]
    assert len(waiver) == 1, "die Ausnahme muss an der Karte dokumentiert sein"
    assert "Reine Doku, kein Code im Repo" in waiver[0].content


@pytest.mark.asyncio
async def test_delegate_inherits_project_from_parent_so_guard_stays_silent(client, fake_redis):
    """Karte unter einem Parent MIT Projekt: die Bindung wird geerbt, der
    Waechter darf NICHT zuschlagen. Ohne diesen Fall waere nicht bewiesen,
    dass der Guard nach der Vererbung greift statt davor."""
    from app.models.board import Project

    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    project = Project(
        id=uuid.uuid4(),
        board_id=board.id,
        name=f"Proj-{uuid.uuid4().hex[:5]}",
        github_repo_name="acme/proj",
        github_repo_url="https://example.invalid/acme/proj.git",
    )
    await _mk([board, project])
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="developer")

    parent = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        project_id=project.id,
        title="Parent mit Projekt",
        status="in_progress",
        assigned_agent_id=lead.id,
    )
    await _mk([parent])
    lead.current_task_id = parent.id
    await _mk([lead])

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Fix mit Fundstelle",
                "description": "Der Fehler sitzt in backend/app/routers/agent_scoped.py:1289.",
                "assigned_agent_id": str(worker.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text


# ── Erkennungsregel: Was zaehlt als Fundstelle, was nicht ────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("backend/app/routers/agent_scoped.py:1289", True),
        ("frontend-v2/src/components/chat/Composer.tsx:610", True),
        (".github/workflows/ci.yml:167", True),
        # Absolute Fundstelle — kommt in Bestandskarten vor.
        ("/workspace/mission-control/backend/app/main.py:258", True),
        # Ohne Verzeichnis nicht eindeutig -> NICHT als Fundstelle zaehlen.
        ("Composer.tsx:610", False),
        # Falschtreffer-Proben aus dem Bestand.
        ("Release am 16.09.2026", False),
        ("Python 3.5 wird unterstuetzt", False),
        ("Der Socket .tmux.sock blieb haengen", False),
        ("Siehe Kapitel 4:12 im Handbuch", False),
    ],
)
def test_file_reference_detection(text, expected):
    from app.services.repo_binding import find_file_references

    assert bool(find_file_references(text)) is expected


def test_guard_stays_silent_when_repo_or_project_is_bound():
    """Mutation-Guard: die Regel hat ZWEI Bedingungen. Nimmt man die
    Bindungs-Bedingung heraus, schlaegt der Waechter auf jeder Alltagskarte
    mit Fundstelle falsch an; nimmt man die Fundstellen-Bedingung heraus, ist
    er zahnlos. Beide Kontrastfaelle werden hier festgehalten."""
    from app.services.repo_binding import enforce_repo_binding

    ref = "backend/app/routers/agent_scoped.py:1289"

    # Bedingung 1 fehlt (keine Bindung, keine Markierung) -> muss werfen.
    with pytest.raises(Exception) as exc:
        enforce_repo_binding(
            title="x", description=ref, repo_bound=False, waiver_reason=None
        )
    assert "422" in str(exc.value) or "Fundstellen" in str(exc.value)

    # Bedingung 1 erfuellt (gebunden) -> laeuft durch.
    enforce_repo_binding(title="x", description=ref, repo_bound=True, waiver_reason=None)
    # Bedingung 1 erfuellt (Markierung) -> laeuft durch.
    enforce_repo_binding(title="x", description=ref, repo_bound=False, waiver_reason="Doku")
    # Bedingung 2 fehlt (keine Fundstelle) -> laeuft durch.
    enforce_repo_binding(
        title="x", description="Recherche ohne Fundstelle", repo_bound=False, waiver_reason=None
    )


def test_waiver_reason_may_not_be_empty_string():
    """Ein leerer Grund ist keine Markierung — sonst waere `--no-repo-reason ''`
    ein stiller Bypass, der wie eine bewusste Entscheidung aussieht."""
    from app.services.repo_binding import enforce_repo_binding

    with pytest.raises(Exception):
        enforce_repo_binding(
            title="x",
            description="backend/app/x.py:12",
            repo_bound=False,
            waiver_reason="",
        )


# ── Wirksame Projekt-Bindung: ein Projekt OHNE GitHub-Repo bindet nichts ──
#
# Rex' Befund C: `repo_bound = resolved_repo_id is not None or project_id is
# not None` zaehlte JEDE Projekt-Erbung als Bindung — auch bei Projekten ohne
# GitHub-Repo. Dort landet die Arbeit aber genau im gemeinsamen Ad-hoc-Klon,
# also im Fall, den der Waechter verhindern soll.


async def _board_project_without_repo(board_id) -> "Project":
    from app.models.board import Project

    project = Project(
        id=uuid.uuid4(),
        board_id=board_id,
        name=f"Proj-ohne-Repo-{uuid.uuid4().hex[:5]}",
    )
    await _mk([project])
    return project


@pytest.mark.asyncio
async def test_delegate_into_project_without_github_repo_is_rejected(client, fake_redis):
    """Delegation erbt ein Projekt OHNE GitHub-Repo und nennt eine Fundstelle
    → 422. Ohne den Fix gilt die blosse project_id als Bindung und der Worker
    landet im Ad-hoc-Klon."""
    from app.models.board import Project  # noqa: F401 — Modell registrieren

    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    project = await _board_project_without_repo(board.id)
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="developer")

    parent = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        project_id=project.id,
        title="Parent im Projekt ohne Repo",
        status="in_progress",
        assigned_agent_id=lead.id,
    )
    await _mk([parent])
    lead.current_task_id = parent.id
    await _mk([lead])

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Fix mit Fundstelle",
                "description": "Der Fehler sitzt in backend/app/routers/agent_scoped.py:1289.",
                "assigned_agent_id": str(worker.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 422, resp.text
    assert "agent_scoped.py:1289" in resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        leftovers = (await s.exec(select(Task).where(Task.board_id == board.id))).all()
    assert [t.id for t in leftovers] == [parent.id], (
        "abgelehnte Delegation darf keine Karte hinterlassen"
    )


@pytest.mark.asyncio
async def test_agent_create_task_into_project_without_github_repo_is_rejected(
    client, fake_redis
):
    """Derselbe Fall am zweiten Anlege-Pfad (POST /tasks) — sonst waere die
    Regel ueber den anderen Endpunkt umgehbar."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    project = await _board_project_without_repo(board.id)
    agent, token = await _agent(board.id, is_board_lead=True)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks",
        json={
            "title": "Karte mit Fundstelle",
            "project_id": str(project.id),
            "description": "Siehe frontend-v2/src/components/chat/Composer.tsx:610.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    assert "Composer.tsx:610" in resp.text


@pytest.mark.asyncio
async def test_waiver_is_recorded_even_when_the_project_has_no_repo(client, fake_redis):
    """Die Gegenprobe zur Ablehnung: eine Projekt-Bindung ohne Repo ist keine
    Bindung — also muss `--no-repo-reason` hier eine dokumentierte Ausnahme
    ergeben. Sonst waere der Ausweg genau dort unmoeglich, wo er gebraucht
    wird, und der Kommentar-Check (`not resolved_repo_id and project_id is
    None`) liefe der Ablehnung hinterher."""
    from app.models.task import TaskComment

    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    project = await _board_project_without_repo(board.id)
    lead, lead_token = await _agent(board.id, is_board_lead=True)
    worker, _ = await _agent(board.id, role="researcher")

    parent = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        project_id=project.id,
        title="Parent im Projekt ohne Repo",
        status="in_progress",
        assigned_agent_id=lead.id,
    )
    await _mk([parent])
    lead.current_task_id = parent.id
    await _mk([lead])

    p1, p2, p3 = _delegate_patches()
    with p1, p2, p3:
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Doku zur Fundstelle",
                "description": "Beschreibe backend/app/routers/agent_scoped.py:1289 im Handbuch.",
                "assigned_agent_id": str(worker.id),
                "no_repo_reason": "Reine Doku, kein Code im Repo",
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text
    subtask_id = uuid.UUID(resp.json()["subtask_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (
            await s.exec(select(TaskComment).where(TaskComment.task_id == subtask_id))
        ).all()
    waiver = [c for c in comments if c.comment_type == "repo_binding_waiver"]
    assert len(waiver) == 1, (
        "eine Projekt-Bindung ohne GitHub-Repo ist keine Bindung — die "
        "Ausnahme muss also dokumentiert sein"
    )
