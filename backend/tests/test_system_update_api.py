"""Self-update API — /api/v1/system/update/* (ADR-094, update plan PR 4c).

The backend is the messenger between the Settings card and the cli-bridge's
update door on the host. These tests put a fake door behind an
``httpx.MockTransport`` and check what the API lets through:

* gates: admin only (403), ``MC_UPDATE_MANAGED=deploy`` → 409 before
  anything else, feature switch off → 404;
* step-up on ``start`` and ``rollback`` (403 ``step_up_required``; the
  ``LOCAL_AUTH_TOKEN`` admin is refused), consumed once;
* preflight: door checks + backend checks (due schedules red — plan
  sabotage probe (c) —, group rounds/loops noted, step-up availability),
  bridge unreachable or too old → a red check with the command for the machine;
* status: filtered (no paths, no unknown fields), mirrored into the history
  and Redis; bridge gone → last known run + the resume command;
* door refusals map to stable codes (409/422/423/429/401).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.auth import create_access_token, hash_password
from app.models.group import AgentGroup, GroupRound
from app.models.scheduled_job import ScheduledJob
from app.models.system_update import DRAIN_KEY, SystemState, SystemUpdate
from app.models.task import Task
from app.models.board import Board
from app.models.user import User
from app.services import docker_gateway, mc_update_runner, update_drain
from tests.conftest import test_engine

PASSWORD = "correct horse battery"
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000004c1")
OPERATOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000004c2")
RUN_ID = "20261019T120000Z-abc123"
TOKEN = "door-token-" + "x" * 40
LOCAL_TOKEN = "local-token-for-update-tests-0123456789"
_PW_HASH = hash_password(PASSWORD)


class FakeDoor:
    """The bridge's update door, as httpx sees it."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.reachable = True
        self.read_timeout = False
        self.ping = {"status": "ok", "api_version": 1}
        self.replies: dict[str, tuple[int, dict]] = {
            "/mc-update/preflight": (200, {
                "checks": [
                    {"id": "release", "status": "ok", "message": "v0.9.2 → v0.9.3"},
                    {"id": "disk", "status": "ok", "message": "enough space in /home/testuser/mc"},
                ],
                "current": "v0.9.2", "target": "v0.9.3", "up_to_date": False, "ok": True,
                "starts": {"used": 0, "limit": 3}, "api_version": 1,
            }),
            "/mc-update/start": (202, {"run_id": RUN_ID, "state": "started"}),
            "/mc-update/status": (200, _status_body()),
            "/mc-update/rollback": (202, {"run_id": RUN_ID, "state": "rolling_back"}),
            "/mc-update/resume": (202, {"resumed": True, "run_id": RUN_ID, "state": "resuming"}),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("refused", request=request)
        self.requests.append(request)
        if self.read_timeout:
            raise httpx.ReadTimeout("no answer", request=request)
        if request.url.path == "/ping":
            return httpx.Response(200, json=self.ping)
        if request.headers.get(mc_update_runner.TOKEN_HEADER) != TOKEN:
            return httpx.Response(401, json={"error": "unauthorized"})
        status, body = self.replies.get(request.url.path, (404, {"error": "unknown route"}))
        return httpx.Response(status, json=body)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def _status_body(**over) -> dict:
    body = {
        "run_id": RUN_ID, "phase": "pull", "phase_state": "intent", "status": "running",
        "source": "bridge",
        "from": {"tag": "v0.9.2", "version": "0.9.2"}, "target": {"tag": "v0.9.3", "version": "0.9.3"},
        "created_at": "2026-10-19T12:00:00Z", "updated_at": "2026-10-19T12:01:00Z", "finished_at": None,
        "error": None, "failure_kind": None, "backup": None, "interrupted": False,
        "lock": {"held": True, "pid": 4242, "pid_alive": True},
        "events": [{"at": "2026-10-19T12:00:30Z", "phase": "pull", "event": "intent",
                    "detail": "pulling into /Users/testuser/mission-control", "raw": "secret-ish"}],
        "out_file": "/home/testuser/.mc/update/bridge/start-x.out",
        "bridge": {"api_version": 1, "boot": {"state": "idle", "drain_sweep": {"lifted": []}},
                   "last_job": {"kind": "start", "exit": None, "out_file": "/home/testuser/x"},
                   "starts": {"used": 1, "limit": 3}},
    }
    body.update(over)
    return body


@pytest.fixture
def door(tmp_path, monkeypatch):
    fake = FakeDoor()
    token_file = tmp_path / "mc-update.token"
    token_file.write_text(TOKEN + "\n")
    monkeypatch.setattr(app.config.settings, "mc_update_token_file", str(token_file))
    monkeypatch.setattr(app.config.settings, "cli_bridge_token", "the-general-bridge-token-not-for-the-door")
    previous = mc_update_runner.set_door(
        mc_update_runner.Door(base_url="http://bridge.test:18792", transport=httpx.MockTransport(fake.handler)))
    yield fake
    mc_update_runner.set_door(previous)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(app.config.settings, "mc_self_update_enabled", True)
    monkeypatch.setattr(app.config.settings, "mc_update_managed", "")
    monkeypatch.setattr(app.config.settings, "app_version", "0.9.2")
    update_drain.reset_cache()
    yield
    update_drain.reset_cache()
    docker_gateway.reset_drain_cache()


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    async def latest(*a, **kw):
        return {"tag": "v0.9.3", "url": None}

    monkeypatch.setattr("app.services.update_check.get_latest_release", latest)


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    from app.routers import auth as auth_router

    auth_router._login_attempts.clear()
    yield
    auth_router._login_attempts.clear()


@pytest.fixture
async def users(client):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=ADMIN_ID, email="admin@update.test", name="Update Admin", role="admin",
                   is_active=True, password_hash=_PW_HASH))
        s.add(User(id=OPERATOR_ID, email="op@update.test", name="Op", role="operator",
                   is_active=True, password_hash=_PW_HASH))
        await s.commit()
    return client


def _bearer(user_id=ADMIN_ID, role="admin") -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id), role, 0)}"}


async def _step_up(client) -> dict:
    r = await client.post("/api/v1/auth/step-up", json={"password": PASSWORD}, headers=_bearer())
    assert r.status_code == 200, r.text
    return {**_bearer(), "X-MC-Step-Up": r.json()["step_up_token"]}


def _code(r) -> str | None:
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


ROUTES = [
    ("POST", "/api/v1/system/update/preflight", {}),
    ("POST", "/api/v1/system/update/start", {"expected_target": "v0.9.3"}),
    ("GET", "/api/v1/system/update/status", None),
    ("POST", "/api/v1/system/update/rollback", {}),
    ("POST", "/api/v1/system/update/resume", {}),
    ("GET", "/api/v1/system/update/history", None),
]


async def _call(client, method, path, body, headers):
    if method == "GET":
        return await client.get(path, headers=headers)
    return await client.post(path, json=body, headers=headers)


# ── gates ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_non_admin_gets_403(users, door, method, path, body):
    r = await _call(users, method, path, body, _bearer(OPERATOR_ID, "operator"))
    assert r.status_code == 403, r.text
    assert door.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_managed_install_answers_409_on_every_route(users, door, monkeypatch, method, path, body):
    """The operator's production install: MC_UPDATE_MANAGED=deploy → 409,
    even while the feature switch is still off, and the door is never asked."""
    monkeypatch.setattr(app.config.settings, "mc_update_managed", "deploy")
    monkeypatch.setattr(app.config.settings, "mc_self_update_enabled", False)
    r = await _call(users, method, path, body, _bearer())
    assert r.status_code == 409, r.text
    assert _code(r) == "update_managed"
    assert door.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_managed_409_tells_the_ui_whether_the_switch_is_on(users, door, monkeypatch, enabled):
    """The Settings card shows its "updated through the deploy procedure"
    note only once the feature switch is on (update plan PR 5: nothing new on
    screen before the acceptance) — the 409 says which of the two it is."""
    monkeypatch.setattr(app.config.settings, "mc_update_managed", "deploy")
    monkeypatch.setattr(app.config.settings, "mc_self_update_enabled", enabled)
    r = await users.get("/api/v1/system/update/status", headers=_bearer())
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["managed"] is True
    assert r.json()["detail"]["enabled"] is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_feature_switch_off_answers_404(users, door, monkeypatch, method, path, body):
    monkeypatch.setattr(app.config.settings, "mc_self_update_enabled", False)
    r = await _call(users, method, path, body, _bearer())
    assert r.status_code == 404, r.text
    assert _code(r) == "update_disabled"
    assert door.requests == []


def test_feature_switch_is_off_by_default():
    from app.config import Settings

    assert Settings.model_fields["mc_self_update_enabled"].default is False
    assert Settings.model_fields["mc_update_managed"].default == ""


# ── start ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_without_step_up_is_refused(users, door):
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=_bearer())
    assert r.status_code == 403
    assert _code(r) == "step_up_required"
    assert door.requests == []


@pytest.mark.asyncio
async def test_start_with_local_auth_token_is_refused(users, door, monkeypatch):
    monkeypatch.setattr(app.config.settings, "local_auth_token", LOCAL_TOKEN)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"},
                         headers={"Authorization": f"Bearer {LOCAL_TOKEN}", "X-MC-Step-Up": "whatever"})
    assert r.status_code == 403
    assert _code(r) == "step_up_local_token"
    assert door.requests == []


@pytest.mark.asyncio
async def test_start_with_step_up_starts_the_run_and_records_who(users, door):
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json() == {"run_id": RUN_ID, "state": "started", "target": "v0.9.3"}
    req = door.requests[-1]
    assert req.url.path == "/mc-update/start"
    assert json.loads(req.content) == {"expected_target": "v0.9.3"}
    # Own token scope: the door token, never the general bridge token.
    assert req.headers[mc_update_runner.TOKEN_HEADER] == TOKEN
    assert "X-MC-Bridge-Token" not in req.headers
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = (await s.exec(select(SystemUpdate).where(SystemUpdate.run_id == RUN_ID))).one()
    assert row.started_by_user_id == ADMIN_ID and row.started_by_name == "Update Admin"
    assert row.from_version == "0.9.2" and row.target_version == "v0.9.3" and row.source == "ui"

    # The step-up token was single-use.
    r2 = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r2.status_code == 403 and _code(r2) == "step_up_required"


@pytest.mark.asyncio
async def test_start_takes_only_a_release_tag(users, door):
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "main; rm -rf /"},
                         headers=headers)
    assert r.status_code == 400 and _code(r) == "update_bad_target"
    assert "/mc-update/start" not in door.paths()


@pytest.mark.asyncio
async def test_start_with_a_job_due_soon_changes_nothing(users, door):
    await _job_due_in(minutes=10)
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 422, r.text
    assert _code(r) == "preflight_failed"
    assert [c["id"] for c in r.json()["detail"]["checks"]] == ["schedules"]
    assert "/mc-update/start" not in door.paths()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,code,http", [
    (409, {"error": "the engine offers v0.9.4, not v0.9.3"}, "update_refused", 409),
    (409, {"error": "managed", "managed": True}, "update_managed", 409),
    (422, {"error": "a check failed — nothing was changed",
           "checks": [{"id": "linger", "status": "fail", "message": "run on the machine: sudo loginctl enable-linger"}]},
     "preflight_failed", 422),
    (423, {"error": "an update run is active on this machine"}, "update_busy", 423),
    (429, {"error": "3 update starts in the last 24 hours", "used": 3, "limit": 3}, "update_rate_limited", 429),
    (500, {"error": "the engine stopped with exit 1"}, "update_engine_error", 502),
])
async def test_door_refusals_map_to_stable_codes(users, door, status, body, code, http):
    door.replies["/mc-update/start"] = (status, body)
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == http, r.text
    assert _code(r) == code


@pytest.mark.asyncio
async def test_interrupted_run_answers_423_with_the_resume_command(users, door):
    door.replies["/mc-update/start"] = (423, {"error": "run x was interrupted in phase up — resume it first",
                                              "run_id": RUN_ID, "interrupted": True})
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 423
    d = r.json()["detail"]
    assert d["code"] == "update_busy" and d["interrupted"] is True and d["command"] == "./install.sh --resume"


@pytest.mark.asyncio
async def test_wrong_door_token_says_set_the_bridge_up_again(users, door, tmp_path, monkeypatch):
    bad = tmp_path / "other.token"
    bad.write_text("not-the-door-token-" + "y" * 30)
    monkeypatch.setattr(app.config.settings, "mc_update_token_file", str(bad))
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 502
    assert _code(r) == "door_token" and r.json()["detail"]["command"] == "./install.sh --bridge"


@pytest.mark.asyncio
async def test_start_with_bridge_gone_is_a_clear_503(users, door):
    door.reachable = False
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 503
    assert _code(r) == "bridge_unreachable" and r.json()["detail"]["command"] == "./install.sh --bridge"


# ── preflight ──────────────────────────────────────────────────────────────


async def _job_due_in(*, minutes: float, name: str = "Morning report") -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ScheduledJob(name=name, schedule_type="daily", schedule_time="07:00", action_type="create_task",
                           enabled=True, next_run_at=datetime.now(timezone.utc) + timedelta(minutes=minutes)))
        await s.commit()


async def _running_group(s: AsyncSession) -> AgentGroup:
    from app.models.agent import Agent
    from app.services import group_service

    members = []
    for name in ("Alpha", "Beta"):
        a = Agent(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:4]}", agent_runtime="cli-bridge", comm_v2=True)
        s.add(a)
        await s.commit()
        await s.refresh(a)
        members.append(a)
    group = await group_service.create_group(
        s, name="Room", goal="decide", member_ids=[a.id for a in members], lead_agent_id=members[0].id)
    group.status = "running"
    s.add(group)
    await s.commit()
    return group


def _by_id(checks: list[dict]) -> dict:
    return {c["id"]: c for c in checks}


@pytest.mark.asyncio
async def test_preflight_merges_door_and_backend_checks(users, door):
    r = await users.post("/api/v1/system/update/preflight", json={}, headers=_bearer())
    assert r.status_code == 200, r.text
    body = r.json()
    checks = _by_id(body["checks"])
    assert body["ok"] is True
    assert checks["release"]["status"] == "ok"
    assert checks["step_up"]["status"] == "ok"
    assert checks["schedules"]["status"] == "ok"
    # Paths from the machine never reach the UI.
    assert "/home/" not in json.dumps(body) and "/Users/" not in json.dumps(body)
    # The expected target is the release the UI offers (update_check), compared by the engine.
    assert json.loads(door.requests[-1].content) == {"expected_target": "v0.9.3"}
    assert body["target"] == "v0.9.3" and body["starts"] == {"used": 0, "limit": 3}
    assert body["running_work"] == {"tasks": 0, "group_rounds": 0}
    assert body["drain"]["active"] is False


@pytest.mark.asyncio
async def test_preflight_schedule_due_in_10_minutes_is_red(users, door):
    """Plan sabotage probe (c)."""
    await _job_due_in(minutes=10)
    await _job_due_in(minutes=40, name="Later")  # outside the window
    r = await users.post("/api/v1/system/update/preflight", json={}, headers=_bearer())
    body = r.json()
    sched = _by_id(body["checks"])["schedules"]
    assert sched["status"] == "fail" and "Morning report" in sched["message"] and "Later" not in sched["message"]
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_preflight_names_running_group_rounds_and_tasks(users, door):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        s.add(Task(board_id=board.id, title="busy", status="in_progress"))
        await s.commit()
        group = await _running_group(s)
        s.add(GroupRound(group_id=group.id, round_no=1))
        await s.commit()
    r = await users.post("/api/v1/system/update/preflight", json={}, headers=_bearer())
    body = r.json()
    assert body["running_work"] == {"tasks": 1, "group_rounds": 1}
    assert _by_id(body["checks"])["group_rounds"]["status"] == "warn"


@pytest.mark.asyncio
async def test_preflight_bridge_gone_is_a_red_check_with_the_command(users, door):
    door.reachable = False
    r = await users.post("/api/v1/system/update/preflight", json={}, headers=_bearer())
    assert r.status_code == 200
    bridge = _by_id(r.json()["checks"])["bridge"]
    assert bridge["status"] == "fail" and bridge["command"] == "./install.sh --bridge"
    assert r.json()["ok"] is False and r.json()["bridge"]["reachable"] is False


@pytest.mark.asyncio
async def test_preflight_old_bridge_without_the_door_is_red(users, door):
    door.ping = {"status": "ok"}  # a helper from before the update door
    r = await users.post("/api/v1/system/update/preflight", json={}, headers=_bearer())
    bridge = _by_id(r.json()["checks"])["bridge"]
    assert bridge["status"] == "fail" and bridge["command"] == "./install.sh --bridge"
    assert "/mc-update/preflight" not in door.paths()


@pytest.mark.asyncio
async def test_preflight_for_an_account_without_password_says_terminal(users, door):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        u = await s.get(User, ADMIN_ID)
        u.password_hash = None
        s.add(u)
        await s.commit()
    r = await users.post("/api/v1/system/update/preflight", json={}, headers=_bearer())
    step = _by_id(r.json()["checks"])["step_up"]
    assert step["status"] == "fail" and step["code"] == "step_up_no_password"
    assert r.json()["ok"] is False


# ── status ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_is_filtered_and_mirrored_into_history(users, door, fake_redis):
    r = await users.get("/api/v1/system/update/status", headers=_bearer())
    assert r.status_code == 200, r.text
    body = r.json()
    run = body["run"]
    assert run["run_id"] == RUN_ID and run["phase"] == "pull" and run["running"] is True
    assert run["from"] == {"tag": "v0.9.2", "version": "0.9.2"}
    dump = json.dumps(body)
    assert "out_file" not in dump and "raw" not in dump and "secret-ish" not in dump
    assert "/Users/" not in dump and "/home/" not in dump
    assert run["events"][0]["detail"] == "pulling into ~/mission-control"
    assert body["bridge_reachable"] is True
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = (await s.exec(select(SystemUpdate).where(SystemUpdate.run_id == RUN_ID))).one()
    assert row.phase == "pull" and row.source == "bridge" and row.target_version == "v0.9.3"
    assert json.loads(await fake_redis.get(mc_update_runner.MIRROR_KEY))["run_id"] == RUN_ID


@pytest.mark.asyncio
async def test_status_with_bridge_gone_shows_last_known_run_and_resume(users, door, fake_redis):
    assert (await users.get("/api/v1/system/update/status", headers=_bearer())).status_code == 200
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(SystemState(key=DRAIN_KEY, value={
            "run_id": RUN_ID, "set_by": "mc_update",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}))
        await s.commit()
    door.reachable = False
    r = await users.get("/api/v1/system/update/status", headers=_bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["bridge_reachable"] is False and body["stale"] is True
    assert body["run"]["run_id"] == RUN_ID
    assert body["drain"] == {"active": True, "run_id": RUN_ID, "expires_at": body["drain"]["expires_at"]}
    assert body["command"] == "./install.sh --resume"


@pytest.mark.asyncio
async def test_status_of_a_finished_run_records_the_result(users, door):
    door.replies["/mc-update/status"] = (200, _status_body(
        phase="rolled_back", phase_state="done", lock={"held": False}, finished_at="2026-10-19T12:20:00Z",
        error="health check failed in /home/testuser/mc", failure_kind="health",
        backup="mc_backup_preupdate_20261019T120500Z.sql.gz"))
    r = await users.get("/api/v1/system/update/status", headers=_bearer())
    assert r.json()["run"]["error"] == "health check failed in ~/mc"
    hist = await users.get("/api/v1/system/update/history", headers=_bearer())
    run = hist.json()["runs"][0]
    assert run["phase"] == "rolled_back" and run["failure_kind"] == "health"
    assert run["backup"] == "mc_backup_preupdate_20261019T120500Z.sql.gz"
    assert run["finished_at"].startswith("2026-10-19T12:20:00")


@pytest.mark.asyncio
async def test_status_interrupted_run_shows_the_resume_command(users, door):
    door.replies["/mc-update/status"] = (200, _status_body(interrupted=True, lock={"held": False}))
    r = await users.get("/api/v1/system/update/status", headers=_bearer())
    assert r.json()["command"] == "./install.sh --resume"


@pytest.mark.asyncio
async def test_status_notes_a_drain_the_bridge_lifted_in_the_history(users, door):
    door.replies["/mc-update/status"] = (200, _status_body(bridge={
        "api_version": 1, "starts": {"used": 0, "limit": 3},
        "boot": {"state": "idle", "drain_sweep": {"lifted": [{"run_id": "20261018T090000Z-def456"}]}}}))
    await users.get("/api/v1/system/update/status", headers=_bearer())
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = (await s.exec(select(SystemUpdate).where(SystemUpdate.run_id == "20261018T090000Z-def456"))).one()
    assert row.drain_lifted_at is not None


@pytest.mark.asyncio
async def test_status_rejects_a_malformed_run_id(users, door):
    r = await users.get("/api/v1/system/update/status?run=../../etc", headers=_bearer())
    assert r.status_code == 400 and _code(r) == "update_bad_run"
    assert "/mc-update/status" not in door.paths()


# ── rollback / resume ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/api/v1/system/update/start", {"expected_target": "v0.9.3"}),
    ("/api/v1/system/update/rollback", {"run_id": RUN_ID}),
])
async def test_bridge_gone_keeps_the_password_confirmation(users, door, path, body):
    """Nothing happened, so the operator need not type the password again:
    the step-up token stays spendable for the retry (within its 5 minutes)."""
    door.reachable = False
    headers = await _step_up(users)
    r = await users.post(path, json=body, headers=headers)
    assert r.status_code == 503 and _code(r) == "bridge_unreachable"
    door.reachable = True
    r = await users.post(path, json=body, headers=headers)
    assert r.status_code == 202, r.text
    # Spent now: still single-use once something was handed to the bridge.
    r = await users.post(path, json=body, headers=headers)
    assert r.status_code == 403 and _code(r) == "step_up_required"


@pytest.mark.asyncio
async def test_a_lost_answer_spends_the_confirmation(users, door):
    """The bridge got the request but the answer timed out: it may have
    started, so the confirmation stays spent (no silent second start)."""
    door.read_timeout = True
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 503
    door.read_timeout = False
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 403 and _code(r) == "step_up_required"


@pytest.mark.asyncio
async def test_a_kept_confirmation_keeps_its_expiry(users, door, fake_redis):
    """Handing the token back must not stretch its lifetime."""
    from app.services import step_up as step_up_mod

    door.reachable = False
    headers = await _step_up(users)
    key = step_up_mod._key(headers["X-MC-Step-Up"])
    await fake_redis.pexpire(key, 60_000)
    r = await users.post("/api/v1/system/update/start", json={"expected_target": "v0.9.3"}, headers=headers)
    assert r.status_code == 503
    assert 0 < await fake_redis.pttl(key) <= 60_000


@pytest.mark.asyncio
async def test_rollback_needs_step_up(users, door):
    r = await users.post("/api/v1/system/update/rollback", json={"run_id": RUN_ID}, headers=_bearer())
    assert r.status_code == 403 and _code(r) == "step_up_required"
    assert door.requests == []


@pytest.mark.asyncio
async def test_rollback_with_step_up_is_recorded(users, door):
    headers = await _step_up(users)
    r = await users.post("/api/v1/system/update/rollback", json={"run_id": RUN_ID}, headers=headers)
    assert r.status_code == 202, r.text
    assert json.loads(door.requests[-1].content) == {"run_id": RUN_ID}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = (await s.exec(select(SystemUpdate).where(SystemUpdate.run_id == RUN_ID))).one()
    assert row.rollback_requested_by_user_id == ADMIN_ID and row.rollback_requested_at is not None


@pytest.mark.asyncio
async def test_resume_with_bridge_gone_names_the_terminal_command(users, door):
    door.reachable = False
    r = await users.post("/api/v1/system/update/resume", json={}, headers=_bearer())
    assert r.status_code == 503
    assert r.json()["detail"]["command"] == "./install.sh --resume"


@pytest.mark.asyncio
async def test_resume_passes_through(users, door):
    r = await users.post("/api/v1/system/update/resume", json={"run_id": RUN_ID}, headers=_bearer())
    assert r.status_code == 202
    assert r.json() == {"resumed": True, "run_id": RUN_ID, "state": "resuming"}


# ── history reconcile at backend start ─────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_reconcile_records_how_the_run_ended(door, monkeypatch, fake_redis):
    """The update restarts the backend; at the next start the history learns
    the end of the run from the journal, even with no Settings page open."""
    monkeypatch.setattr("app.database.engine", test_engine)
    door.replies["/mc-update/status"] = (200, _status_body(
        phase="done", phase_state="done", lock={"held": False}, finished_at="2026-10-19T12:10:00Z",
        backup="mc_backup_preupdate_20261019T120500Z.sql.gz"))
    assert await mc_update_runner.reconcile_on_boot(delay=0) == "mirrored"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = (await s.exec(select(SystemUpdate).where(SystemUpdate.run_id == RUN_ID))).one()
    assert row.phase == "done" and row.finished_at is not None


@pytest.mark.asyncio
async def test_boot_reconcile_stays_quiet_when_switched_off_or_managed(door, monkeypatch):
    monkeypatch.setattr(app.config.settings, "mc_self_update_enabled", False)
    assert await mc_update_runner.reconcile_on_boot(delay=0) == "skipped"
    monkeypatch.setattr(app.config.settings, "mc_self_update_enabled", True)
    monkeypatch.setattr(app.config.settings, "mc_update_managed", "deploy")
    assert await mc_update_runner.reconcile_on_boot(delay=0) == "skipped"
    assert door.requests == []


@pytest.mark.asyncio
async def test_boot_reconcile_without_bridge_is_harmless(door):
    door.reachable = False
    assert await mc_update_runner.reconcile_on_boot(delay=0) == "bridge_unreachable"
