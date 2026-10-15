"""Scratch repos registered under Repos are prepared for heads automatically
(docs/specs/head-launcher.md §9).

Live finding 2026-10-04: the first head on a scratch repo outside MC ended
with a bare ``prepare_failed``; the operator listed the repo, built the bare
origin and the clone, and added the repos row by hand. Now the repos row
(``source="scratch"``, ``full_name="scratch/<name>"``, ``url`` = where the
code comes from) is enough: the backend lists the repo in
``heads/scratch-repos`` and hands the source to mc-head, which builds the
origin and clone on the host (the backend container cannot see host paths).
No source → 422 ``scratch_source_missing``, never a bare host failure.
"""
from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.repo import Repo
from app.models.runtime import Runtime
from app.models.task import Task
from app.services import runtime_protocols as rp
from app.services.heads import engine
from tests.conftest import test_engine
from tests.heads_backend_helpers import heads_root, make_run  # noqa: F401

EP = "http://192.0.2.10:8000/v1"
SCRATCH_MARK = "no pull request is possible"
SOURCE = "/srv/code/tool"


@pytest.fixture(autouse=True)
def _probes(monkeypatch):
    rp.clear_cache()
    engine.clear_cache()

    async def served(endpoint, now=None):
        return frozenset({"glm"})

    async def running(endpoint):
        return None

    async def anth(endpoint):
        return True

    monkeypatch.setattr(engine, "served_models", served)
    monkeypatch.setattr(engine, "running_requests", running)
    monkeypatch.setattr(rp, "probe_anthropic_route", anth)


async def _world(make_board, make_task, *, full_name="scratch/tool", url=SOURCE, source="scratch"):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        box = Host(slug=f"box-{uuid.uuid4().hex[:4]}", display_name="BOX", kind="ssh", ssh_host="192.0.2.10")
        s.add(box)
        await s.commit()
        await s.refresh(box)
        s.add(Runtime(slug="box-slot", display_name="Local slot", runtime_type="openai_compatible",
                      endpoint=EP, model_identifier="glm", host_id=box.id, is_slot=True))
        r = Repo(full_name=full_name, url=url, default_branch="main", source=source)
        s.add(r)
        await s.commit()
        await s.refresh(r)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Tidy the tool", description="Small cleanup.", status="inbox",
                           repo_id=r.id)
    return task


async def _start(auth_client, task):
    return await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})


def _listed(root) -> list[str]:
    try:
        return [ln for ln in (root / "scratch-repos").read_text().splitlines() if ln.strip()]
    except OSError:
        return []


def _bare(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)


# ── fresh scratch repo: listed + source handed to mc-head ───────────────


async def test_fresh_scratch_repo_is_listed_and_source_goes_on_the_spec(auth_client, heads_root, make_board,
                                                                       make_task):
    task = await _world(make_board, make_task)
    resp = await _start(auth_client, task)
    assert resp.status_code == 201, resp.text
    run = heads_root / resp.json()["run_id"]
    spec = json.loads((run / "spec.json").read_text())
    assert spec["scratch_source"] == SOURCE
    assert _listed(heads_root) == ["scratch/tool"]
    # mc-head will give it a local origin → the job must say "push only"
    assert SCRATCH_MARK in (run / "job.md").read_text()


async def test_listing_is_idempotent_and_keeps_other_entries(auth_client, heads_root, make_board, make_task):
    (heads_root / "scratch-repos").write_text("# operator list\nscratch/other\nscratch/tool\n")
    task = await _world(make_board, make_task)
    assert (await _start(auth_client, task)).status_code == 201
    assert (heads_root / "scratch-repos").read_text() == "# operator list\nscratch/other\nscratch/tool\n"


async def test_existing_clone_needs_no_source(auth_client, heads_root, make_board, make_task):
    """The hand-made live layout: origin + clone exist, url points at the origin."""
    origin = heads_root / "scratch-origin" / "tool.git"
    _bare(origin)
    clone = heads_root / "clones" / "scratch--tool"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", str(origin)], check=True)
    task = await _world(make_board, make_task, url=f"file://{origin}")
    resp = await _start(auth_client, task)
    assert resp.status_code == 201, resp.text
    run = heads_root / resp.json()["run_id"]
    assert json.loads((run / "spec.json").read_text())["scratch_source"] is None
    assert SCRATCH_MARK in (run / "job.md").read_text()


async def test_existing_origin_without_clone_needs_no_source(auth_client, heads_root, make_board, make_task):
    origin = heads_root / "scratch-origin" / "tool.git"
    _bare(origin)
    task = await _world(make_board, make_task, url=f"file://{origin}")
    resp = await _start(auth_client, task)
    assert resp.status_code == 201, resp.text
    run = heads_root / resp.json()["run_id"]
    assert json.loads((run / "spec.json").read_text())["scratch_source"] is None
    assert SCRATCH_MARK in (run / "job.md").read_text()
    assert _listed(heads_root) == ["scratch/tool"]


# ── no source → clear refusal, nothing written ──────────────────────────


@pytest.mark.parametrize("url", ["", "   ", "file://{heads}/scratch-origin/tool.git", "not a path", "ext::sh"])
async def test_no_usable_source_is_refused_with_a_clear_code(auth_client, heads_root, make_board, make_task, url):
    task = await _world(make_board, make_task, url=url.format(heads=heads_root))
    resp = await _start(auth_client, task)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {"code": "scratch_source_missing", "repo": "scratch/tool"}
    # no run folder, no listing, task untouched
    assert [p.name for p in heads_root.iterdir() if p.name not in ("task-locks",)] == []
    async with AsyncSession(test_engine) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "inbox" and fresh.run_control is None


async def test_restart_on_a_scratch_repo_without_source_is_refused_too(auth_client, heads_root, make_board,
                                                                      make_task):
    task = await _world(make_board, make_task, url="")
    old = make_run(heads_root, task_id=str(task.id), repo_full_name="scratch/tool",
                   status={"phase": "exited", "exit_code": 1, "reason": "prepare_failed"})
    resp = await auth_client.post(f"/api/v1/heads/{old}/restart",
                                  json={"harness": "omp", "runtime_slug": "box-slot", "mode": "fresh"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "scratch_source_missing"


# ── everything else is unchanged ────────────────────────────────────────


async def test_real_repo_gets_no_source_and_is_never_listed(auth_client, heads_root, make_board, make_task):
    task = await _world(make_board, make_task, full_name="owner/demo", url="https://github.com/owner/demo",
                        source="imported")
    resp = await _start(auth_client, task)
    assert resp.status_code == 201, resp.text
    spec = json.loads((heads_root / resp.json()["run_id"] / "spec.json").read_text())
    assert spec["scratch_source"] is None
    assert not (heads_root / "scratch-repos").exists()


async def test_scratch_source_outside_the_scratch_owner_is_not_auto_listed(auth_client, heads_root, make_board,
                                                                          make_task):
    """source=scratch but not scratch/<name>: today's manual path, untouched."""
    task = await _world(make_board, make_task, full_name="owner/tool")
    resp = await _start(auth_client, task)
    assert resp.status_code == 201, resp.text
    assert json.loads((heads_root / resp.json()["run_id"] / "spec.json").read_text())["scratch_source"] is None
    assert not (heads_root / "scratch-repos").exists()


# ── registering a scratch repo under Repos ──────────────────────────────


async def test_register_scratch_repo(auth_client):
    resp = await auth_client.post("/api/v1/repos/scratch", json={"name": "tool", "source": SOURCE})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["full_name"], body["url"], body["source"], body["default_branch"]) == (
        "scratch/tool", SOURCE, "scratch", "main")
    dup = await auth_client.post("/api/v1/repos/scratch", json={"name": "tool", "source": SOURCE})
    assert dup.status_code == 409
    # a bare repo's path keeps its ".git" (upsert_repo would strip it)
    bare = await auth_client.post("/api/v1/repos/scratch", json={"name": "bare", "source": "/srv/code/bare.git"})
    assert bare.status_code == 201 and bare.json()["url"] == "/srv/code/bare.git"


@pytest.mark.parametrize("body", [
    {"name": "tool", "source": "relative/path"},
    {"name": "tool", "source": "ext::sh -c x"},
    {"name": "tool", "source": "https://user:pw@example.invalid/x.git"},
    {"name": "../evil", "source": SOURCE},
    {"name": "-x", "source": SOURCE},
    {"name": "tool", "source": SOURCE, "default_branch": "-x"},
])
async def test_register_scratch_repo_validates(auth_client, body):
    resp = await auth_client.post("/api/v1/repos/scratch", json=body)
    assert resp.status_code == 422, resp.text


async def test_update_scratch_source(auth_client):
    created = (await auth_client.post("/api/v1/repos/scratch", json={"name": "tool", "source": SOURCE})).json()
    ok = await auth_client.patch(f"/api/v1/repos/{created['id']}", json={"url": "/srv/code/tool2"})
    assert ok.status_code == 200 and ok.json()["url"] == "/srv/code/tool2"
    bad = await auth_client.patch(f"/api/v1/repos/{created['id']}", json={"url": "ext::sh"})
    assert bad.status_code == 422


async def test_url_of_a_github_repo_is_not_editable(auth_client):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        r = Repo(full_name="owner/demo", url="https://github.com/owner/demo", source="imported")
        s.add(r)
        await s.commit()
        await s.refresh(r)
    resp = await auth_client.patch(f"/api/v1/repos/{r.id}", json={"url": "/srv/code/other"})
    assert resp.status_code == 422


def test_backend_and_host_accept_the_same_sources(tmp_path, monkeypatch):
    """One rule on both sides: the backend never writes a source mc-head
    would refuse as spec_invalid, and vice versa."""
    import importlib.machinery
    import importlib.util

    from app.services.heads import scratch
    from tests.heads_host_helpers import MC_HEAD

    monkeypatch.setenv("MC_HOME", str(tmp_path / "mc"))
    loader = importlib.machinery.SourceFileLoader("mc_head_parity_src", str(MC_HEAD))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("mc_head_parity_src", loader))
    loader.exec_module(mod)
    assert mod.SCRATCH_SOURCE_RE.pattern == scratch.SOURCE_RE.pattern
    for src in ["/a/b", "file:///a/b", "https://h/x.git", "ssh://git@h/x", "git@h:o/x.git",
                "relative", "-u", "ext::x", "/a/../b", "https://u:p@h/x", "/a b", ""]:
        assert scratch.source_allowed(src) == mod.scratch_source_allowed(src), src
