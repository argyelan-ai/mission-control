"""The update door of the cli-bridge (scripts/mc_update_door.py, ADR-094 decision 6) — PR 3.

The door is the only way the backend can reach the update engine on the
host: exactly five routes, its own token, 409 / 423 / 429, the engine started
detached from the bridge, and a start routine that finishes an interrupted
run after a reboot.

Every test drives the real door against the real engine in the same scratch
world as the engine tests (scripts/tests/test_mc_update.py: a local git
"upstream" with v0.1.0 and v0.2.0, a fake ``docker`` in PATH, ``file://``
release API and health). Nothing touches a real Docker, a real bridge
service or the network.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import mc_update_door as door_mod  # noqa: E402
from test_mc_update import World, _git  # noqa: E402

UTOKEN = "update-token-0123456789abcdef0123456789abcdef"
BTOKEN = "bridge-token-0123456789abcdef0123456789abcdef"


# ── fixtures + helpers ──────────────────────────────────────────────────────

@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path).build()


def _door(world: World, **kw) -> "door_mod.Door":
    d = door_mod.Door(repo=world.inst, state=world.state, token=kw.pop("token", UTOKEN),
                      env=kw.pop("env", world.env), start_wait=kw.pop("start_wait", 30.0),
                      sync_timeout=60.0, log=lambda m: None, **kw)
    d.detach = "fork"          # the CI runner may have systemd-run without a user manager
    d.system = lambda: "Darwin"
    d.docker_poll = 0.05
    return d


@pytest.fixture
def door(world: World):
    return _door(world)


def call(d, method: str, path: str, body=None, token: str = UTOKEN):
    headers = {door_mod.TOKEN_HEADER: token} if token is not None else {}
    raw = json.dumps(body).encode() if body is not None else b""
    return d.handle(method, path, headers, raw)


def wait_phase(world: World, phases, timeout: float = 40.0) -> dict:
    phases = (phases,) if isinstance(phases, str) else tuple(phases)
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        runs = sorted((world.state / "journal").glob("*.json")) if (world.state / "journal").exists() else []
        if runs:
            try:
                last = json.loads(runs[-1].read_text())
            except ValueError:
                last = None
            if last and last["phase"] in phases:
                return last
        time.sleep(0.05)
    raise AssertionError(f"run never reached {phases}; last journal: {last and last['phase']}")


def wait_lock_free(world: World, timeout: float = 40.0) -> None:
    eng = door_mod.engine_module()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if eng.RunLock(world.state / "lock").live_holder() is None:
            return
        time.sleep(0.05)
    raise AssertionError("the run never let go of the lock")


def fabricate_run(world: World, *, source="bridge", phase="done", kind=None, hours_ago=1.0) -> str:
    created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    run_id = created.strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()
    jd = world.state / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    iso = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    (jd / f"{run_id}.json").write_text(json.dumps({
        "schema": 1, "run_id": run_id, "source": source, "phase": phase, "phase_state": "done",
        "status": phase, "created_at": iso, "updated_at": iso, "finished_at": iso,
        "from": {"tag": "v0.0.9"}, "target": {"tag": "v0.1.0"}, "images": {}, "rollback": {},
        "drain": {}, "backup": {}, "engine": {}, "error": None, "failure_kind": kind, "events": []}))
    return run_id


def journals(world: World) -> list:
    d = world.state / "journal"
    return sorted(d.glob("*.json")) if d.exists() else []


# ── the door itself: routes, token, managed ─────────────────────────────────

def test_door_has_exactly_five_routes(door):
    assert set(door_mod.ROUTES) == {"/mc-update/preflight", "/mc-update/start", "/mc-update/status",
                                    "/mc-update/rollback", "/mc-update/resume"}
    assert call(door, "POST", "/mc-update/exec", {})[0] == 404
    assert call(door, "POST", "/mc-update/start/../../health", {})[0] == 404
    assert call(door, "GET", "/mc-update/start")[0] == 405
    assert call(door, "GET", "/mc-update/preflight")[0] == 405
    assert call(door, "POST", "/mc-update/status", {})[0] == 405


@pytest.mark.parametrize("token", [None, "", "wrong", BTOKEN])
def test_without_the_update_token_nothing_runs(world, door, token):
    for method, path in (("POST", "/mc-update/start"), ("POST", "/mc-update/preflight"),
                         ("GET", "/mc-update/status"), ("POST", "/mc-update/exec")):
        status, body = call(door, method, path, {"expected_target": "v0.2.0"} if method == "POST" else None,
                            token=token)
        assert status == 401, (path, body)
        assert body == {"error": "unauthorized"}
    assert world.calls() == []
    assert journals(world) == []


def test_no_token_loaded_fails_closed(world):
    d = _door(world, token=None)
    assert call(d, "GET", "/mc-update/status", token="")[0] == 401
    assert call(d, "GET", "/mc-update/status", token=UTOKEN)[0] == 401


def test_token_file_is_created_private_and_kept(tmp_path):
    f = tmp_path / "secrets" / "mc-update.token"
    t1 = door_mod.load_or_create_token(f)
    assert len(t1) >= 32 and oct(f.stat().st_mode & 0o777) == "0o600"
    assert door_mod.load_or_create_token(f) == t1
    f.write_text("short\n")
    with pytest.raises(door_mod.WeakTokenError):
        door_mod.load_or_create_token(f)


def test_managed_install_is_409_on_every_route(world, door):
    with open(world.inst / ".env", "a") as fh:
        fh.write("MC_UPDATE_MANAGED=deploy\n")
    for method, path, body in (("POST", "/mc-update/preflight", {}),
                               ("POST", "/mc-update/start", {"expected_target": "v0.2.0"}),
                               ("GET", "/mc-update/status", None),
                               ("POST", "/mc-update/rollback", {}),
                               ("POST", "/mc-update/resume", {})):
        status, out = call(door, method, path, body)
        assert status == 409, (path, out)
        assert out["managed"] is True
    assert world.calls() == []
    assert journals(world) == []


# ── preflight ───────────────────────────────────────────────────────────────

def test_preflight_returns_the_checks_and_changes_nothing(world, door):
    status, out = call(door, "POST", "/mc-update/preflight", {"expected_target": "v0.2.0"})
    assert status == 200, out
    ids = {c["id"] for c in out["checks"]}
    assert {"install", "release", "database", "bridge"} <= ids
    assert out["ok"] is True and out["target"] == "v0.2.0"
    assert out["api_version"] == door_mod.API_VERSION
    assert journals(world) == []
    assert world.head_tag() == "v0.1.0"


def test_preflight_with_another_target_is_409(world, door):
    status, out = call(door, "POST", "/mc-update/preflight", {"expected_target": "v0.3.0"})
    assert status == 409, out
    assert "v0.2.0" in out["error"]


def test_preflight_on_linux_without_linger_names_the_command(world, tmp_path):
    d = _door(world)
    d.system = lambda: "Linux"
    d.linger_dir = tmp_path / "linger"
    d.linger_dir.mkdir()
    status, out = call(d, "POST", "/mc-update/preflight", {})
    assert status == 200
    linger = next(c for c in out["checks"] if c["id"] == "linger")
    assert linger["status"] == "fail"
    assert "sudo loginctl enable-linger" in linger["message"]
    assert out["ok"] is False
    (d.linger_dir / door_mod.current_user()).touch()
    status, out = call(d, "POST", "/mc-update/preflight", {})
    assert next(c for c in out["checks"] if c["id"] == "linger")["status"] == "ok"
    assert out["ok"] is True


def test_start_on_linux_without_linger_is_refused(world, tmp_path):
    """Defence in depth: without linger a logout stops the user manager and
    kills the job mid-switch — start refuses, it does not rely on the UI
    having read preflight.ok."""
    d = _door(world)
    d.system = lambda: "Linux"
    d.linger_dir = tmp_path / "linger"
    d.linger_dir.mkdir()
    status, out = call(d, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 422, out
    assert [c["id"] for c in out["checks"]] == ["linger"]
    assert "sudo loginctl enable-linger" in out["checks"][0]["message"]
    assert journals(world) == []
    assert world.head_tag() == "v0.1.0"
    assert d.last_job is None


def test_macos_has_no_linger_check(door):
    status, out = call(door, "POST", "/mc-update/preflight", {})
    assert status == 200
    assert "linger" not in {c["id"] for c in out["checks"]}


# ── start ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [{}, {"expected_target": "latest"}, {"expected_target": "v0.2.0", "version": "v0.2.0"},
                                  {"expected_target": "v0.2.0", "ref": "main"}, ["v0.2.0"]])
def test_start_takes_only_the_expected_target(world, door, body):
    status, out = call(door, "POST", "/mc-update/start", body)
    assert status == 400, out
    assert journals(world) == []


def test_start_with_another_target_is_409_and_changes_nothing(world, door):
    """Sabotage (a): drop --expect from the engine call → this goes red."""
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.3.0"})
    assert status == 409, out
    assert journals(world) == []
    assert world.head_tag() == "v0.1.0"


def test_start_runs_detached_to_done(world, door):
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 202, out
    run_id = out["run_id"]
    j = wait_phase(world, ("done", "aborted", "rolled_back", "rollback_failed"))
    assert j["phase"] == "done", j.get("error")
    assert j["run_id"] == run_id and j["source"] == "bridge"
    wait_lock_free(world)
    assert world.head_tag() == "v0.2.0"
    out_file = world.state / "bridge" / f"start-{run_id}.out"
    assert oct(out_file.stat().st_mode & 0o777) == "0o600"
    assert (world.state / "bridge" / f"start-{run_id}.exit").read_text().strip() == "0"


def test_start_when_already_on_the_latest_release(world, door):
    assert world.run("start").returncode == 0
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 200, out
    assert out["up_to_date"] is True


def test_start_with_a_failing_check_is_422_and_says_why(world, door):
    world.patch_state(db_down=True)
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 422, out
    assert any(c["id"] == "database" and c["status"] == "fail" for c in out["checks"])
    assert journals(world) == []


def _live_run(world: World) -> subprocess.Popen:
    """A real run that waits for running work in the drain phase."""
    world.patch_state(in_progress=[1])
    env = dict(world.env, MC_UPDATE_DRAIN_TIMEOUT="60", MC_UPDATE_POLL_INTERVAL="0.05")
    p = subprocess.Popen([sys.executable, str(world.inst / "scripts" / "mc_update.py"), "start"],
                         cwd=world.inst, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_phase(world, "drain")
    return p


def test_second_start_while_a_run_is_live_is_423(world, door):
    p = _live_run(world)
    try:
        status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
        assert status == 423, out
        assert call(door, "POST", "/mc-update/resume", {})[0] == 423
        status, st = call(door, "GET", "/mc-update/status")
        assert status == 200 and st["lock"]["held"] is True
        # rollback on a live run before the switch = cancel, answered right away
        status, out = call(door, "POST", "/mc-update/rollback", {})
        assert status == 200, out
        assert out["cancel_requested"] is True
        assert p.wait(timeout=30) == 3
    finally:
        if p.poll() is None:
            p.kill()
    assert world.journal()["phase"] == "aborted"
    assert len(journals(world)) == 1


def test_two_starts_at_the_same_moment_only_one_runs(world, door):
    world.patch_state(pull_sleep=2)
    results = []

    def go():
        results.append(call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"}))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert sorted(s for s, _ in results) == [202, 423], results
    wait_phase(world, "done")
    wait_lock_free(world)
    assert len(journals(world)) == 1


def test_interrupted_run_blocks_a_new_start_with_423(world, door):
    assert world.run("start", fault="up.intent").returncode == 137
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 423, out
    assert "resume" in out["error"]


# ── rate limit: at most N starts per 24 h ───────────────────────────────────

def test_fourth_start_within_a_day_is_429(world, door):
    for _ in range(3):
        fabricate_run(world)
    n = len(journals(world))
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 429, out
    assert out["limit"] == 3 and out["used"] == 3
    assert len(journals(world)) == n
    assert world.calls() == []


def test_network_failures_terminal_runs_and_old_starts_do_not_count(world, door):
    fabricate_run(world, phase="aborted", kind="network")
    fabricate_run(world, phase="aborted", kind="network")
    fabricate_run(world, source="terminal")
    fabricate_run(world, hours_ago=25)
    fabricate_run(world)
    fabricate_run(world, phase="rolled_back")
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 202, out
    wait_phase(world, "done")


def test_preflight_status_resume_and_rollback_do_not_count(world, door):
    fabricate_run(world)
    fabricate_run(world)
    for _ in range(4):
        assert call(door, "POST", "/mc-update/preflight", {})[0] == 200
        assert call(door, "GET", "/mc-update/status")[0] == 200
        assert call(door, "POST", "/mc-update/resume", {})[0] == 200
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 202, out
    wait_phase(world, "done")


def test_start_limit_is_read_from_env(world, door):
    with open(world.inst / ".env", "a") as fh:
        fh.write("MC_UPDATE_MAX_STARTS_PER_DAY=1\n")
    fabricate_run(world)
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 429 and out["limit"] == 1


# ── status / rollback / resume ──────────────────────────────────────────────

def test_status_reports_the_run_and_the_bridge(world, door):
    status, out = call(door, "GET", "/mc-update/status")
    assert status == 200 and out["run_id"] is None
    status, started = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 202
    wait_phase(world, "done")
    wait_lock_free(world)
    status, out = call(door, "GET", f"/mc-update/status?run={started['run_id']}")
    assert status == 200, out
    assert out["phase"] == "done" and out["target"]["tag"] == "v0.2.0"
    assert out["bridge"]["api_version"] == door_mod.API_VERSION
    assert out["bridge"]["last_job"]["kind"] == "start" and out["bridge"]["last_job"]["exit"] == 0
    assert str(Path.home()) not in json.dumps(out)
    assert str(world.tmp) not in json.dumps(out["bridge"])


@pytest.mark.parametrize("query,code", [("?run=nope", 400), ("?run=20991231T000000Z-abcdef", 404),
                                        ("?other=1", 400)])
def test_status_with_a_bad_run(door, query, code):
    assert call(door, "GET", "/mc-update/status" + query)[0] == code


@pytest.mark.parametrize("body,code", [({"run_id": "../../etc"}, 400), ({"run_id": "20991231T000000Z-abcdef"}, 404),
                                       ({"backup": "/tmp/x.sql.gz"}, 400)])
def test_rollback_takes_only_a_known_run(world, door, body, code):
    fabricate_run(world)
    assert call(door, "POST", "/mc-update/rollback", body)[0] == code


def test_resume_without_an_open_run_says_so(world, door):
    status, out = call(door, "POST", "/mc-update/resume", {})
    assert status == 200, out
    assert out["resumed"] is False


def test_resume_runs_an_interrupted_run_to_the_end(world, door):
    assert world.run("start", fault="up.intent").returncode == 137
    status, out = call(door, "POST", "/mc-update/resume", {})
    assert status in (200, 202), out
    j = wait_phase(world, "done")
    assert j["resumes"]
    wait_lock_free(world)
    assert world.head_tag() == "v0.2.0"


def test_rollback_of_a_finished_run_runs_detached(world, door):
    world.patch_state(schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    _git(world.up, "tag", "-d", "v0.2.0")
    (world.up / "backend" / "alembic" / "versions" / "0002_more.py").unlink()
    _git(world.up, "add", "-A")
    _git(world.up, "commit", "-q", "-m", "v0.2.0 without migration")
    _git(world.up, "tag", "v0.2.0")
    assert world.run("start").returncode == 0
    run_id = world.journal()["run_id"]
    status, out = call(door, "POST", "/mc-update/rollback", {"run_id": run_id})
    assert status in (200, 202), out
    wait_phase(world, "rolled_back")
    wait_lock_free(world)
    assert world.head_tag() == "v0.1.0"


# ── detached: the job outlives the bridge ───────────────────────────────────

RUNNER = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mc_update_door as d
door = d.Door(repo=Path(sys.argv[2]), state=Path(sys.argv[3]), token="t" * 40, start_wait=30, log=lambda m: None)
door.detach = "fork"
door.system = lambda: "Darwin"
if len(sys.argv) > 4 and sys.argv[4] == "attached":
    door.new_session = False      # sabotage: the job stays in the bridge's process group
status, out = door.handle("POST", "/mc-update/start", {d.TOKEN_HEADER: "t" * 40},
                          json.dumps({"expected_target": "v0.2.0"}).encode())
print(json.dumps([status, out]), flush=True)
time.sleep(600)
"""


def _kill_bridge_during_up(world: World, mode: str) -> dict:
    world.patch_state(up_sleep_for_tag={"0.2.0": 2})
    env = dict(world.env, MC_REPO_PATH=str(world.inst))
    bridge = subprocess.Popen([sys.executable, "-c", RUNNER, str(SCRIPTS), str(world.inst), str(world.state), mode],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True)
    try:
        status, out = json.loads(bridge.stdout.readline())
        assert status == 202, out
        wait_phase(world, "up")
        # launchd / systemd stop a service by killing its whole process group
        os.killpg(bridge.pid, signal.SIGKILL)
        bridge.wait(timeout=10)
    finally:
        if bridge.poll() is None:
            os.killpg(bridge.pid, signal.SIGKILL)
    return out


def test_killing_the_bridge_does_not_stop_the_update(world):
    """Sabotage (b): run the job inside the bridge's process group → it dies with it."""
    _kill_bridge_during_up(world, "detached")
    j = wait_phase(world, "done", timeout=60)
    assert j["phase"] == "done"
    wait_lock_free(world)
    assert world.head_tag() == "v0.2.0"


def test_the_sabotage_of_detaching_is_seen(world):
    """The same kill without a new session leaves the run interrupted — proof
    that the test above measures the detaching, not luck."""
    _kill_bridge_during_up(world, "attached")
    time.sleep(4)
    j = world.journal()
    assert j["phase"] == "up"
    eng = door_mod.engine_module()
    assert eng.RunLock(world.state / "lock").live_holder() is None


def test_linux_detaches_through_systemd_run(world, tmp_path):
    bin_dir = tmp_path / "sdbin"
    bin_dir.mkdir()
    rec = tmp_path / "systemd-run.argv"
    (bin_dir / "systemd-run").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{rec}"\nexit 0\n')
    (bin_dir / "systemd-run").chmod(0o755)
    env = dict(world.env, PATH=f"{bin_dir}:{world.env['PATH']}")
    d = _door(world, env=env, start_wait=1.0)
    d.detach = None
    d.system = lambda: "Linux"
    d.linger_dir = tmp_path / "linger"          # linger on: start is allowed
    d.linger_dir.mkdir()
    (d.linger_dir / door_mod.current_user()).touch()
    status, out = call(d, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 202, out          # the fake systemd-run never starts it: "starting"
    argv = rec.read_text().splitlines()
    assert argv[:1] == ["--user"]
    assert "--collect" in argv
    unit = argv[argv.index("--unit") + 1]
    assert unit.startswith("mc-update-start-")
    assert f"--setenv=MC_REPO_PATH={world.inst}" in argv
    assert f"--setenv=MC_UPDATE_HOME={world.state}" in argv
    assert any(a.endswith("mc_update.py") for a in argv)
    assert "--expect" in argv and "v0.2.0" in argv
    assert journals(world) == []


def test_linux_falls_back_to_the_double_fork_when_systemd_run_fails(world, tmp_path):
    bin_dir = tmp_path / "sdbin"
    bin_dir.mkdir()
    (bin_dir / "systemd-run").write_text("#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n")
    (bin_dir / "systemd-run").chmod(0o755)
    env = dict(world.env, PATH=f"{bin_dir}:{world.env['PATH']}")
    d = _door(world, env=env)
    d.detach = None
    d.system = lambda: "Linux"
    d.linger_dir = None
    status, out = call(d, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status == 202, out
    wait_phase(world, "done")
    wait_lock_free(world)
    status, st = call(d, "GET", "/mc-update/status")
    assert st["bridge"]["last_job"]["detach"] == "fork"


# ── start routine: resume an interrupted run when the bridge starts ─────────

def test_boot_resumes_a_run_interrupted_after_the_switch(world, door):
    """Sabotage (c): without the start routine the run stays interrupted."""
    assert world.run("start", fault="up.intent").returncode == 137
    state = door.boot(docker_wait=10)
    assert state["state"] == "resuming", state
    j = wait_phase(world, "done")
    assert j["resumes"][0]["phase"] == "up"
    wait_lock_free(world)
    assert world.head_tag() == "v0.2.0"


def test_boot_aborts_a_run_interrupted_before_the_switch(world, door):
    assert world.run("start", fault="pull.intent").returncode == 137
    assert world.get_state()["drain"] is not None
    door.boot(docker_wait=10)
    wait_phase(world, "aborted")
    wait_lock_free(world)
    assert world.head_tag() == "v0.1.0"
    assert world.get_state().get("drain") is None


def test_boot_waits_for_docker_first(world, door):
    assert world.run("start", fault="up.intent").returncode == 137
    world.patch_state(info_fail=5)
    state = door.boot(docker_wait=20)
    assert state["state"] == "resuming"
    assert sum(1 for c in world.calls() if c[:1] == ["info"]) >= 6
    wait_phase(world, "done")


def test_boot_gives_up_when_docker_never_answers(world, door):
    assert world.run("start", fault="up.intent").returncode == 137
    world.patch_state(info_fail=10_000)
    state = door.boot(docker_wait=0.5)
    assert state["state"] == "docker_unavailable"
    time.sleep(0.5)
    assert world.journal()["phase"] == "up"


def test_boot_leaves_a_live_run_alone(world, door):
    p = _live_run(world)
    try:
        state = door.boot(docker_wait=5)
        assert state["state"] == "running"
    finally:
        p.kill()
        p.wait()


def test_boot_lifts_a_drain_left_by_a_finished_run(world, door):
    run_id = fabricate_run(world, phase="aborted")
    world.patch_state(drain={"run_id": run_id, "expires_at": "2099-01-01T00:00:00Z", "set_by": "mc_update"})
    state = door.boot(docker_wait=5)
    assert state["state"] == "idle"
    assert world.get_state().get("drain") is None
    assert state["drain_sweep"]["lifted"][0]["run_id"] == run_id


def test_boot_without_docker_keeps_the_drain_of_an_interrupted_run(world, door):
    assert world.run("start", fault="pull.intent").returncode == 137
    drain = world.get_state()["drain"]
    world.patch_state(info_fail=10_000)
    door.boot(docker_wait=0.3)
    assert world.get_state()["drain"] == drain
    assert world.journal()["phase"] == "pull"


def test_engine_drain_sweep_refuses_while_a_run_is_open(world):
    assert world.run("start", fault="up.intent").returncode == 137
    r = world.run("drain-sweep", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["lifted"] == [] and "interrupted" in out["skipped"]
    assert world.get_state()["drain"] is not None


# ── the bridge: wiring, /ping, separate token scope over HTTP ───────────────

def _load_bridge(tmp_path: Path):
    os.environ["CLI_BRIDGE_LOG_DIR"] = str(tmp_path / "bridge-logs")
    try:
        spec = importlib.util.spec_from_file_location("cli_bridge_door_test", SCRIPTS / "cli-bridge.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.pop("CLI_BRIDGE_LOG_DIR", None)
    mod.BRIDGE_TOKEN = BTOKEN
    return mod


@pytest.fixture
def http_bridge(world, tmp_path):
    mod = _load_bridge(tmp_path)
    mod.UPDATE_DOOR = _door(world)
    server = mod._make_http_server("127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield mod, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _http(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw.startswith("{") else raw)


def test_ping_names_the_api_version(http_bridge):
    _, base = http_bridge
    assert _http(base + "/ping") == (200, {"status": "ok", "api_version": door_mod.API_VERSION})


def test_the_two_tokens_do_not_open_each_others_doors(http_bridge):
    _, base = http_bridge
    assert _http(base + "/mc-update/status", headers={"X-MC-Bridge-Token": BTOKEN})[0] == 401
    assert _http(base + "/mc-update/status", headers={door_mod.TOKEN_HEADER: BTOKEN})[0] == 401
    assert _http(base + "/health", headers={door_mod.TOKEN_HEADER: UTOKEN})[0] == 401
    assert _http(base + "/mc-update/status", headers={door_mod.TOKEN_HEADER: UTOKEN})[0] == 200
    assert _http(base + "/health", headers={"X-MC-Bridge-Token": BTOKEN})[0] == 200


def test_http_door_answers_the_door_codes(http_bridge, world):
    _, base = http_bridge
    h = {door_mod.TOKEN_HEADER: UTOKEN}
    assert _http(base + "/mc-update/start", "POST", h, {"expected_target": "v0.3.0"})[0] == 409
    assert _http(base + "/mc-update/nope", "POST", h, {})[0] == 404
    assert _http(base + "/mc-update/start", "DELETE", h)[0] == 405
    assert journals(world) == []


def test_oversized_body_is_refused(http_bridge):
    _, base = http_bridge
    h = {door_mod.TOKEN_HEADER: UTOKEN}
    assert _http(base + "/mc-update/start", "POST", h, {"expected_target": "v0.2.0", "pad": "x" * 9000})[0] == 413


def test_door_not_loaded_fails_closed(http_bridge):
    mod, base = http_bridge
    mod.UPDATE_DOOR = None
    assert _http(base + "/mc-update/status", headers={door_mod.TOKEN_HEADER: UTOKEN})[0] == 401


def test_bridge_start_runs_the_resume_routine(world, tmp_path):
    """Sabotage (c), wired: the bridge's start hook must launch the routine."""
    mod = _load_bridge(tmp_path)
    assert world.run("start", fault="up.intent").returncode == 137
    d = _door(world)
    t = mod._start_update_door(d, docker_wait=10)
    t.join(30)
    wait_phase(world, "done")
    wait_lock_free(world)
    assert world.head_tag() == "v0.2.0"


def test_bridge_starts_the_boot_routine_only_after_its_bind(tmp_path):
    """Linux binds to MC's network gateway, which can be missing for a while;
    systemd then restarts the bridge every few seconds. Each restart must not
    spawn another boot routine (orphaned `docker info` / drain-sweep children,
    a relaunched resume job wiping the previous job's files)."""
    src = (SCRIPTS / "cli-bridge.py").read_text()
    main = src.split('if __name__ == "__main__":', 1)[1]
    assert main.index("_make_http_server(BIND, PORT)") < main.index("_start_update_door(UPDATE_DOOR)")


def test_launch_prunes_job_files_older_than_two_weeks(world, door):
    jobs = door._jobs_dir()
    old = time.time() - 15 * 86400
    for name in ("start-r1.out", "start-r1.exit", "resume-r1-boot.out", "resume-r1-boot.exit"):
        (jobs / name).write_text("0\n")
        os.utime(jobs / name, (old, old))
    (jobs / "start-r2.exit").write_text("0\n")       # recent: kept
    keep = jobs / "notes.txt"                          # not a job file: never touched
    keep.write_text("x")
    os.utime(keep, (old, old))
    status, out = call(door, "POST", "/mc-update/start", {"expected_target": "v0.2.0"})
    assert status in (200, 202), out
    names = {p.name for p in jobs.iterdir()}
    assert not names & {"start-r1.out", "start-r1.exit", "resume-r1-boot.out", "resume-r1-boot.exit"}
    assert {"start-r2.exit", "notes.txt"} <= names
    wait_phase(world, "done")
    wait_lock_free(world)
