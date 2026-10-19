"""First run: after setup there is a real lead agent, a practice repo and a first job.

Onboarding wave 3, PR 3-1 (plan E9/E10/Z14). Before this, the wizard ended on a
demo board with four manual agents that could never work, and a first agent
took seven hand steps with the wrong (orchestrator-only) lead template.

``POST /api/v1/first-run/starter`` is the one call the setup wizard makes
(and that works today without it):

1. the lead agent from the built-in ``Lead`` template, bound to the runtime the
   operator chose (or the registry's ``lead`` runtime), provisioned like any
   agent created with one click;
2. a practice repo — created locally, registered as a normal entry in the repo
   registry (``source="local"``), cloned by the normal dispatch path;
3. a first job on that repo: "add a test, ask me before you commit".

No foreign seed: it refuses an install that already has boards or agents, and
creates nothing when the operator has no AI to run the agent on.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

URL = "/api/v1/first-run/starter"


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """MC's ~/.mc lives under a temp dir — never the real one."""
    from app.config import settings

    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path


@pytest.fixture
async def seeded(home):
    """A fresh install right after boot: shipped runtimes + built-in templates."""
    from app.services.runtime_seeder import seed_runtimes
    from app.services.template_seeder import seed_builtin_templates

    async with _session() as s:
        await seed_runtimes(s)
        await seed_builtin_templates(s)
    return home


@pytest.fixture
def side_effects():
    """Provisioning (host helper + container) and dispatch are background work;
    record what was scheduled instead of touching Docker or the host."""
    calls: dict[str, list] = {"provision": [], "dispatch": []}

    async def _fake_provision(agent_id, raw_token):
        calls["provision"].append((agent_id, raw_token))

    def _fake_tracked(coro):
        calls["dispatch"].append(coro)
        coro.close()

    with patch("app.routers.first_run.provision_new_agent", _fake_provision), patch(
        "app.services.task_create.create_tracked_task", _fake_tracked
    ):
        yield calls


async def _all(model):
    async with _session() as s:
        return list((await s.exec(select(model))).all())


def _git(*args: str, cwd: Path | str | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


# ── the happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_starter_creates_lead_agent_practice_repo_and_first_job(
    auth_client: AsyncClient, seeded, side_effects
):
    from app.models.agent import Agent
    from app.models.agent_template import AgentTemplate
    from app.models.repo import Repo
    from app.models.runtime import Runtime
    from app.models.task import Task

    resp = await auth_client.post(URL, json={"language": "de"})
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # 1. the agent: built-in Lead template, board lead, bound to the lead runtime
    agents = await _all(Agent)
    assert len(agents) == 1
    agent = agents[0]
    lead_tmpl = next(t for t in await _all(AgentTemplate) if t.name == "Lead" and t.is_builtin)
    assert agent.template_id == lead_tmpl.id
    assert agent.is_board_lead is True
    assert agent.agent_runtime == "cli-bridge"
    runtime = next(r for r in await _all(Runtime) if r.id == agent.runtime_id)
    assert "lead" in (runtime.role_tags or [])
    assert agent.harness == "claude"
    assert agent.operator_language == "de"
    assert agent.workspace_path == f"{seeded}/.mc/workspaces/lead"
    # provisioned like a one-click agent, with the token it was created with
    assert [a for a, _ in side_effects["provision"]] == [agent.id]
    assert side_effects["provision"][0][1]

    # 2. the practice repo: a normal registry entry, clonable by its URL
    repos = await _all(Repo)
    assert len(repos) == 1
    repo = repos[0]
    assert repo.full_name == "local/mc-practice"
    assert repo.source == "local"
    assert repo.is_active is True
    assert repo.default_branch == "main"
    from app.services.repo_registry import clone_url_for

    clone = seeded / "clone-check"
    _git("clone", clone_url_for(repo), str(clone))
    assert (clone / "practice" / "parse.py").is_file()
    assert (clone / "tests" / "test_slug.py").is_file()
    assert not (clone / "tests" / "test_parse.py").exists(), "the job is to add this test"
    assert _git("rev-list", "--count", "HEAD", cwd=clone) == "1"

    # 3. the first job: assigned, on the practice repo, asks before the commit
    tasks = await _all(Task)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.assigned_agent_id == agent.id
    assert task.repo_id == repo.id
    assert task.skip_review is True
    assert task.status == "inbox"
    assert "parse()" in task.title
    assert "mc question" in task.description
    assert "before you commit" in task.description.lower()
    assert len(side_effects["dispatch"]) == 1, "the job is dispatched like any assigned card"

    assert body["agent"]["id"] == str(agent.id)
    assert body["repo"]["id"] == str(repo.id)
    assert body["task"]["id"] == str(task.id)
    assert body["board"]["id"] == str(task.board_id)
    assert body["runtime"]["id"] == str(runtime.id)
    assert sorted(body["created"]) == ["agent", "board", "repo", "task"]


@pytest.mark.asyncio
async def test_practice_job_lands_in_a_real_clone_through_the_normal_dispatch_path(
    auth_client: AsyncClient, seeded, side_effects
):
    """No special case downstream: the cli-bridge workspace resolver clones the
    registry entry into the agent's workspace exactly like a GitHub repo."""
    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.cli_bridge_runner import _resolve_workspace

    assert (await auth_client.post(URL, json={})).status_code == 201
    async with _session() as s:
        task = (await s.exec(select(Task))).one()
        agent = (await s.exec(select(Agent))).one()
        workspace, worktree, has_repo = await _resolve_workspace(task, agent, s)

    assert has_repo is True
    assert worktree is not None
    assert Path(workspace, "practice", "parse.py").is_file()
    assert _git("branch", "--show-current", cwd=workspace).startswith("task/")


@pytest.mark.asyncio
async def test_starter_is_idempotent_for_a_retry(auth_client: AsyncClient, seeded, side_effects):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.repo import Repo
    from app.models.task import Task

    first = (await auth_client.post(URL, json={})).json()
    again = await auth_client.post(URL, json={})
    assert again.status_code == 200, again.text
    second = again.json()

    for part in ("board", "repo", "agent", "task"):
        assert second[part]["id"] == first[part]["id"]
    assert second["created"] == []
    assert [len(await _all(m)) for m in (Board, Repo, Agent, Task)] == [1, 1, 1, 1]
    assert len(side_effects["provision"]) == 1, "a retry never provisions twice"


@pytest.mark.asyncio
async def test_get_reports_the_parts_after_a_start(auth_client: AsyncClient, seeded, side_effects):
    missing = await auth_client.get(URL)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_started"

    started = (await auth_client.post(URL, json={})).json()
    state = await auth_client.get(URL)
    assert state.status_code == 200
    body = state.json()
    assert body["agent"]["id"] == started["agent"]["id"]
    assert body["agent"]["provision_status"]
    assert body["task"]["status"] == "inbox"
    assert body["repo"]["full_name"] == "local/mc-practice"


@pytest.mark.asyncio
async def test_explicit_runtime_choice_wins(auth_client: AsyncClient, seeded, side_effects):
    from app.models.agent import Agent
    from app.models.runtime import Runtime

    sonnet = next(r for r in await _all(Runtime) if r.slug == "anthropic-claude-sonnet")
    resp = await auth_client.post(URL, json={"runtime_id": str(sonnet.id)})
    assert resp.status_code == 201, resp.text
    (agent,) = await _all(Agent)
    assert agent.runtime_id == sonnet.id


# ── no foreign seed, no dead ends ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_refuses_an_install_that_already_has_a_board(
    auth_client: AsyncClient, seeded, side_effects
):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.repo import Repo

    async with _session() as s:
        s.add(Board(name="Mine", slug="mine"))
        await s.commit()

    resp = await auth_client.post(URL, json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_fresh"
    assert [b.slug for b in await _all(Board)] == ["mine"]
    assert await _all(Agent) == [] and await _all(Repo) == []
    assert not (seeded / ".mc" / "repos").exists()


@pytest.mark.asyncio
async def test_refuses_an_install_that_already_has_an_agent(
    auth_client: AsyncClient, seeded, side_effects
):
    from app.models.agent import Agent

    async with _session() as s:
        s.add(Agent(name="Existing", agent_token_hash=uuid.uuid4().hex))
        await s.commit()

    resp = await auth_client.post(URL, json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_fresh"


@pytest.mark.asyncio
async def test_without_an_ai_nothing_is_created(auth_client: AsyncClient, seeded, side_effects):
    """Honest dead-end guard: no runtime an agent can run on → one clear code,
    and no half-built board or repo left behind."""
    from app.models.board import Board
    from app.models.repo import Repo
    from app.models.runtime import Runtime

    async with _session() as s:
        for r in (await s.exec(select(Runtime))).all():
            await s.delete(r)
        await s.commit()

    resp = await auth_client.post(URL, json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_brain"
    assert await _all(Board) == [] and await _all(Repo) == []


@pytest.mark.asyncio
async def test_unknown_runtime_is_a_404(auth_client: AsyncClient, seeded, side_effects):
    resp = await auth_client.post(URL, json={"runtime_id": str(uuid.uuid4())})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rejects_a_malformed_language(auth_client: AsyncClient, seeded, side_effects):
    resp = await auth_client.post(URL, json={"language": "de; rm -rf"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_only_an_admin_may_start_it(client: AsyncClient, seeded, side_effects):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with _session() as s:
        s.add(User(id=uid, email="op@mc.local", name="Op", role="operator", is_active=True))
        await s.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(uid), 'operator')}"}
    assert (await client.post(URL, json={}, headers=headers)).status_code == 403


# ── a throw-away stack: real migrations + boot seeders on Postgres ─────────


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_postgres_install_ends_with_agent_repo_and_job(home):
    """``alembic upgrade head`` on an EMPTY database, the boot seeders, then the
    first run — the same chain a new install goes through. Runs in its own
    database (created and dropped here); the lane's shared database is untouched.
    """
    import os
    import sys
    from urllib.parse import urlsplit, urlunsplit

    import asyncpg
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models.agent import Agent
    from app.models.repo import Repo
    from app.models.task import Task
    from app.services import first_run_starter
    from app.services.runtime_seeder import seed_runtimes
    from app.services.template_seeder import seed_builtin_templates

    backend = Path(__file__).resolve().parents[1]
    base = os.environ.get("MC_TEST_DATABASE_URL", "")
    admin_url = base.replace("postgresql+asyncpg://", "postgresql://", 1)
    fresh_name = f"test_first_run_{uuid.uuid4().hex[:8]}"
    fresh_url = urlunsplit(urlsplit(base)._replace(path=f"/{fresh_name}"))

    admin = await asyncpg.connect(admin_url)
    try:
        await admin.execute(f'CREATE DATABASE "{fresh_name}"')
    finally:
        await admin.close()
    try:
        done = subprocess.run(
            [str(Path(sys.executable).with_name("alembic")), "upgrade", "head"],
            cwd=backend, env={**os.environ, "DATABASE_URL": fresh_url},
            capture_output=True, text=True, timeout=600,
        )
        assert done.returncode == 0, done.stderr[-4000:]

        engine = create_async_engine(fresh_url)
        try:
            with patch("app.services.task_create.create_tracked_task", lambda coro: coro.close()):
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    await seed_runtimes(s)
                    await seed_builtin_templates(s)
                    assert (await s.exec(select(Agent))).all() == [], "fresh = no agent yet"
                    result = await first_run_starter.start(s, language="en")
                    agents = (await s.exec(select(Agent))).all()
                    repos = (await s.exec(select(Repo))).all()
                    tasks = (await s.exec(select(Task))).all()
        finally:
            await engine.dispose()

        assert [a.id for a in agents] == [result.agent.id]
        assert agents[0].is_board_lead and agents[0].runtime_id is not None
        assert [(r.full_name, r.source) for r in repos] == [("local/mc-practice", "local")]
        assert [(t.assigned_agent_id, t.repo_id) for t in tasks] == [(agents[0].id, repos[0].id)]
        assert result.new_agent_token, "a new agent comes with its token for provisioning"
    finally:
        admin = await asyncpg.connect(admin_url)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{fresh_name}" WITH (FORCE)')
        finally:
            await admin.close()
