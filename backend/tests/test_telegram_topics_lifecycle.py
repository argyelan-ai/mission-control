"""Themen-Lebenszyklus P3.1 — Projekt-Thema, ✓-Umbenennen, 30-Tage-Purge.

Marks Entscheidungen (verbindlich):
  * Ad-hoc-Task  -> eigenes Thema
  * Projekt      -> EIN Thema fuer alle seine Tasks
  * Subtask      -> kein eigenes Thema (nutzt das des Elterntasks/Projekts)
  * Erledigt     -> Thema auf `✓ …` umbenennen, NICHT schliessen
                    (closeForumTopic funktioniert im Privatchat nachweislich nicht)
  * Nach 30 Tagen loeschen; das Allgemein-Thema (DM) nie.

Alle Tests fahren ohne Netz: der Telegram-Client wird injiziert und gefaelscht.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board, Project
from app.models.task import Task
from app.models.thread import Thread
from app.services.telegram_topics import (
    GENERAL_TOPIC_ID,
    ensure_topic_for_thread,
    handle_task_done,
    purge_old_topics,
)


class FakeForumClient:
    """Zeichnet Aufrufe auf statt zu senden — ersetzt das Netz."""

    def __init__(self, *, next_id: int = 100, delete_raises: Exception | None = None):
        self.created: list[str] = []
        self.edited: list[tuple[int, str]] = []
        self.deleted: list[int] = []
        self._next_id = next_id
        self._delete_raises = delete_raises

    async def create_forum_topic(self, name: str) -> int:
        self.created.append(name)
        tid = self._next_id
        self._next_id += 1
        return tid

    async def edit_forum_topic(self, message_thread_id: int, name: str) -> None:
        self.edited.append((message_thread_id, name))

    async def delete_forum_topic(self, message_thread_id: int) -> None:
        if self._delete_raises is not None:
            raise self._delete_raises
        self.deleted.append(message_thread_id)


# ── Fixtures/Helfer ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def team_chat_on(monkeypatch):
    """Der Team-Chat ist in der Test-Config aus; diese Suite prueft ihn
    eingeschaltet. Die Aus-Tests schalten ihn selbst wieder ab."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True)

async def _board(session: AsyncSession) -> Board:
    board = Board(name="MC Dev", slug=f"mc-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def _project(session: AsyncSession, board: Board, name: str = "Landing Page") -> Project:
    project = Project(board_id=board.id, name=name)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _task_with_thread(
    session: AsyncSession,
    board: Board,
    title: str,
    *,
    project: Project | None = None,
    parent: Task | None = None,
) -> tuple[Task, Thread]:
    task = Task(
        board_id=board.id,
        title=title,
        project_id=project.id if project is not None else None,
        parent_task_id=parent.id if parent is not None else None,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    thread = Thread(kind="task", task_id=task.id, project_id=task.project_id)
    session.add(thread)
    await session.commit()
    await session.refresh(thread)

    task.thread_id = thread.id
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task, thread


# ── 1) Projekt = EIN Thema ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_tasks_of_the_same_project_share_one_topic(async_session: AsyncSession):
    board = await _board(async_session)
    project = await _project(async_session, board, "Landing Page")
    _t1, thread1 = await _task_with_thread(async_session, board, "Hero", project=project)
    _t2, thread2 = await _task_with_thread(async_session, board, "Footer", project=project)
    client = FakeForumClient(next_id=900)

    first = await ensure_topic_for_thread(async_session, thread1, client)
    second = await ensure_topic_for_thread(async_session, thread2, client)

    assert first == second == 900
    assert client.created == ["Landing Page"], "genau EIN Thema, benannt nach dem Projekt"
    await async_session.refresh(project)
    assert project.telegram_topic_id == 900


@pytest.mark.asyncio
async def test_project_topic_is_not_written_onto_the_task_threads(async_session: AsyncSession):
    """threads.telegram_topic_id ist unique — ein geteiltes Thema darf dort NICHT
    landen, sonst kollidiert der zweite Task an der DB."""
    board = await _board(async_session)
    project = await _project(async_session, board)
    _t1, thread1 = await _task_with_thread(async_session, board, "A", project=project)
    _t2, thread2 = await _task_with_thread(async_session, board, "B", project=project)
    client = FakeForumClient()

    await ensure_topic_for_thread(async_session, thread1, client)
    await ensure_topic_for_thread(async_session, thread2, client)

    await async_session.refresh(thread1)
    await async_session.refresh(thread2)
    assert thread1.telegram_topic_id is None
    assert thread2.telegram_topic_id is None


@pytest.mark.asyncio
async def test_subtask_uses_the_parent_topic(async_session: AsyncSession):
    board = await _board(async_session)
    parent, parent_thread = await _task_with_thread(async_session, board, "Eltern")
    _child, child_thread = await _task_with_thread(
        async_session, board, "Kind", parent=parent
    )
    client = FakeForumClient(next_id=310)

    parent_topic = await ensure_topic_for_thread(async_session, parent_thread, client)
    child_topic = await ensure_topic_for_thread(async_session, child_thread, client)

    assert child_topic == parent_topic == 310
    assert len(client.created) == 1, "ein Subtask legt kein eigenes Thema an"
    await async_session.refresh(child_thread)
    assert child_thread.telegram_topic_id is None


@pytest.mark.asyncio
async def test_subtask_of_a_project_task_lands_on_the_project_topic(async_session: AsyncSession):
    board = await _board(async_session)
    project = await _project(async_session, board, "Shop")
    parent, _pt = await _task_with_thread(async_session, board, "Eltern", project=project)
    _child, child_thread = await _task_with_thread(
        async_session, board, "Kind", parent=parent
    )
    client = FakeForumClient(next_id=700)

    topic = await ensure_topic_for_thread(async_session, child_thread, client)

    assert topic == 700
    assert client.created == ["Shop"]


@pytest.mark.asyncio
async def test_adhoc_task_still_gets_its_own_topic(async_session: AsyncSession):
    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Recherche")
    client = FakeForumClient(next_id=42)

    topic = await ensure_topic_for_thread(async_session, thread, client)

    assert topic == 42
    await async_session.refresh(thread)
    assert thread.telegram_topic_id == 42
    assert client.created[0].endswith("Recherche")


@pytest.mark.asyncio
async def test_project_topic_id_is_unique_in_the_database(async_session: AsyncSession):
    """Constraint im Modell UND in der Migration — sonst prueft dieser Test eine
    Tabelle ohne den Constraint und beweist nichts."""
    board = await _board(async_session)
    a = await _project(async_session, board, "A")
    b = await _project(async_session, board, "B")

    a.telegram_topic_id = 8080
    async_session.add(a)
    await async_session.commit()

    b.telegram_topic_id = 8080
    async_session.add(b)
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()


@pytest.mark.asyncio
async def test_inbound_finds_the_thread_behind_a_project_topic(async_session: AsyncSession):
    """Eingehend: ein Projekt-Thema haengt an keinem Thread (die ID sitzt am
    Projekt). Ohne Aufloesung ueber das Projekt bekaeme Mark auf jede Nachricht
    im Projekt-Thema „unbekanntes Thema" — der Rueckkanal waere tot."""
    from app.services.telegram_inbound import _thread_for_topic

    board = await _board(async_session)
    project = await _project(async_session, board, "Shop")
    _open_task, open_thread = await _task_with_thread(
        async_session, board, "Laeuft", project=project
    )
    _done_task, done_thread = await _task_with_thread(
        async_session, board, "Erledigt", project=project
    )
    client = FakeForumClient(next_id=4711)
    await ensure_topic_for_thread(async_session, open_thread, client)

    # Der JUENGERE Thread ist der erledigte — reine Neuheit wuerde also falsch
    # antworten. Der offene ist der Ansprechpartner.
    done_thread.closed_at = datetime.utcnow()
    async_session.add(done_thread)
    await async_session.commit()

    found = await _thread_for_topic(async_session, 4711)

    assert found is not None
    assert found.id == open_thread.id, "der offene Task-Thread des Projekts, nicht der erledigte"


@pytest.mark.asyncio
async def test_inbound_prefers_the_youngest_open_project_thread(async_session: AsyncSession):
    from app.services.telegram_inbound import _thread_for_topic

    board = await _board(async_session)
    project = await _project(async_session, board, "Shop")
    _t1, first = await _task_with_thread(async_session, board, "Erst", project=project)
    _t2, second = await _task_with_thread(async_session, board, "Zweit", project=project)
    await ensure_topic_for_thread(async_session, first, FakeForumClient(next_id=4712))

    # Beide offen -> die juengste Aufgabe ist die, an der gerade gearbeitet wird.
    first.created_at = datetime.utcnow() - timedelta(hours=2)
    async_session.add(first)
    await async_session.commit()

    found = await _thread_for_topic(async_session, 4712)

    assert found is not None and found.id == second.id


@pytest.mark.asyncio
async def test_inbound_still_finds_a_plain_task_topic(async_session: AsyncSession):
    from app.services.telegram_inbound import _thread_for_topic

    board = await _board(async_session)
    _t, thread = await _task_with_thread(async_session, board, "Ad-hoc")
    await ensure_topic_for_thread(async_session, thread, FakeForumClient(next_id=99))

    found = await _thread_for_topic(async_session, 99)

    assert found is not None and found.id == thread.id


# ── 2) ✓-Umbenennen bei done ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_renames_the_own_adhoc_topic(async_session: AsyncSession):
    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Recherche")
    client = FakeForumClient(next_id=55)
    await ensure_topic_for_thread(async_session, thread, client)

    await handle_task_done(async_session, task, client=client)

    assert len(client.edited) == 1
    topic_id, name = client.edited[0]
    assert topic_id == 55
    assert name.startswith("✓ ")


@pytest.mark.asyncio
async def test_done_never_renames_a_project_topic(async_session: AsyncSession):
    """KRITISCH: ein Projekt-Thema laeuft weiter, wenn ein einzelner Task fertig
    wird — es traegt alle Tasks des Projekts."""
    board = await _board(async_session)
    project = await _project(async_session, board, "Landing Page")
    task, thread = await _task_with_thread(async_session, board, "Hero", project=project)
    client = FakeForumClient(next_id=900)
    await ensure_topic_for_thread(async_session, thread, client)

    await handle_task_done(async_session, task, client=client)

    assert client.edited == [], "das Projekt-Thema darf nicht umbenannt werden"
    await async_session.refresh(project)
    assert project.telegram_topic_id == 900


@pytest.mark.asyncio
async def test_done_of_a_subtask_renames_nothing(async_session: AsyncSession):
    board = await _board(async_session)
    parent, parent_thread = await _task_with_thread(async_session, board, "Eltern")
    child, _ct = await _task_with_thread(async_session, board, "Kind", parent=parent)
    client = FakeForumClient(next_id=310)
    await ensure_topic_for_thread(async_session, parent_thread, client)

    await handle_task_done(async_session, child, client=client)

    assert client.edited == [], "ein Subtask benennt nichts um — das Thema gehoert dem Elterntask"


@pytest.mark.asyncio
async def test_done_closes_the_thread_so_the_purge_can_see_it(async_session: AsyncSession):
    """Ohne closed_at findet purge_old_topics nie etwas — der done-Uebergang ist
    der einzige Ort, der den Thread schliesst."""
    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Recherche")
    client = FakeForumClient()
    await ensure_topic_for_thread(async_session, thread, client)

    await handle_task_done(async_session, task, client=client)

    await async_session.refresh(thread)
    assert thread.closed_at is not None


@pytest.mark.asyncio
async def test_done_is_silent_when_the_flag_is_off(async_session: AsyncSession, monkeypatch):
    from app.config import settings

    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Recherche")
    client = FakeForumClient()
    await ensure_topic_for_thread(async_session, thread, client)
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", False)

    # Mit gefaelschtem Client: faellt der Flag-Guard weg, wuerde hier umbenannt —
    # ohne Client waere der Test blind (der echte Client scheitert am Netz und
    # bliebe unbemerkt gruen).
    await handle_task_done(async_session, task, client=client)

    assert client.edited == []
    await async_session.refresh(thread)
    assert thread.closed_at is None, "bei abgeschaltetem Team-Chat aendert sich nichts"


@pytest.mark.asyncio
async def test_done_survives_a_telegram_error(async_session: AsyncSession):
    """Ein Telegram-Fehler darf den Task-Abschluss nie kippen."""
    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Recherche")

    class Exploding(FakeForumClient):
        async def edit_forum_topic(self, message_thread_id: int, name: str) -> None:
            raise RuntimeError("boom")

    client = Exploding()
    await ensure_topic_for_thread(async_session, thread, client)

    await handle_task_done(async_session, task, client=client)  # darf nicht werfen


# ── 3) 30-Tage-Purge ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_deletes_old_project_topics_too(async_session: AsyncSession):
    board = await _board(async_session)
    project = await _project(async_session, board, "Alt")
    _t, thread = await _task_with_thread(async_session, board, "A", project=project)
    client = FakeForumClient(next_id=606)
    await ensure_topic_for_thread(async_session, thread, client)

    project.status = "done"
    project.completed_at = datetime.utcnow() - timedelta(days=45)
    async_session.add(project)
    await async_session.commit()

    purged = await purge_old_topics(async_session, client, older_than_days=30)

    assert purged == 1
    assert client.deleted == [606]
    await async_session.refresh(project)
    assert project.telegram_topic_id is None


@pytest.mark.asyncio
async def test_purge_spares_a_running_project(async_session: AsyncSession):
    board = await _board(async_session)
    project = await _project(async_session, board, "Laeuft")
    _t, thread = await _task_with_thread(async_session, board, "A", project=project)
    client = FakeForumClient()
    await ensure_topic_for_thread(async_session, thread, client)

    project.status = "active"
    project.completed_at = datetime.utcnow() - timedelta(days=400)
    async_session.add(project)
    await async_session.commit()

    assert await purge_old_topics(async_session, client, older_than_days=30) == 0
    assert client.deleted == []


@pytest.mark.asyncio
async def test_purge_never_touches_the_general_topic(async_session: AsyncSession):
    """Der DM-Thread ist das Allgemein-Thema (Sentinel 0) — nie loeschen."""
    dm = Thread(
        kind="dm",
        agent_id=uuid.uuid4(),
        title="DM Boss",
        telegram_topic_id=GENERAL_TOPIC_ID,
        closed_at=datetime.utcnow() - timedelta(days=999),
    )
    async_session.add(dm)
    await async_session.commit()
    client = FakeForumClient()

    purged = await purge_old_topics(async_session, client, older_than_days=30)

    assert purged == 0
    assert client.deleted == []


@pytest.mark.asyncio
async def test_purge_tick_is_a_noop_when_the_flag_is_off(monkeypatch):
    from app.config import settings
    from app.services import telegram_topics

    monkeypatch.setattr(settings, "telegram_team_chat_enabled", False)

    called = False

    async def _boom(*a, **kw):  # pragma: no cover — darf nie laufen
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(telegram_topics, "purge_old_topics", _boom)

    assert await telegram_topics.purge_topics_tick() == 0
    assert called is False


@pytest.mark.asyncio
async def test_purge_tick_swallows_errors(monkeypatch):
    """Der periodische Job darf nie sterben — ein Fehler wird geloggt, nicht geworfen."""
    from app.config import settings
    from app.services import telegram_topics

    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True)

    async def _boom(*a, **kw):
        raise RuntimeError("Telegram down")

    monkeypatch.setattr(telegram_topics, "purge_old_topics", _boom)

    assert await telegram_topics.purge_topics_tick() == 0


@pytest.mark.asyncio
async def test_purge_loop_calls_the_tick_and_stops_on_cancel(monkeypatch):
    """Der periodische Job ist verdrahtet: main.py's Loop ruft den Tick."""
    import asyncio

    from app import main as app_main

    monkeypatch.setattr(app_main, "TELEGRAM_TOPIC_PURGE_INTERVAL_SECONDS", 0)
    calls: list[int] = []

    async def _tick(older_than_days: int = 30) -> int:
        calls.append(older_than_days)
        await asyncio.sleep(0.01)
        return 0

    from app.services import telegram_topics

    monkeypatch.setattr(telegram_topics, "purge_topics_tick", _tick)

    task = asyncio.create_task(app_main._telegram_topic_purge_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    await task  # der Loop bricht bei CancelledError sauber ab, statt zu werfen

    assert calls, "der Loop muss den Purge-Tick aufrufen"
    assert calls[0] == 30, "Marks Regel: 30 Tage Aufbewahrung"


@pytest.mark.asyncio
async def test_system_finalize_done_renames_the_topic(async_session: AsyncSession, monkeypatch):
    """End-to-End durch den echten done-Uebergang (task_lifecycle), nicht nur
    gegen den Helfer: ohne die Verdrahtung bleibt mark_topic_done totes Kapital."""
    from app.services import telegram_topics
    from app.services.task_lifecycle import system_finalize_task_done

    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Recherche")
    client = FakeForumClient(next_id=161)
    await ensure_topic_for_thread(async_session, thread, client)

    seen: list = []
    real = telegram_topics.handle_task_done

    async def _spy(session, t, *, client=None):
        seen.append(t.id)
        await real(session, t, client=client)

    monkeypatch.setattr(telegram_topics, "handle_task_done", _spy)

    await system_finalize_task_done(
        async_session, task, board.id, old_status="review", reason="test"
    )

    assert seen == [task.id], "der done-Uebergang muss das Thema-Handling aufrufen"


@pytest.mark.asyncio
async def test_purge_error_leaves_the_id_for_a_later_run(async_session: AsyncSession):
    from app.services.telegram_topics import TelegramTopicError

    board = await _board(async_session)
    task, thread = await _task_with_thread(async_session, board, "Alt")
    ok_client = FakeForumClient(next_id=77)
    await ensure_topic_for_thread(async_session, thread, ok_client)
    thread.closed_at = datetime.utcnow() - timedelta(days=45)
    async_session.add(thread)
    await async_session.commit()

    failing = FakeForumClient(delete_raises=TelegramTopicError("nope"))
    purged = await purge_old_topics(async_session, failing, older_than_days=30)

    assert purged == 0
    await async_session.refresh(thread)
    assert thread.telegram_topic_id == 77, "die ID bleibt stehen, damit ein spaeterer Lauf es erneut versucht"
