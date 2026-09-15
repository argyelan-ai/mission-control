"""ADR-052: agenten-seitige Repo-Bindung (Board Lead kann `repo_id` beim

Anlegen mitgeben, ohne die Operator-Route zu brauchen).

Deckt zwei Anlege-Pfade ab, die `mc delegate` bzw. der generische
agenten-seitige Create-Endpunkt tatsaechlich treffen:
  - POST /agent/boards/{board_id}/tasks           (AgentTaskCreate)
  - POST /agent/boards/{board_id}/delegate        (DelegateCreate, `mc delegate`)

Der wichtigste Test hier prueft NICHT nur den HTTP-Status, sondern liest
`repo_id` danach frisch aus der DB zurueck — genau dieses Feld wurde an
anderer Stelle schon einmal mit 200 quittiert und nicht gesetzt.

Die Sabotage-Tests am Ende gehen einen Schritt weiter als ein Feldwert-Check:
sie fahren echte lokale Git-Clones (file://-Remotes, kein Netzwerk) und lesen
danach `git remote -v` im vorbereiteten Workspace — das ist der Beweis, den
die Karte verlangt ("nicht am Feldwert allein").
"""

import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
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


def _board(**kw) -> Board:
    return Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}", **kw)


def _repo(**kw) -> Repo:
    d = dict(full_name=f"acme/tool-{uuid.uuid4().hex[:5]}", url="https://example.invalid/acme/tool")
    d.update(kw)
    return Repo(**d)


async def _agent_with_token(board_id, **kw) -> tuple[Agent, str]:
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


# ── AgentTaskCreate (POST /agent/boards/{board_id}/tasks) ────────────────


@pytest.mark.asyncio
async def test_agent_create_task_persists_repo_id_in_db(client, fake_redis):
    """Der wichtigste Test: repo_id muss nach dem Anlegen wirklich in der DB
    stehen — nicht nur ein 200er, der nichts geschrieben hat."""
    board = _board()
    repo = _repo()
    await _mk([board, repo])
    agent, token = await _agent_with_token(board.id, is_board_lead=True)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            json={"title": "Repo-gebundene Karte", "repo_id": str(repo.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text
    task_id = uuid.UUID(resp.json()["id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
    assert fresh.repo_id == repo.id


@pytest.mark.asyncio
async def test_agent_create_task_resolves_repo_by_slug(client, fake_redis):
    """--repo darf ein Name-Slug sein ('owner/name'), nicht nur die UUID."""
    board = _board()
    repo = _repo(full_name="acme/by-slug")
    await _mk([board, repo])
    agent, token = await _agent_with_token(board.id, is_board_lead=True)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            json={"title": "Slug-Bindung", "repo_id": "acme/by-slug"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text
    task_id = uuid.UUID(resp.json()["id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
    assert fresh.repo_id == repo.id


@pytest.mark.asyncio
async def test_agent_create_task_rejects_inactive_repo(client, fake_redis):
    board = _board()
    inactive = _repo(is_active=False)
    await _mk([board, inactive])
    agent, token = await _agent_with_token(board.id, is_board_lead=True)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks",
        json={"title": "x", "repo_id": str(inactive.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "repo_id" in resp.text

    # Kein Task-Fragment darf trotz Ablehnung entstanden sein.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(select(Task).where(Task.board_id == board.id))
        assert result.first() is None


@pytest.mark.asyncio
async def test_agent_create_task_rejects_unknown_repo(client, fake_redis):
    board = _board()
    await _mk([board])
    agent, token = await _agent_with_token(board.id, is_board_lead=True)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks",
        json={"title": "x", "repo_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_agent_create_task_without_repo_id_stays_none(client, fake_redis):
    """Sabotage-Gegenprobe (Feldwert-Ebene): ohne Angabe bleibt repo_id NULL,
    nicht irgendein stiller Default."""
    board = _board()
    await _mk([board])
    agent, token = await _agent_with_token(board.id, is_board_lead=True)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            json={"title": "Ohne Bindung"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text
    task_id = uuid.UUID(resp.json()["id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_id)
    assert fresh.repo_id is None


# ── DelegateCreate (POST /agent/boards/{board_id}/delegate, `mc delegate`) ─


@pytest.mark.asyncio
async def test_delegate_with_repo_persists_repo_id_in_db(client, fake_redis):
    board = _board()
    repo = _repo()
    await _mk([board, repo])
    lead, lead_token = await _agent_with_token(board.id, is_board_lead=True)
    worker, _ = await _agent_with_token(board.id, role="researcher")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock), \
         patch("app.services.operations.check_dispatch_allowed",
               new_callable=AsyncMock, return_value=(True, None)):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Delegierte Repo-Karte",
                "description": "Beschreibung lang genug fuer die Delegation-Guards hier.",
                "assigned_agent_id": str(worker.id),
                "repo_id": str(repo.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text
    subtask_id = uuid.UUID(resp.json()["subtask_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, subtask_id)
    assert fresh.repo_id == repo.id


@pytest.mark.asyncio
async def test_delegate_rejects_inactive_repo(client, fake_redis):
    board = _board()
    inactive = _repo(is_active=False)
    await _mk([board, inactive])
    lead, lead_token = await _agent_with_token(board.id, is_board_lead=True)
    worker, _ = await _agent_with_token(board.id, role="researcher")

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/delegate",
        json={
            "title": "x",
            "description": "Beschreibung lang genug fuer die Delegation-Guards hier.",
            "assigned_agent_id": str(worker.id),
            "repo_id": str(inactive.id),
        },
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 400
    assert "repo_id" in resp.text


@pytest.mark.asyncio
async def test_delegate_without_repo_stays_none(client, fake_redis):
    board = _board()
    await _mk([board])
    lead, lead_token = await _agent_with_token(board.id, is_board_lead=True)
    worker, _ = await _agent_with_token(board.id, role="researcher")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock), \
         patch("app.services.operations.check_dispatch_allowed",
               new_callable=AsyncMock, return_value=(True, None)):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/delegate",
            json={
                "title": "Ohne Bindung delegiert",
                "description": "Beschreibung lang genug fuer die Delegation-Guards hier.",
                "assigned_agent_id": str(worker.id),
            },
            headers={"Authorization": f"Bearer {lead_token}"},
        )
    assert resp.status_code == 201, resp.text
    subtask_id = uuid.UUID(resp.json()["subtask_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, subtask_id)
    assert fresh.repo_id is None


# ── Sabotage in beide Richtungen, nachgewiesen am echten Workspace-Remote ─
#
# Kein Mock von git_service.ensure_workspace/create_task_worktree — das
# sind echte `git`-Subprozesse gegen lokale file://-Bare-Repos (kein
# Netzwerk, kein GitHub). Einzige Mocks: der GitHub-Netzwerkaufruf im
# Ad-hoc-Scratch-Pfad (create_repo) und der Backend-Mount-Check.


def _init_bare_repo_with_commit(path: Path) -> str:
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as seed:
        subprocess.run(["git", "clone", str(path), seed], check=True, capture_output=True)
        (Path(seed) / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", seed, "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", seed, "-c", "user.email=t@t.test", "-c", "user.name=t",
             "commit", "-m", "seed"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", seed, "push", "origin", "main"], check=True, capture_output=True)
    return f"file://{path}"


def _remote_url_of(workspace_path: str) -> str:
    out = subprocess.run(
        ["git", "-C", workspace_path, "remote", "get-url", "origin"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


@pytest.mark.asyncio
async def test_with_flag_workspace_remote_is_registry_repo(tmp_path):
    """Mit Bindung: der vorbereitete Workspace zeigt am echten `git remote -v`
    auf das Registry-Repo — nicht auf den gemeinsamen Ad-hoc-Klon."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    registry_remote_dir = tmp_path / "registry-target.git"
    registry_remote_url = _init_bare_repo_with_commit(registry_remote_dir)

    board = _board()
    repo = _repo(full_name="acme/registry-target", url=registry_remote_url)
    agent = Agent(
        id=uuid.uuid4(), name="Worker", role="Developer", board_id=board.id,
        agent_runtime="cli-bridge", model="x",
        workspace_path=str(tmp_path / "agent-ws"),
    )
    task = Task(board_id=board.id, title="Repo-Bind Probe", status="inbox", repo_id=repo.id)
    await _mk([board, repo, agent, task])

    with patch("app.services.dispatch.is_backend_writable_path", return_value=True):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task.id)
            ok = await setup_git_workspace_for_dispatch(t, agent, s)

    assert ok is True
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.workspace_path is not None
    assert _remote_url_of(fresh.workspace_path) == registry_remote_url


@pytest.mark.asyncio
async def test_without_flag_workspace_remote_is_shared_adhoc_repo(tmp_path):
    """Ohne Bindung: dieselbe Karte landet im gemeinsamen Ad-hoc-Repo, nicht
    im Registry-Repo — auch das am echten `git remote -v` nachgewiesen."""
    from app.services.task_context_builder import setup_git_workspace_for_dispatch

    adhoc_remote_dir = tmp_path / "adhoc-scratch.git"
    adhoc_remote_url = _init_bare_repo_with_commit(adhoc_remote_dir)

    board = _board()
    agent = Agent(
        id=uuid.uuid4(), name="Worker2", role="Developer", board_id=board.id,
        agent_runtime="cli-bridge", model="x",
        workspace_path=str(tmp_path / "agent-ws-2"),
    )
    # Kein repo_id, kein project_id, kein board.default_project_id → Ad-hoc.
    task = Task(board_id=board.id, title="No-Flag Probe", status="inbox")
    await _mk([board, agent, task])

    with patch("app.services.dispatch.is_backend_writable_path", return_value=True), \
         patch("app.services.git_service.git_service.create_repo",
               new=AsyncMock(return_value=adhoc_remote_url)):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task.id)
            ok = await setup_git_workspace_for_dispatch(t, agent, s)

    assert ok is True
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.workspace_path is not None
    assert _remote_url_of(fresh.workspace_path) == adhoc_remote_url
