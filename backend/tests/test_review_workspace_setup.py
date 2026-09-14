"""Review-Karten bekommen beim Dispatch einen vorbereiteten Arbeitsordner.

Incident 2026-09-12 (Task dd4bf92c): der allgemeine Workspace-Pfad
(setup_git_workspace_for_dispatch) laeuft bei JEDEM Dispatch derselben Task
— auch beim Review-Handoff — und legt dabei immer einen frischen Branch von
main an. Fuer den Reviewer bedeutet das: der Ordner ist zwar nicht leer,
aber ohne die PR-Aenderungen. Der Reviewer musste den PR-Stand von Hand
holen und war 81 Minuten blockiert, bevor das als Normalzustand erkannt
wurde.

Fix: bei `task.dispatch_intent == "review_handoff"` wird stattdessen der
PR-Stand ausgecheckt (`gh pr checkout`) und das Ziel-SHA als Kommentar auf
die Karte geschrieben. Schlaegt das fehl (Checkout-Fehler oder keine
PR-Nummer auffindbar), wird die Karte sichtbar geblockt statt still mit
leerem/veraltetem Ordner zu dispatchen.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board, Project
from app.models.repo import Repo
from app.models.task import Task, TaskComment

from tests.conftest import test_engine


async def _mk(objs: list):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in objs:
            s.add(o)
        await s.commit()
        for o in objs:
            await s.refresh(o)


def _board(**kw) -> Board:
    return Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
                 auto_dispatch_enabled=False, **kw)


def _repo(**kw) -> Repo:
    d = dict(full_name=f"acme/tool-{uuid.uuid4().hex[:5]}",
             url="https://github.com/acme/tool")
    d.update(kw)
    return Repo(**d)


def _reviewer(board) -> Agent:
    return Agent(
        id=uuid.uuid4(), name=f"Rex-{uuid.uuid4().hex[:6]}", role="reviewer",
        board_id=board.id, agent_runtime="cli-bridge", model="x",
        workspace_path="/tmp/reviewer-ws",
    )


async def _seed_review_task(*, with_pr_number: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (reviewer_id, task_id). Task has repo_id set + dispatch_intent
    review_handoff (as handle_review_handoff leaves it) and, unless
    with_pr_number=False, the explicit task.pr_number field (Option B,
    Boss decision 2026-09-13 — NOT the 'PR erstellt:' comment heuristic)."""
    board, repo = _board(), _repo(full_name="acme/mytool", url="https://github.com/acme/mytool")
    reviewer = _reviewer(board)
    await _mk([board, repo, reviewer])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            board_id=board.id, title="Fix the thing", status="review",
            repo_id=repo.id, assigned_agent_id=reviewer.id,
            dispatch_intent="review_handoff",
            pr_number=148 if with_pr_number else None,
            pr_url="https://github.com/acme/mytool/pull/148" if with_pr_number else None,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return reviewer.id, task.id


# ── Review-Karte bekommt den PR-Stand ──────────────────────────────────

@pytest.mark.asyncio
async def test_review_dispatch_checks_out_pr_head(fake_redis):
    """Review-Karte mit repo_id + PR-Kommentar: gh pr checkout statt
    frischer Branch-von-main."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    reviewer_id, task_id = await _seed_review_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reviewer = await s.get(Agent, reviewer_id)
        task = await s.get(Task, task_id)

        with patch(
            "app.services.git_service.git_service.prepare_review_checkout",
            new=AsyncMock(return_value=("/tmp/reviewer-ws/mytool", "abc123deadbeef")),
        ) as checkout, \
             patch(
            "app.services.git_service.git_service.setup_git_identity",
            new=AsyncMock(),
        ):
            ok = await setup_git_workspace_for_dispatch(task, reviewer, s)

    assert ok is True
    checkout.assert_awaited_once()
    args = checkout.await_args.args
    assert args[0] == "/tmp/reviewer-ws"
    assert args[1] == "https://github.com/acme/mytool.git"
    assert args[2] == "mytool"
    assert args[3] == 148  # PR-Nummer aus dem "PR erstellt:"-Kommentar

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
        assert fresh.workspace_path == "/tmp/reviewer-ws/mytool"
        assert fresh.status != "blocked"


# ── Ziel-SHA landet auf der Karte ───────────────────────────────────────

@pytest.mark.asyncio
async def test_review_dispatch_writes_target_sha_to_card(fake_redis):
    """Definition of Done: Ziel-SHA steht in der Karte, damit der Reviewer
    sein geprueftes SHA gegen den PR-Head belegen kann, ohne es selbst
    aufzuloesen."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    reviewer_id, task_id = await _seed_review_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reviewer = await s.get(Agent, reviewer_id)
        task = await s.get(Task, task_id)

        with patch(
            "app.services.git_service.git_service.prepare_review_checkout",
            new=AsyncMock(return_value=("/tmp/reviewer-ws/mytool", "cafef00d1234")),
        ), patch(
            "app.services.git_service.git_service.setup_git_identity",
            new=AsyncMock(),
        ):
            ok = await setup_git_workspace_for_dispatch(task, reviewer, s)

    assert ok is True
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all())
        sha_comments = [c for c in comments if "cafef00d1234" in c.content]
        assert len(sha_comments) == 1, "Ziel-SHA muss als Kommentar auf der Karte stehen"
        assert "Ziel-SHA" in sha_comments[0].content


# ── Checkout-Fehlschlag ist sichtbar, kein stiller leerer Ordner ───────

@pytest.mark.asyncio
async def test_review_dispatch_checkout_failure_is_visible_not_silent(fake_redis):
    """gh pr checkout schlaegt fehl (z.B. PR geschlossen, Netzwerk) —
    Karte muss sichtbar geblockt werden, NICHT still mit leerem/veraltetem
    Ordner dispatcht werden."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    reviewer_id, task_id = await _seed_review_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reviewer = await s.get(Agent, reviewer_id)
        task = await s.get(Task, task_id)

        with patch(
            "app.services.git_service.git_service.prepare_review_checkout",
            new=AsyncMock(side_effect=RuntimeError("gh: pull request #148 is closed")),
        ), patch(
            "app.services.task_lifecycle.apply_terminal_unassign",
            new=AsyncMock(),
        ):
            ok = await setup_git_workspace_for_dispatch(task, reviewer, s)

    assert ok is False

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
        assert fresh.status == "blocked"

        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all())
        blockers = [c for c in comments if c.comment_type == "blocker"]
        assert len(blockers) == 1
        assert "Review-Workspace nicht vorbereitet" in blockers[0].content
        assert "is closed" in blockers[0].content


@pytest.mark.asyncio
async def test_review_dispatch_missing_pr_number_is_visible_not_silent(fake_redis):
    """Bekannte Luecke: Registry-Repo-Tasks bekommen den 'PR erstellt:'-
    Kommentar heute nicht automatisch (siehe mc-ask an Boss, Task dd4bf92c).
    Fehlt die PR-Nummer, muss die Karte trotzdem sichtbar geblockt werden —
    kein Raten aus dem Titel, kein stiller leerer Ordner."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    reviewer_id, task_id = await _seed_review_task(with_pr_number=False)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reviewer = await s.get(Agent, reviewer_id)
        task = await s.get(Task, task_id)

        with patch(
            "app.services.git_service.git_service.prepare_review_checkout",
            new=AsyncMock(),
        ) as checkout, patch(
            "app.services.task_lifecycle.apply_terminal_unassign",
            new=AsyncMock(),
        ):
            ok = await setup_git_workspace_for_dispatch(task, reviewer, s)

    assert ok is False
    checkout.assert_not_awaited()  # kein Rate-Versuch, kein Checkout ohne PR-Nummer

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
        assert fresh.status == "blocked"

        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all())
        blockers = [c for c in comments if c.comment_type == "blocker"]
        assert len(blockers) == 1
        assert "Keine PR-Nummer" in blockers[0].content


# ── Regression: Entwickler-Karten verhalten sich unveraendert ──────────

@pytest.mark.asyncio
async def test_developer_dispatch_unaffected_by_review_branch(fake_redis):
    """dispatch_intent != review_handoff -> der neue Review-Zweig darf gar
    nicht erst betreten werden; der bestehende Registry-Repo-Pfad (Branch
    von main, kein PR-Checkout) laeuft unveraendert weiter."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    board, repo = _board(), _repo(full_name="acme/mytool", url="https://github.com/acme/mytool")
    agent = Agent(
        id=uuid.uuid4(), name="Worker", role="Developer", board_id=board.id,
        agent_runtime="cli-bridge", model="x", workspace_path="/tmp/dev-ws",
    )
    await _mk([board, repo, agent])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            board_id=board.id, title="Fix bug", status="inbox",
            repo_id=repo.id, assigned_agent_id=agent.id,
        )  # dispatch_intent default == "root"
        s.add(task)
        await s.commit()
        await s.refresh(task)

        with patch("app.services.dispatch.is_backend_writable_path", return_value=True), \
             patch("app.services.git_service.git_service.ensure_workspace",
                   new=AsyncMock(return_value="/tmp/dev-ws/mytool")) as ensure, \
             patch("app.services.git_service.git_service.create_task_worktree",
                   new=AsyncMock(return_value="/tmp/dev-ws/mytool-wt")), \
             patch("app.services.git_service.git_service.setup_git_identity",
                   new=AsyncMock()), \
             patch("app.services.git_service.git_service.prepare_review_checkout",
                   new=AsyncMock()) as checkout:
            ok = await setup_git_workspace_for_dispatch(task, agent, s)

    assert ok is True
    ensure.assert_awaited_once()  # normaler Entwickler-Pfad lief
    checkout.assert_not_awaited()  # Review-Zweig wurde gar nicht betreten
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.workspace_path == "/tmp/dev-ws/mytool-wt"


# ── Schreibpfad: handle_review_pr_creation setzt das Feld (project_id-Pfad) ─

@pytest.mark.asyncio
async def test_handle_review_pr_creation_persists_pr_number_field():
    """Boss-Entscheidung (Option B, Task dd4bf92c, 2026-09-13): der
    Backend-Pfad, der den PR selbst erstellt (project_id), muss die
    zuverlaessige `pr_number`/`pr_url`-Spalte fuellen — zusaetzlich zum
    unveraenderten `PR erstellt:`-Kommentar (Pitfall H), nicht statt ihm."""
    from app.routers.agent_git import handle_review_pr_creation

    board = _board()
    project = Project(
        id=uuid.uuid4(), board_id=board.id, name="AI Project",
        github_repo_url="https://github.com/test-owner/ai-project.git",
    )
    agent = Agent(
        id=uuid.uuid4(), name="Worker", role="developer",
        agent_runtime="cli-bridge", workspace_path="/tmp/dev-ws",
    )
    await _mk([board, project, agent])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            board_id=board.id, title="Fix the thing", status="in_progress",
            project_id=project.id, assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

        with patch("app.services.git_service.git_service._run_cmd", new=AsyncMock()), \
             patch("app.services.git_service.git_service.push_branch", new=AsyncMock()), \
             patch("app.services.git_service.git_service.create_pr",
                   new=AsyncMock(return_value="https://github.com/test-owner/ai-project/pull/271")):
            pr_url = await handle_review_pr_creation(s, task, agent)

    assert pr_url == "https://github.com/test-owner/ai-project/pull/271"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.pr_number == 271
        assert fresh.pr_url == "https://github.com/test-owner/ai-project/pull/271"

        # Der bestehende Kommentar-Contract bleibt unangetastet (Pitfall H).
        comments = list((await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all())
        marker = [c for c in comments if "PR erstellt:" in c.content]
        assert len(marker) == 1
