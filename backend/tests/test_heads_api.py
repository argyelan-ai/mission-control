"""A14 — /api/v1/heads: start, restart, list, detail, log, run record, stop."""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.host import Host
from app.models.repo import Repo
from app.models.runtime import Runtime
from app.models.task import Task
from app.services import runtime_protocols as rp
from app.services.heads import engine
from tests.conftest import test_engine
from tests.heads_backend_helpers import heads_root, make_run, write_run_record  # noqa: F401

EP = "http://192.0.2.10:8000/v1"


@pytest.fixture(autouse=True)
def _probes(monkeypatch):
    rp.clear_cache()
    engine.clear_cache()
    state = {"served": frozenset({"glm"}), "anthropic": True}

    async def served(endpoint, now=None):
        return state["served"]

    async def running(endpoint):
        return None

    async def anth(endpoint):
        return state["anthropic"]

    monkeypatch.setattr(engine, "served_models", served)
    monkeypatch.setattr(engine, "running_requests", running)
    monkeypatch.setattr(rp, "probe_anthropic_route", anth)
    return state


async def _world(make_board, make_task, *, repo=True, status="inbox"):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        box = Host(slug=f"box-{uuid.uuid4().hex[:4]}", display_name="BOX", kind="ssh", ssh_host="192.0.2.10")
        s.add(box)
        await s.commit()
        await s.refresh(box)
        s.add(Runtime(slug="box-slot", display_name="Local slot", runtime_type="openai_compatible",
                      endpoint=EP, model_identifier="glm", host_id=box.id, is_slot=True))
        s.add(Runtime(slug="anthropic-claude-opus", display_name="Claude", runtime_type="cloud",
                      endpoint="https://api.anthropic.com/v1/messages", model_identifier="claude-opus"))
        r = Repo(full_name="owner/demo", url="https://github.com/owner/demo", default_branch="main")
        s.add(r)
        await s.commit()
        await s.refresh(r)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="Fix flaky retry test", description="Make it deterministic.",
                           status=status, repo_id=r.id if repo else None)
    return box, task


async def _task(task_id) -> Task:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, task_id)


async def test_disabled_is_404(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "heads_enabled", False)
    for path in ("/api/v1/heads", "/api/v1/heads/pairs", "/api/v1/heads/occupancy"):
        resp = await auth_client.get(path)
        assert resp.status_code == 404 and resp.json()["detail"]["code"] == "heads_disabled"
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(uuid.uuid4()), "harness": "omp",
                                                         "runtime_slug": "x"})
    assert resp.status_code == 404


async def test_start_writes_run_folder_spool_and_holds_the_task(auth_client, heads_root, make_board, make_task):
    box, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    folder = heads_root / run_id
    spec = json.loads((folder / "spec.json").read_text())
    assert spec["base_url"] == EP and spec["model"] == "glm"
    assert spec["box_keys"] == [str(box.id)]
    assert spec["repo_full_name"] == "owner/demo" and spec["branch"].startswith("mc-head/")
    from app.services.heads.launcher import SECRETISH

    assert not [k for k in spec if SECRETISH.search(k)]
    assert SECRETISH.search("api_key") and SECRETISH.search("GH_TOKEN") and not SECRETISH.search("box_keys")
    blob = json.dumps(spec)
    assert "ghp_" not in blob and "oauth" not in blob.lower()
    assert oct((folder / "head.env").stat().st_mode & 0o777) == "0o600"
    assert (folder / "head.env").read_text() == ""  # omp × local: keyless
    assert "Make it deterministic." in (folder / "job.md").read_text()
    proc = (folder / "procedure.md").read_text()
    assert "{{" not in proc and run_id in proc and str(folder / "wt") in proc
    spool = json.loads((heads_root / "spool" / f"{run_id}.start.json").read_text())
    assert spool == {"action": "start", "run_id": run_id}
    fresh = await _task(task.id)
    assert (fresh.status, fresh.run_control) == ("in_progress", "manual_hold")
    assert fresh.assigned_agent_id is None


async def test_held_head_task_is_invisible_to_dispatch_and_healers(auth_client, heads_root, make_board,
                                                                   make_task, fake_redis):
    from app.services.operations import check_dispatch_allowed

    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 201
    fresh = await _task(task.id)
    allowed, reason = await check_dispatch_allowed(fresh, None)
    assert allowed is False and "manual_hold" in reason

    from app.services.task_runner import TaskRunnerService

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
            patch("app.services.task_runner.emit_event", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await TaskRunnerService()._check_stuck_in_progress(s)
    after = await _task(task.id)
    assert (after.status, after.run_control) == ("in_progress", "manual_hold")


async def test_claude_local_gets_anthropic_env_pointing_at_the_engine(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "claude",
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 201, resp.text
    env = (heads_root / resp.json()["run_id"] / "head.env").read_text()
    assert "ANTHROPIC_BASE_URL='http://192.0.2.10:8000'" in env
    assert "ANTHROPIC_MODEL='glm'" in env
    assert "ANTHROPIC_API_KEY='local-engine-no-key'" in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "GH_TOKEN" not in env


async def test_repo_required(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task, repo=False)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "repo_required"


async def test_blocked_pair_is_422_with_reason_code(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "anthropic-claude-opus"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"code": "pair_blocked", "reason_code": "needs_operator_decision"}
    assert not list(heads_root.glob("*/spec.json"))


async def test_engine_not_ready(auth_client, heads_root, make_board, make_task, _probes):
    _probes["served"] = None
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "engine_not_ready"


async def test_head_active_and_box_busy(auth_client, heads_root, make_board, make_task):
    box, task = await _world(make_board, make_task)
    body = {"task_id": str(task.id), "harness": "omp", "runtime_slug": "box-slot"}
    assert (await auth_client.post("/api/v1/heads", json=body)).status_code == 201
    again = await auth_client.post("/api/v1/heads", json=body)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "head_active"
    board2 = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    other = await make_task(board2.id, title="Other", repo_id=(await _task(task.id)).repo_id)
    busy = await auth_client.post("/api/v1/heads", json={**body, "task_id": str(other.id)})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "box_busy"


async def test_pairs_endpoint_default_is_local(auth_client, heads_root, make_board, make_task):
    await _world(make_board, make_task)
    resp = await auth_client.get("/api/v1/heads/pairs")
    assert resp.status_code == 200
    d = resp.json()["default_pair"]
    assert (d["harness"], d["runtime_slug"], d["locality"]) == ("omp", "box-slot", "local")


async def test_list_detail_log_run_record_stop(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task)
    import time as _t

    now = _t.time()
    from tests.heads_backend_helpers import iso

    run_id = make_run(heads_root, task_id=str(task.id), status={
        "phase": "running", "started_at": iso(now - 60)}, heartbeat_age=3)
    (heads_root / run_id / "step.txt").write_text("step 4/7 sabotage probe\n")
    (heads_root / run_id / "head.log").write_text(
        "line one\nusing token ghp_abcdefghijklmnopqrstuvwxyz0123 now\nurl https://x/?api_key=supersecret\n"
    )
    write_run_record(heads_root, run_id, mtime=now - 10)
    sp = heads_root / run_id / ".wrapper" / "status.json"
    st = json.loads(sp.read_text())
    st["run_record_path"] = str(write_run_record(heads_root, run_id, mtime=now - 10))
    sp.write_text(json.dumps(st))

    lst = (await auth_client.get(f"/api/v1/heads?task_id={task.id}")).json()["runs"]
    assert [r["run_id"] for r in lst] == [run_id] and lst[0]["state"] == "running"
    assert (await auth_client.get("/api/v1/heads?active=false")).json()["runs"] == []
    detail = (await auth_client.get(f"/api/v1/heads/{run_id}")).json()
    assert detail["step"] == "step 4/7 sabotage probe" and detail["pr_url"] is None
    log = (await auth_client.get(f"/api/v1/heads/{run_id}/log?tail=5")).text
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in log and "supersecret" not in log
    assert "line one" in log
    rr = await auth_client.get(f"/api/v1/heads/{run_id}/run-record")
    assert rr.status_code == 200 and f"head_run: {run_id}" in rr.text
    occ = (await auth_client.get("/api/v1/heads/occupancy")).json()["boxes"]
    assert any(v["run_id"] == run_id for v in occ.values())
    stop = await auth_client.post(f"/api/v1/heads/{run_id}/stop")
    assert stop.status_code == 202
    assert json.loads((heads_root / "spool" / f"{run_id}.stop.json").read_text()) == {
        "action": "stop", "run_id": run_id}
    assert (await auth_client.get("/api/v1/heads/not-a-uuid")).status_code == 404


async def test_restart_with_another_pair_continue_mode(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})
    old = resp.json()["run_id"]
    old_folder = heads_root / old
    (old_folder / ".wrapper").mkdir(exist_ok=True)
    (old_folder / ".wrapper" / "status.json").write_text(json.dumps({"phase": "exited", "exit_code": 0}))
    (old_folder / "question.md").write_text("Keep the old endpoint?")
    r2 = await auth_client.post(f"/api/v1/heads/{old}/restart", json={
        "harness": "claude", "runtime_slug": "box-slot", "mode": "continue", "answer": "Deprecate it."})
    assert r2.status_code == 202, r2.text
    new = r2.json()["run_id"]
    spec_old = json.loads((old_folder / "spec.json").read_text())
    spec_new = json.loads((heads_root / new / "spec.json").read_text())
    assert spec_new["branch"] == spec_old["branch"]
    assert (spec_new["mode"], spec_new["restarted_from"], spec_new["harness"]) == ("continue", old, "claude")
    job = (heads_root / new / "job.md").read_text()
    assert "Keep the old endpoint?" in job and "Deprecate it." in job and "omp × box-slot" in job
    spool = json.loads((heads_root / "spool" / f"{new}.restart.json").read_text())
    assert spool == {"action": "restart", "run_id": new, "from_run_id": old}


async def test_viewer_cannot_start(client, heads_root, make_board, make_task):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"v-{uid.hex[:6]}@mc.local", name="V", role="viewer", is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), 'viewer')}"
    _, task = await _world(make_board, make_task)
    resp = await client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                    "runtime_slug": "box-slot"})
    assert resp.status_code == 403
    assert (await client.get("/api/v1/heads")).status_code == 200


# ── review fixes ────────────────────────────────────────────────────────


async def test_stop_on_a_finished_run_is_409_and_writes_nothing(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task)
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "exit_code": 0, "reason": None})
    resp = await auth_client.post(f"/api/v1/heads/{run_id}/stop")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "head_not_active"
    assert not (heads_root / run_id / ".backend" / "stop-requested").exists()
    assert not (heads_root / "spool" / f"{run_id}.stop.json").exists()


async def test_head_written_symlinks_are_never_followed(auth_client, heads_root, make_board, make_task, tmp_path):
    """question.md / step.txt / head.log are writable by the head: a symlink
    to a file the backend can see must not leak it into the API."""
    _, task = await _world(make_board, make_task)
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP-SECRET-VALUE\n")
    run_id = make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "exit_code": 0})
    folder = heads_root / run_id
    for name in ("question.md", "step.txt", "head.log"):
        os.symlink(secret, folder / name)
    detail = (await auth_client.get(f"/api/v1/heads/{run_id}")).json()
    assert detail["question"] is None and detail["step"] is None
    assert detail["state"] != "needs_you"
    log = (await auth_client.get(f"/api/v1/heads/{run_id}/log")).text
    assert "TOP-SECRET-VALUE" not in log
    # control: a regular file is read
    (folder / "question.md").unlink()
    (folder / "question.md").write_text("Which endpoint?")
    assert (await auth_client.get(f"/api/v1/heads/{run_id}")).json()["question"] == "Which endpoint?"


async def test_failed_spool_leaves_no_half_run_behind(auth_client, heads_root, make_board, make_task):
    from app.services.heads import launcher

    _, task = await _world(make_board, make_task)
    body = {"task_id": str(task.id), "harness": "omp", "runtime_slug": "box-slot"}
    with patch.object(launcher, "spool", side_effect=launcher.SpoolUnavailable("disk")):
        resp = await auth_client.post("/api/v1/heads", json=body)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "spool_unavailable"
    assert not [p for p in heads_root.iterdir() if (p / "spec.json").exists()]
    fresh = await _task(task.id)
    assert fresh.status == "inbox"
    again = await auth_client.post("/api/v1/heads", json=body)
    assert again.status_code == 201, again.text


async def test_a_start_in_flight_for_the_same_task_is_head_active(auth_client, heads_root, make_board, make_task):
    _, task = await _world(make_board, make_task)
    body = {"task_id": str(task.id), "harness": "omp", "runtime_slug": "box-slot"}
    locks = heads_root / "task-locks"
    locks.mkdir()
    (locks / str(task.id)).write_text("")
    busy = await auth_client.post("/api/v1/heads", json=body)
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "head_active"
    assert not [p for p in heads_root.iterdir() if (p / "spec.json").exists()]
    # a marker left over from a crashed request is taken over
    old = __import__("time").time() - 600
    os.utime(locks / str(task.id), (old, old))
    ok = await auth_client.post("/api/v1/heads", json=body)
    assert ok.status_code == 201, ok.text
    assert not (locks / str(task.id)).exists()


async def test_hidden_duplicate_runtime_row_says_so(auth_client, heads_root, make_board, make_task):
    box, task = await _world(make_board, make_task)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Runtime(slug="box-recipe", display_name="Recipe row", runtime_type="openai_compatible",
                      endpoint=EP, model_identifier="glm", host_id=box.id, is_slot=False))
        await s.commit()
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-recipe"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"code": "pair_blocked", "reason_code": "runtime_not_offered"}
    bad = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "kimi",
                                                        "runtime_slug": "box-slot"})
    assert bad.json()["detail"]["reason_code"] == "harness_not_supported"
