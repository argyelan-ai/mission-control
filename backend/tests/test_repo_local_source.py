"""A local repo is a normal registry entry with its own source adapter.

The practice repo of the first run lives on this computer, not on GitHub
(``source="local"``, a ``file://`` URL). The registry API must not pretend
otherwise: no "open on GitHub" web link, and Sync reads the branch from the
local repository instead of asking GitHub.
"""
from __future__ import annotations

import subprocess

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.repo import Repo
from tests.conftest import test_engine


def _bare_repo(tmp_path, branch: str = "trunk") -> str:
    work = tmp_path / "work"
    work.mkdir()
    run = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("init", "-b", branch)
    (work / "README.md").write_text("x\n")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "init")
    bare = tmp_path / "demo.git"
    run("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return f"file://{tmp_path}/demo"


async def _add(**kw) -> Repo:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        repo = Repo(**kw)
        s.add(repo)
        await s.commit()
        await s.refresh(repo)
        return repo


@pytest.mark.asyncio
async def test_web_url_only_for_hosted_repos(auth_client: AsyncClient, tmp_path):
    await _add(full_name="local/demo", url=f"file://{tmp_path}/demo", source="local")
    await _add(full_name="octo/hello", url="https://github.com/octo/hello", source="imported")
    rows = {r["full_name"]: r for r in (await auth_client.get("/api/v1/repos")).json()}
    assert rows["local/demo"]["web_url"] is None
    assert rows["octo/hello"]["web_url"] == "https://github.com/octo/hello"


@pytest.mark.asyncio
async def test_sync_of_a_local_repo_reads_the_local_branch(auth_client: AsyncClient, tmp_path):
    url = _bare_repo(tmp_path, branch="trunk")
    repo = await _add(full_name="local/demo", url=url, source="local", default_branch="main")
    resp = await auth_client.post(f"/api/v1/repos/{repo.id}/sync")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_branch"] == "trunk"
    assert body["last_synced_at"]


@pytest.mark.asyncio
async def test_sync_of_a_missing_local_repo_says_so(auth_client: AsyncClient, tmp_path):
    repo = await _add(full_name="local/gone", url=f"file://{tmp_path}/gone", source="local")
    resp = await auth_client.post(f"/api/v1/repos/{repo.id}/sync")
    assert resp.status_code == 502
    assert "gone" in resp.json()["detail"]
