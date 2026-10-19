"""The update engine (scripts/mc_update.py, ADR-094) — PR 2a + 2b of the update plan.

Every test runs the real engine end to end in a scratch world:

* a local git "upstream" with two releases, v0.1.0 and v0.2.0, each carrying
  the engine itself (so the hand-over to the *target's* engine is real), the
  repository's real ``backup.sh`` (pre-update backup and restore run for
  real against the fake docker), a fake ``setup.sh`` and a migration chain
  0001 → 0002;
  v0.1.0 still tracks the fleet file, v0.2.0 no longer does — the case that
  makes git delete it during a plain checkout;
* an install cloned on v0.1.0 with a ``.env`` and an operator-edited fleet
  file;
* a fake ``docker`` in PATH (fake_docker.py) that keeps images, containers,
  health, schema, running work and the drain row in a JSON state file;
* ``file://`` URLs for the release API and the backend's ``/health``.

No network, no real Docker, no Postgres. Crashes are real too: with
``MC_UPDATE_TEST_FAULT=<point>`` the engine kills itself at that point
(exit 137) and the test resumes from what is on disk.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts" / "mc_update.py"
FAKE_DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
FLEET = "docker/docker-compose.agents.yml"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

BACKUP_SH = ROOT / "backup.sh"
FAKE_SETUP = """#!/usr/bin/env bash
echo "setup on $(git describe --tags --exact-match HEAD 2>/dev/null || echo none)" >> "$STUB_BACKUP_LOG"
"""

USER_FLEET = "services:\n  agent-one:\n    image: ghcr.io/argyelan-ai/mc-agent-claude:0.1.0\n  # edited by the operator\n"
COMMITTED_FLEET = "services: {}\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.up = tmp / "upstream"
        self.inst = tmp / "mission-control"
        self.state = tmp / "update-state"
        self.stub_state = tmp / "docker-state.json"
        self.log = tmp / "docker-calls.jsonl"
        self.backup_log = tmp / "backup.log"
        self.api = tmp / "api"
        self.backups = tmp / "backups"
        self.release_json = tmp / "latest.json"

    # ── building ──────────────────────────────────────────────────────────
    def build(self) -> "World":
        up = self.up
        up.mkdir()
        _git(up, "init", "-q", "-b", "main")
        (up / "docker-compose.yml").write_text("services: {}\n")
        shutil.copy(BACKUP_SH, up / "backup.sh")
        (up / "backup.sh").chmod(0o755)
        (up / "setup.sh").write_text(FAKE_SETUP)
        (up / "setup.sh").chmod(0o755)
        (up / "scripts").mkdir()
        shutil.copy(ENGINE, up / "scripts" / "mc_update.py")
        (up / "docker").mkdir()
        (up / FLEET).write_text(COMMITTED_FLEET)
        versions = up / "backend" / "alembic" / "versions"
        versions.mkdir(parents=True)
        (versions / "0001_init.py").write_text('revision = "0001"\ndown_revision = None\n')
        _git(up, "add", "-A")
        _git(up, "commit", "-q", "-m", "v0.1.0")
        _git(up, "tag", "v0.1.0")
        # v0.2.0: one migration more, the fleet file left version control.
        (versions / "0002_more.py").write_text("revision: str = '0002'\ndown_revision: Union[str, None] = '0001'\n")
        _git(up, "rm", "-q", "--cached", FLEET)
        (up / ".gitignore").write_text(FLEET + "\n")
        _git(up, "add", "-A")
        _git(up, "commit", "-q", "-m", "v0.2.0")
        _git(up, "tag", "v0.2.0")

        _git(self.tmp, "clone", "-q", "--branch", "v0.1.0", str(up), str(self.inst))
        (self.inst / ".env").write_text(
            "DB_PASSWORD=keep-me\nMC_CHANNEL=stable\nMC_IMAGE_TAG=0.1.0\nMC_AGENT_IMAGE_TAG=0.1.0\n")
        (self.inst / FLEET).write_text(USER_FLEET)

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_DOCKER}" "$@"\n')
        (bin_dir / "docker").chmod(0o755)
        self.release_json.write_text(json.dumps({"tag_name": "v0.2.0", "draft": False, "prerelease": False}))
        self.log.write_text("")
        self.backup_log.write_text("")
        old = {"backend": "sha256:mc-backend-0.1.0", "frontend": "sha256:mc-frontend-0.1.0"}
        self.set_state({
            "repo": str(self.inst),
            "images": {f"ghcr.io/argyelan-ai/mc-backend:0.1.0": old["backend"],
                       f"ghcr.io/argyelan-ai/mc-frontend:0.1.0": old["frontend"]},
            "containers": {
                "backend": {"image": old["backend"], "tag": "0.1.0", "state": "running", "health": "healthy"},
                "mc-worker": {"image": old["backend"], "tag": "0.1.0", "state": "running", "health": "healthy"},
                "frontend": {"image": old["frontend"], "tag": "0.1.0", "state": "running", "health": "healthy"},
            },
            "schema": "0001",
            "schema_for_tag": {"0.1.0": "0001", "0.2.0": "0002"},
            "in_progress": [0],
            "drain_table": True,
        })
        self.api.mkdir()
        (self.api / "health").write_text(json.dumps({"status": "ok", "version": "v0.1.0"}))
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_STATE": str(self.stub_state), "STUB_LOG": str(self.log), "STUB_API": str(self.api),
            "STUB_BACKUP_LOG": str(self.backup_log),
            "MC_UPDATE_HOME": str(self.state),
            "MC_REPO_URL": str(up),
            "MC_RELEASE_API": self.release_json.as_uri(),
            "MC_API_URL": self.api.as_uri(),
            "MC_BACKUP_DIR": str(self.backups),
            "MC_BACKUP_MIN_FREE_MB": "1",
            "HOME": str(self.tmp),
            "MC_UPDATE_POLL_INTERVAL": "0.01",
            "MC_UPDATE_START_PERIOD": "0",
            "MC_UPDATE_VERIFY_TIMEOUT": "5",
            "MC_UPDATE_DRAIN_TIMEOUT": "5",
            "MC_UPDATE_MIN_FREE_MB": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        for k in ("MC_UPDATE_TEST_FAULT", "MC_UPDATE_LOCK_FD", "MC_REPO_PATH", "COMPOSE_PROJECT_NAME",
                  "MC_UPDATE_MANAGED", "MC_IMAGE_TAG", "MC_BACKUP_PASSPHRASE", "MC_BACKUP_PASSPHRASE_FILE",
                  "MC_BACKUP_KEEP", "MC_INSTALL_DIR", "MC_AGENT_IMAGE_TAG", "MC_CHANNEL"):
            self.env.pop(k, None)
        return self

    # ── state helpers ─────────────────────────────────────────────────────
    def set_state(self, data: dict) -> None:
        self.stub_state.write_text(json.dumps(data))

    def patch_state(self, **kw) -> None:
        s = self.get_state()
        s.update(kw)
        self.set_state(s)

    def get_state(self) -> dict:
        return json.loads(self.stub_state.read_text())

    def calls(self) -> list:
        return [json.loads(line)["argv"] for line in self.log.read_text().splitlines() if line.strip()]

    def compose_calls(self, sub: str) -> list:
        out = []
        for argv in self.calls():
            if argv[:1] == ["compose"]:
                rest = argv[3:] if argv[1:2] == ["-p"] else argv[1:]
                if rest[:1] == [sub]:
                    out.append(argv)
        return out

    def journal(self, run_id: str = "") -> dict:
        runs = sorted((self.state / "journal").glob("*.json"))
        assert runs, "no journal written"
        path = (self.state / "journal" / f"{run_id}.json") if run_id else runs[-1]
        return json.loads(path.read_text())

    def head_tag(self) -> str:
        return subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=self.inst,
                              capture_output=True, text=True).stdout.strip()

    def env_value(self, key: str) -> str:
        for line in (self.inst / ".env").read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return ""

    def run(self, *args: str, fault: str = "", extra_env: dict = None, timeout: int = 60):
        env = dict(self.env)
        if fault:
            env["MC_UPDATE_TEST_FAULT"] = fault
        if extra_env:
            env.update(extra_env)
        engine = self.inst / "scripts" / "mc_update.py"
        return subprocess.run([sys.executable, str(engine), *args], cwd=self.inst, env=env,
                              capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path).build()


def _phases(j: dict) -> list:
    return [(e["phase"], e["event"]) for e in j["events"]]


# ── the normal update ───────────────────────────────────────────────────────

def test_update_runs_all_phases_in_order_and_ends_done(world: World):
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "done" and j["status"] == "done"
    order = [p for p, ev in _phases(j) if ev == "done"]
    assert order == ["preflight", "drain", "fetch", "verify_release", "pull", "backup",
                     "switch", "up", "verify", "done"]
    # Every phase journaled its intent before its result.
    for phase in ("drain", "fetch", "verify_release", "pull", "backup", "switch", "up", "verify"):
        evs = [ev for p, ev in _phases(j) if p == phase and ev in ("intent", "done")]
        assert evs[:2] == ["intent", "done"], (phase, evs)
    assert world.head_tag() == "v0.2.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.2.0"
    assert world.env_value("MC_AGENT_IMAGE_TAG") == "0.2.0"
    assert world.env_value("DB_PASSWORD") == "keep-me"
    # .env is bind-mounted as a single file: written in place, same inode.
    assert (world.inst / ".env").stat().st_ino == inode
    st = world.get_state()
    assert st["containers"]["backend"]["image"] == "sha256:mc-backend-0.2.0"
    assert st["containers"]["mc-worker"]["image"] == "sha256:mc-backend-0.2.0"
    assert st["containers"]["frontend"]["image"] == "sha256:mc-frontend-0.2.0"
    assert "✅ Update complete" in r.stdout


def test_pull_takes_only_mc_images(world: World):
    """Sabotage (c): add qdrant to the pull list → red."""
    assert world.run("start").returncode == 0
    pulls = world.compose_calls("pull")
    assert len(pulls) == 1
    assert pulls[0][-2:] == ["backend", "frontend"]
    assert pulls[0][3:] == ["pull", "backend", "frontend"]
    ups = world.compose_calls("up")
    assert ups and all(c[3:] == ["up", "-d", "--pull", "never", "backend", "mc-worker", "frontend"] for c in ups)


def test_backup_runs_on_the_old_release_before_the_switch(world: World):
    """Sabotage (e): drop the backup step from the engine → red."""
    assert world.run("start").returncode == 0
    log = world.backup_log.read_text().splitlines()
    assert log[0] == "backup on v0.1.0"
    assert "setup on v0.2.0" in log
    j = world.journal()
    f = Path(j["backup"]["file"])
    # backup.sh's own pre-update kind: own prefix, pins, no ~/.mc archive.
    assert f.exists() and f.name.startswith("mc_backup_preupdate_"), f
    assert j["backup"]["schema"] == "0001" == j["rollback"]["schema_before"]
    pins = f.with_name(f.name.replace("mc_backup_", "mc_pins_").replace(".sql.gz", ".txt")).read_text()
    assert "MC_IMAGE_TAG=0.1.0" in pins and "SCHEMA=0001" in pins and "GIT_TAG=v0.1.0" in pins
    assert not list(world.backups.glob("mc_data_*")), "an update does not archive ~/.mc"
    # Not the daily kind: the daily rotation can never remove it.
    assert not list(world.backups.glob("mc_backup_2*"))


def test_worker_is_stopped_before_the_code_switch(world: World):
    assert world.run("start").returncode == 0
    calls = world.calls()
    stop_idx = next(i for i, c in enumerate(calls) if c[:1] == ["compose"] and c[3:] == ["stop", "mc-worker"])
    up_idx = next(i for i, c in enumerate(calls) if c[:1] == ["compose"] and c[3:4] == ["up"])
    assert stop_idx < up_idx
    j = world.journal()
    sw = [e for e in j["events"] if e["phase"] == "switch"]
    assert sw, "no switch events"


def test_fleet_file_survives_the_switch(world: World):
    """v0.1.0 tracks the fleet file, v0.2.0 does not — a plain checkout deletes it."""
    assert world.run("start").returncode == 0
    assert (world.inst / FLEET).read_text() == USER_FLEET


def test_target_engine_runs_from_switch_on(world: World):
    assert world.run("start").returncode == 0
    j = world.journal()
    run_dir = world.state / f"run-{j['run_id']}"
    copy = run_dir / "mc_update.py"
    assert copy.read_text() == subprocess.run(["git", "show", "v0.2.0:scripts/mc_update.py"], cwd=world.up,
                                              capture_output=True, text=True).stdout
    installed = os.path.realpath(world.inst / "scripts" / "mc_update.py")
    by_phase = {}
    for e in j["events"]:
        by_phase.setdefault(e["phase"], set()).add(e["engine"])
    for phase in ("drain", "fetch", "verify_release", "pull", "backup"):
        assert by_phase[phase] == {installed}, phase
    for phase in ("switch", "up", "verify", "done"):
        assert by_phase[phase] == {os.path.realpath(copy)}, phase
    assert any(e["event"] == "handoff" for e in j["events"])


def test_real_compose_project_name_is_used(world: World):
    world.patch_state(project_label="mc-custom")
    r = world.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    compose = [c for c in world.calls() if c[:1] == ["compose"]]
    assert compose and all(c[1:3] == ["-p", "mc-custom"] for c in compose)
    j = world.journal()
    assert j["project"] == "mc-custom"
    assert any(c["id"] == "project" and c["status"] == "warn" for c in j["preflight"])


def test_drain_is_set_with_expiry_and_lifted(world: World):
    assert world.run("start").returncode == 0
    st = world.get_state()
    assert st["drain_history"][0] == "set" and st["drain_history"][-1] == "cleared"
    assert st["drain"] is None
    j = world.journal()
    assert j["drain"]["supported"] is True and j["drain"]["active"] is False
    assert j["drain"]["expires_at"].endswith("Z")


def test_drain_waits_for_running_work(world: World):
    # The preflight counts once (2), then the drain waits: 2 → 1 → 0.
    world.patch_state(in_progress=[2, 2, 1, 0])
    r = world.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "waiting for 2 running task(s)" in r.stdout
    assert "waiting for 1 running task(s)" in r.stdout
    # No new work from the moment it waits: the drain is set first.
    assert r.stdout.index("no new work is handed out") < r.stdout.index("waiting for 2")


def test_drain_counts_running_work_through_the_backend_function(world: World):
    """From schema 0219 on, "running work" has one definition: the function
    update_running_work() (tasks in progress + open group-chat rounds, PR 4c).
    The engine waits for exactly what the UI shows; older schemas fall back
    to counting tasks."""
    world.patch_state(work_fn=True, in_progress=[1, 1, 0])
    r = world.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "waiting for 1 running task(s) or group-chat round(s)" in r.stdout
    counts = [c[-1] for c in world.calls() if c[:1] == ["compose"] and "update_running_work" in c[-1]]
    assert any(c == "select coalesce(sum(n), 0) from update_running_work()" for c in counts), counts
    assert not any("status = 'in_progress'" in c[-1] for c in world.calls() if c[:1] == ["compose"])


def test_drain_without_the_function_counts_tasks_only(world: World):
    world.patch_state(work_fn=False, in_progress=[1, 0])
    r = world.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any("status = 'in_progress'" in c[-1] for c in world.calls() if c[:1] == ["compose"])
    assert not any(c[-1] == "select coalesce(sum(n), 0) from update_running_work()"
                   for c in world.calls() if c[:1] == ["compose"])


def test_running_work_that_never_ends_aborts_without_change(world: World):
    world.patch_state(in_progress=[1])
    r = world.run("start", extra_env={"MC_UPDATE_DRAIN_TIMEOUT": "0.3"})
    assert r.returncode == 3, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "aborted"
    assert world.head_tag() == "v0.1.0"
    assert world.get_state()["drain"] is None
    assert not world.compose_calls("pull")


def test_without_drain_table_the_engine_says_so_and_continues(world: World):
    world.patch_state(drain_table=False)
    r = world.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no drain switch" in r.stdout
    assert world.journal()["drain"]["supported"] is False


# ── the four checks and the way back (R1) ───────────────────────────────────

def test_unhealthy_new_version_rolls_back_to_the_old_tag(world: World):
    """Sabotage (a): health stays unhealthy → R1, old tag. Breaking R1 → red."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start")
    assert r.returncode == 1, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "rolled_back"
    assert "not healthy" in j["error"]
    assert world.head_tag() == "v0.1.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert (world.inst / ".env").stat().st_ino == inode
    assert (world.inst / FLEET).read_text() == USER_FLEET
    st = world.get_state()
    assert st["containers"]["backend"]["image"] == "sha256:mc-backend-0.1.0"
    assert st["containers"]["mc-worker"]["state"] == "running"
    assert st["drain"] is None
    assert "set back to v0.1.0" in r.stdout
    # R1: the database was not touched.
    assert j["rollback"]["path"] == "R1"
    assert "db_history" not in st
    assert "database was not changed" in r.stdout


def test_wrong_version_after_up_is_a_failure(world: World):
    """Check 3: /health must report the target version."""
    world.patch_state(schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    # A backend that comes up but reports the old version.
    orig = world.env["PATH"]
    shim = world.tmp / "shim"
    shim.mkdir()
    (shim / "docker").write_text(
        f'#!/bin/sh\n"{sys.executable}" "{FAKE_DOCKER}" "$@"; rc=$?\n'
        f'printf \'{{"status":"ok","version":"v0.1.0"}}\' > "{world.api}/health"\nexit $rc\n')
    (shim / "docker").chmod(0o755)
    r = world.run("start", extra_env={"PATH": f"{shim}:{orig}"})
    assert r.returncode in (1, 4), r.stdout + r.stderr
    j = world.journal()
    assert "version" in (j["error"] or "")


def _assert_r2(world: World, r, inode: int) -> dict:
    """The way back restored the database (R2) and then went back like R1."""
    j = world.journal()
    assert j["phase"] == "rolled_back", (j["phase"], j.get("error"), r.stdout + r.stderr)
    assert j["rollback"]["path"] == "R2"
    st = world.get_state()
    assert st["schema"] == "0001" == j["rollback"]["schema_before"]
    assert st["db_history"] == ["dropped", "restored"]
    assert world.head_tag() == "v0.1.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.1.0" and world.env_value("DB_PASSWORD") == "keep-me"
    assert (world.inst / ".env").stat().st_ino == inode, ".env must be written in place"
    assert (world.inst / FLEET).read_text() == USER_FLEET
    assert st["containers"]["backend"]["image"] == "sha256:mc-backend-0.1.0"
    assert st["containers"]["mc-worker"]["image"] == "sha256:mc-backend-0.1.0"
    assert st["drain"] is None
    # backend AND mc-worker are stopped before the database is dropped, and
    # nothing starts them again on the new code after the restore.
    calls = world.calls()
    drop = next(i for i, c in enumerate(calls) if c[:1] == ["compose"] and "-c" in c and "pg_dump" not in c)
    stopped = set()
    for c in calls[:drop]:
        if c[:1] == ["compose"] and c[3:4] == ["stop"]:
            stopped.update(c[4:])
    assert {"backend", "mc-worker"} <= stopped, stopped
    after = calls[drop:]
    assert not any(c[:1] == ["compose"] and c[3:4] in (["start"], ["restart"]) for c in after)
    # The first `up` after the restore runs the old images.
    ups = [c for c in after if c[:1] == ["compose"] and c[3:4] == ["up"] and "backend" in c]
    assert ups
    return j


def _release_with_two_migrations(world: World) -> None:
    """v0.2.0 brings 0002 and 0003 — a chain that can stop half-way."""
    _git(world.up, "tag", "-d", "v0.2.0")
    (world.up / "backend" / "alembic" / "versions" / "0003_even_more.py").write_text(
        'revision = "0003"\ndown_revision = "0002"\n')
    _git(world.up, "add", "-A")
    _git(world.up, "commit", "-q", "-m", "v0.2.0 with two migrations")
    _git(world.up, "tag", "v0.2.0")


def test_migration_chain_stopped_half_way_restores_the_database(world: World):
    """Sabotage (b): the schema stands on an *intermediate* revision (0002 of
    0001→0002→0003), neither old nor new. Comparing against the new head would
    take R1 (old code on a half-migrated database) — the rule is against
    schema_before, so it must be R2."""
    _release_with_two_migrations(world)
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0002"})
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start")
    assert r.returncode == 1, r.stdout + r.stderr
    j = _assert_r2(world, r, inode)
    assert j["target"]["schema_head"] == "0003"
    assert "restored from the pre-update backup" in r.stdout
    assert Path(j["backup"]["file"]).name in r.stdout


def test_migrated_schema_is_restored_before_old_code_starts(world: World):
    """Schema already on the new head ≠ schema_before → R2, never old code on a
    newer schema."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"})
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start")
    assert r.returncode == 1, r.stdout + r.stderr
    _assert_r2(world, r, inode)


def test_unreadable_schema_counts_as_changed(world: World):
    """The database answers but has no schema version (a broken migration) —
    the engine cannot tell, so it restores (R2)."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, schema_for_tag={"0.1.0": "0001", "0.2.0": ""})
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start")
    assert r.returncode == 1, r.stdout + r.stderr
    _assert_r2(world, r, inode)


def test_r2_keeps_the_env_and_leaves_no_secret_copies_in_the_install(world: World):
    """With a passphrase the backup holds an encrypted .env. The restore must
    not put that one back (--keep-env): the engine writes its own copy in
    place, and no .env.backup-pre-restore-* file is left in the folder."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"})
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start", extra_env={"MC_BACKUP_PASSPHRASE": "pw"})
    assert r.returncode == 1, r.stdout + r.stderr
    j = _assert_r2(world, r, inode)
    enc = Path(j["backup"]["file"].replace("mc_backup_", "mc_env_").replace(".sql.gz", ".env.enc"))
    assert enc.exists()
    assert not list(world.inst.glob(".env.backup-pre-restore-*"))


def test_r2_without_the_backup_fails_safe_and_can_be_finished_later(world: World):
    """The backup vanished between the switch and the way back: no old code on
    the new schema, MC stays drained, and `rollback` finishes the job once the
    backup is back."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"})
    hidden = world.tmp / "hidden"
    hidden.mkdir()
    # Hide the backup right after `up`: a shim moves it away on the first `up`.
    orig = world.env["PATH"]
    shim = world.tmp / "shim"
    shim.mkdir()
    (shim / "docker").write_text(
        f'#!/bin/sh\ncase "$*" in *" up -d --pull never backend"*) mv "{world.backups}"/mc_backup_preupdate_* "{hidden}"/ 2>/dev/null;; esac\n'
        f'exec "{sys.executable}" "{FAKE_DOCKER}" "$@"\n')
    (shim / "docker").chmod(0o755)
    r = world.run("start", extra_env={"PATH": f"{shim}:{orig}"})
    assert r.returncode == 4, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "rollback_failed"
    assert "is missing" in j["error"]
    assert world.head_tag() == "v0.2.0"              # no old code on the new schema
    assert world.get_state()["drain"] is not None    # stays in no-new-work mode
    assert "db_history" not in world.get_state()     # nothing dropped
    assert "./install.sh --rollback" in r.stdout
    # The operator puts the backup back and asks for the way back.
    for f in hidden.iterdir():
        shutil.move(str(f), str(world.backups / f.name))
    r = world.run("rollback")
    assert r.returncode == 5, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "rolled_back" and j["rollback"]["path"] == "R2"
    assert world.get_state()["schema"] == "0001" and world.head_tag() == "v0.1.0"


def test_failed_restore_ends_rollback_failed_on_the_new_code(world: World):
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, restore_fail=True)
    r = world.run("start")
    assert r.returncode == 4, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "rollback_failed"
    assert "restore" in j["error"]
    assert world.head_tag() == "v0.2.0"
    assert world.get_state()["drain"] is not None


@pytest.mark.parametrize("fault", ["rolling_back.r2_stopped", "rolling_back.restored",
                                   "rolling_back.checked_out"])
def test_crash_during_r2_resumes_the_way_back(world: World, fault: str):
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"})
    inode = (world.inst / ".env").stat().st_ino
    r = world.run("start", fault=fault)
    assert r.returncode == 137, r.stdout + r.stderr
    assert world.journal()["phase"] == "rolling_back"
    r = world.run("resume")
    assert r.returncode == 1, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "rolled_back"
    st = world.get_state()
    assert st["schema"] == "0001" and world.head_tag() == "v0.1.0"
    assert (world.inst / ".env").stat().st_ino == inode
    assert (world.inst / FLEET).read_text() == USER_FLEET


def test_schema_that_moves_after_the_backup_aborts_before_the_switch(world: World):
    """The backup must hold exactly schema_before — otherwise R2 could not
    bring the database back to it. Caught before anything changes."""
    world.patch_state(schema_after_dump="0002")
    r = world.run("start")
    assert r.returncode == 3, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "aborted" and "after the backup" in j["error"]
    assert world.head_tag() == "v0.1.0"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["stop"], ["up"]) for c in world.calls())


def test_failures_inside_start_period_do_not_count(world: World):
    world.patch_state(unhealthy_inspects=6)
    r = world.run("start", extra_env={"MC_UPDATE_START_PERIOD": "1.5", "MC_UPDATE_POLL_INTERVAL": "0.05"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert world.journal()["phase"] == "done"


def test_unhealthy_after_start_period_fails_before_the_deadline(world: World):
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    t0 = time.monotonic()
    r = world.run("start", extra_env={"MC_UPDATE_START_PERIOD": "0.3", "MC_UPDATE_VERIFY_TIMEOUT": "30"})
    assert r.returncode == 1
    assert time.monotonic() - t0 < 20


# ── refusals: nothing changes ───────────────────────────────────────────────

def test_backup_failure_aborts_before_the_switch(world: World):
    """Sabotage (a): backup exits 1 → HEAD unchanged, no checkout, no switch."""
    world.patch_state(dump_fail=True)
    head = _git(world.inst, "rev-parse", "HEAD")
    r = world.run("start")
    assert r.returncode == 3, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "aborted" and "backup failed" in j["error"]
    assert not j["backup"].get("file")
    assert _git(world.inst, "rev-parse", "HEAD") == head
    assert world.head_tag() == "v0.1.0" and world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert not any(e["phase"] == "switch" for e in j["events"])
    assert not any(c[3:] == ["stop", "mc-worker"] for c in world.calls() if c[:1] == ["compose"])
    assert world.get_state()["drain"] is None


def test_pull_failure_aborts_and_is_marked_network(world: World):
    world.patch_state(pull_fail=True)
    r = world.run("start")
    assert r.returncode == 3
    j = world.journal()
    assert j["phase"] == "aborted" and j["failure_kind"] == "network"
    assert world.head_tag() == "v0.1.0"


def test_release_list_unreachable_is_fail_closed(world: World):
    r = world.run("start", extra_env={"MC_RELEASE_API": (world.tmp / "missing.json").as_uri()})
    assert r.returncode == 3
    assert "release list not available" in r.stdout
    assert not (world.state / "journal").exists() or not list((world.state / "journal").glob("*.json"))
    assert not any(c[:1] == ["compose"] and c[3:4] == ["pull"] for c in world.calls())


def test_prerelease_is_not_a_target(world: World):
    world.release_json.write_text(json.dumps({"tag_name": "v0.2.0", "prerelease": True}))
    r = world.run("preflight", "--json")
    assert r.returncode == 3
    assert any(c["id"] == "release" and c["status"] == "fail" for c in json.loads(r.stdout)["checks"])


def test_managed_install_is_refused(world: World):
    with open(world.inst / ".env", "a") as f:
        f.write("MC_UPDATE_MANAGED=deploy\n")
    for cmd in ("preflight", "start"):
        r = world.run(cmd)
        assert r.returncode == 9, (cmd, r.stdout, r.stderr)
    assert world.head_tag() == "v0.1.0"
    assert not (world.state / "journal").exists()


def test_expected_target_mismatch_is_refused(world: World):
    r = world.run("start", "--expect", "v0.3.0")
    assert r.returncode == 9
    assert "differs" in r.stderr
    assert world.run("start", "--expect", "v0.2.0").returncode == 0


def test_already_on_latest_is_nothing_to_do(world: World):
    world.release_json.write_text(json.dumps({"tag_name": "v0.1.0"}))
    r = world.run("start")
    assert r.returncode == 0
    assert "nothing to do" in r.stdout
    assert not (world.state / "journal").exists()


def test_preflight_changes_nothing(world: World):
    before = world.get_state()
    r = world.run("preflight", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["target"] == "v0.2.0"
    assert world.get_state() == before
    assert world.head_tag() == "v0.1.0"
    plan = json.loads(world.run("plan", "--json").stdout)
    assert plan["pull"] == ["ghcr.io/argyelan-ai/mc-backend:0.2.0", "ghcr.io/argyelan-ai/mc-frontend:0.2.0"]


# ── lock ────────────────────────────────────────────────────────────────────

def _load_engine_module():
    spec = importlib.util.spec_from_file_location("mc_update_under_test", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_live_lock_holder_gets_423(world: World):
    world.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(world.state / "lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, json.dumps({"pid": os.getpid(), "run_id": "x"}).encode())
    try:
        r = world.run("start")
        assert r.returncode == 23, r.stdout + r.stderr
        assert world.head_tag() == "v0.1.0"
    finally:
        os.close(fd)
    assert world.run("start").returncode == 0


def test_status_and_preflight_never_take_the_lock(world: World):
    """They are polled by the UI. A live holder is read from the record —
    the probe must not grab the flock (a start at that instant would get 423)."""
    world.state.mkdir(parents=True, exist_ok=True)
    mod = _load_engine_module()
    (world.state / "lock").write_text(json.dumps({"pid": os.getpid(), "start": mod.process_start(os.getpid()),
                                                  "run_id": "x"}))
    st = json.loads(world.run("status", "--json").stdout)
    assert st["lock"]["held"] is True
    pf = json.loads(world.run("preflight", "--json").stdout)
    assert any(c["id"] == "lock" and c["status"] == "fail" for c in pf["checks"])
    calls = []
    real_flock = mod.fcntl.flock
    mod.fcntl.flock = lambda *a: (calls.append(a), real_flock(*a))[1]
    try:
        eng = mod.Engine(world.inst, world.state)
        assert eng.lock.live_holder() is not None
    finally:
        mod.fcntl.flock = real_flock
    assert calls == []


def test_lock_left_by_a_dead_process_does_not_block(world: World):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    world.state.mkdir(parents=True, exist_ok=True)
    (world.state / "lock").write_text(json.dumps({"pid": dead.pid, "start": "x", "run_id": "old"}))
    mod = _load_engine_module()
    holder = mod.RunLock(world.state / "lock").holder()
    assert holder["alive"] is False
    assert world.run("start").returncode == 0


def test_reused_pid_is_recognised_as_dead(tmp_path: Path):
    mod = _load_engine_module()
    lock = tmp_path / "lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "start": "a-different-start-time"}))
    assert mod.RunLock(lock).holder()["alive"] is False
    lock.write_text(json.dumps({"pid": os.getpid(), "start": mod.process_start(os.getpid())}))
    assert mod.RunLock(lock).holder()["alive"] is True


# ── journal ─────────────────────────────────────────────────────────────────

def test_journal_write_is_atomic(tmp_path: Path, monkeypatch):
    """A crash before the rename leaves the previous journal whole."""
    mod = _load_engine_module()
    path = tmp_path / "journal" / "r.json"
    mod.atomic_write_json(path, {"phase": "pull"})

    def boom(*_a, **_k):
        raise OSError("power cut")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        mod.atomic_write_json(path, {"phase": "switch", "pad": "x" * 100000})
    assert json.loads(path.read_text()) == {"phase": "pull"}


def test_env_is_written_in_place(tmp_path: Path):
    mod = _load_engine_module()
    env = tmp_path / ".env"
    env.write_text("A=1\nMC_IMAGE_TAG=0.1.0\nB=2\n")
    ino = env.stat().st_ino
    mod.env_file_set(env, {"MC_IMAGE_TAG": "0.2.0", "NEW": "x"})
    assert env.stat().st_ino == ino
    assert env.read_text() == "A=1\nMC_IMAGE_TAG=0.2.0\nB=2\nNEW=x\n"


# ── crash + resume ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("fault", ["preflight.done", "drain.intent", "drain.done", "fetch.done",
                                   "verify_release.done", "pull.intent", "pull.done", "backup.intent",
                                   "backup.done"])
def test_crash_before_switch_resumes_to_aborted(world: World, fault: str):
    """Sabotage (d): state pull + resume → aborted, no checkout."""
    r = world.run("start", fault=fault)
    assert r.returncode == 137, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] not in ("done", "aborted")
    st = json.loads(world.run("status", "--json").stdout)
    assert st["interrupted"] is True and st["lock"]["held"] is False
    calls_before = len(world.calls())
    r = world.run("resume")
    assert r.returncode == 3, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "aborted"
    assert world.head_tag() == "v0.1.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert (world.inst / FLEET).read_text() == USER_FLEET
    new_calls = world.calls()[calls_before:]
    assert not any(c[:1] == ["compose"] and c[3:4] in (["up"], ["pull"], ["stop"]) for c in new_calls)
    assert world.get_state().get("drain") is None


@pytest.mark.parametrize("fault", ["switch.intent", "switch.worker_stopped", "switch.parked",
                                   "switch.checked_out", "switch.fleet_back", "switch.pins",
                                   "switch.done", "up.intent", "up.done", "verify.intent"])
def test_crash_after_switch_resumes_to_done(world: World, fault: str):
    """Sabotage (b): drop the fleet copy → a kill between parking and copying back loses it."""
    r = world.run("start", fault=fault)
    assert r.returncode == 137, r.stdout + r.stderr
    r = world.run("resume")
    assert r.returncode == 0, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "done"
    assert world.head_tag() == "v0.2.0"
    assert (world.inst / FLEET).read_text() == USER_FLEET
    assert world.env_value("MC_IMAGE_TAG") == "0.2.0"
    assert world.env_value("DB_PASSWORD") == "keep-me"
    assert j["resumes"] and j["resumes"][0]["phase"] in ("switch", "up", "verify")
    # The resume ran the target's engine, too.
    copy = os.path.realpath(world.state / f"run-{j['run_id']}" / "mc_update.py")
    assert [e for e in j["events"] if e["event"] == "resume"][0]["engine"] == copy


def test_crash_on_the_way_back_resumes_the_way_back(world: World):
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    r = world.run("start", fault="rolling_back.checked_out")
    assert r.returncode == 137
    assert world.journal()["phase"] == "rolling_back"
    r = world.run("resume")
    assert r.returncode == 1, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "rolled_back"
    assert world.head_tag() == "v0.1.0"
    assert (world.inst / FLEET).read_text() == USER_FLEET


def test_resume_without_an_open_run_does_nothing(world: World):
    assert world.run("start").returncode == 0
    n = len(world.calls())
    r = world.run("resume")
    assert r.returncode == 0 and "nothing to resume" in r.stdout
    assert len(world.calls()) == n


def test_resume_from_another_install_is_refused(world: World):
    """The journal lives in the user's home, shared by every install of that
    user: a resume from another folder must not switch that folder's code."""
    assert world.run("start", fault="up.intent").returncode == 137
    other = world.tmp / "other-install"
    other.mkdir()
    n = len(world.calls())
    r = world.run("resume", "--repo", str(other))
    assert r.returncode == 9, r.stdout + r.stderr
    assert "another Mission Control install" in r.stderr
    assert world.journal()["phase"] == "up"
    assert len(world.calls()) == n
    r = world.run("rollback", "--repo", str(other))
    assert r.returncode == 9, r.stdout + r.stderr
    assert world.run("resume").returncode == 0
    assert world.journal()["phase"] == "done"


def test_preflight_refuses_while_a_run_is_interrupted(world: World):
    assert world.run("start", fault="up.intent").returncode == 137
    r = world.run("start")
    assert r.returncode == 3
    assert "resume it first" in r.stdout


# ── rollback command ────────────────────────────────────────────────────────

def test_rollback_cancels_a_run_waiting_for_quiet(world: World):
    world.patch_state(in_progress=[1])
    env = dict(world.env)
    env["MC_UPDATE_DRAIN_TIMEOUT"] = "60"
    env["MC_UPDATE_POLL_INTERVAL"] = "0.05"
    proc = subprocess.Popen([sys.executable, str(world.inst / "scripts" / "mc_update.py"), "start"],
                            cwd=world.inst, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            js = list((world.state / "journal").glob("*.json")) if (world.state / "journal").exists() else []
            if js and json.loads(js[0].read_text())["phase"] == "drain":
                break
            time.sleep(0.05)
        r = world.run("rollback")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "cancel requested" in r.stdout
        assert proc.wait(timeout=20) == 3
    finally:
        if proc.poll() is None:
            proc.kill()
    assert world.journal()["phase"] == "aborted"
    assert world.head_tag() == "v0.1.0"


def _start_in_background(world: World, extra_env: dict = None) -> subprocess.Popen:
    env = dict(world.env)
    env["MC_UPDATE_POLL_INTERVAL"] = "0.05"
    env.update(extra_env or {})
    return subprocess.Popen([sys.executable, str(world.inst / "scripts" / "mc_update.py"), "start"],
                            cwd=world.inst, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for_phase(world: World, phase: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        js = sorted((world.state / "journal").glob("*.json")) if (world.state / "journal").exists() else []
        if js:
            try:
                if json.loads(js[-1].read_text())["phase"] == phase:
                    return
            except ValueError:
                pass
        time.sleep(0.02)
    raise AssertionError(f"the run never reached phase {phase}")


@pytest.mark.parametrize("phase", ["pull", "backup"])
def test_cancel_in_any_phase_before_the_switch_is_honoured(world: World, phase: str):
    """Regression (review of #868): the cancel was only read in the drain loop —
    a cancel during the download or the backup was answered "ok" and the run
    went on to `done`. Sabotage: drop the cancel checks outside the drain → red."""
    if phase == "pull":
        world.patch_state(pull_sleep=30)
        extra = {}
    else:
        world.patch_state(dump_sleep=1.5)
        extra = {}
    t0 = time.monotonic()
    proc = _start_in_background(world, extra)
    try:
        _wait_for_phase(world, phase)
        r = world.run("rollback")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "cancel requested" in r.stdout
        rc = proc.wait(timeout=25)
        out = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 3, out
    if phase == "pull":
        # A running download is stopped, not waited out (it may take an hour).
        assert time.monotonic() - t0 < 20
    j = world.journal()
    assert j["phase"] == "aborted" and j["failure_kind"] == "cancelled"
    assert world.head_tag() == "v0.1.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["up"], ["stop"]) for c in world.calls())
    assert world.get_state()["drain"] is None


def test_a_normal_run_claims_the_switch_before_it_changes_anything(world: World):
    """The last point before the switch is one atomic decision: whoever claims
    the run's gate first — the cancel or the switch — wins."""
    assert world.run("start").returncode == 0
    j = world.journal()
    gate = world.state / f"run-{j['run_id']}" / "gate"
    assert gate.read_text().strip() == "switch"
    claimed = [i for i, e in enumerate(j["events"]) if e["event"] == "claimed"]
    first_switch = next(i for i, e in enumerate(j["events"]) if e["phase"] == "switch")
    assert claimed and claimed[0] < first_switch


def test_cancel_after_the_switch_was_claimed_is_answered_too_late(world: World):
    """A cancel that loses the race against the switch must not say "ok":
    the run is past the point of no return and finishes or rolls back."""
    r = world.run("start", fault="backup.done")
    assert r.returncode == 137
    j = world.journal()
    mod = _load_engine_module()
    run_dir = world.state / f"run-{j['run_id']}"
    assert mod.claim_gate(run_dir, "switch") == "switch"
    # A live run that has claimed the switch but not yet journaled it.
    (world.state / "lock").write_text(json.dumps({"pid": os.getpid(), "start": mod.process_start(os.getpid()),
                                                  "run_id": j["run_id"]}))
    r = world.run("rollback")
    assert r.returncode == 23, r.stdout + r.stderr
    assert "too late" in r.stderr
    assert "cancel requested" not in r.stdout
    assert (run_dir / "gate").read_text().strip() == "switch"


def test_gate_is_won_by_exactly_one_side(tmp_path: Path):
    mod = _load_engine_module()
    assert mod.claim_gate(tmp_path / "a", "cancel") == "cancel"
    assert mod.claim_gate(tmp_path / "a", "switch") == "cancel"
    assert mod.claim_gate(tmp_path / "b", "switch") == "switch"
    assert mod.claim_gate(tmp_path / "b", "cancel") == "switch"
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == ["gate"]


def test_rollback_of_a_done_run_with_a_new_schema_restores_its_backup(world: World):
    """The manual "back to the previous version": the run's own pre-update
    backup comes back (R2), then the old code."""
    inode = (world.inst / ".env").stat().st_ino
    assert world.run("start").returncode == 0
    assert world.get_state()["schema"] == "0002"
    r = world.run("rollback")
    assert r.returncode == 5, r.stdout + r.stderr
    _assert_r2(world, r, inode)
    assert "everything written since" in r.stdout


def test_rollback_of_a_done_run_without_its_backup_is_refused_untouched(world: World):
    assert world.run("start").returncode == 0
    for f in world.backups.glob("mc_backup_preupdate_*"):
        f.unlink()
    n = len(world.calls())
    r = world.run("rollback")
    assert r.returncode == 9, r.stdout + r.stderr
    assert "is missing" in r.stderr
    assert world.journal()["phase"] == "done"
    assert world.head_tag() == "v0.2.0"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["stop"], ["up"]) for c in world.calls()[n:])


def test_only_the_latest_update_can_be_undone(world: World):
    """Undoing an older run would restore a database from before a newer
    update and check out code that does not match it."""
    assert world.run("start").returncode == 0
    first = world.journal()["run_id"]
    # A newer release and a second update on top.
    _git(world.up, "commit", "-q", "--allow-empty", "-m", "v0.3.0")
    _git(world.up, "tag", "v0.3.0")
    world.release_json.write_text(json.dumps({"tag_name": "v0.3.0", "draft": False, "prerelease": False}))
    s = world.get_state()
    s["schema_for_tag"]["0.3.0"] = "0002"
    world.set_state(s)
    time.sleep(1.1)   # run ids and backup names carry the second
    assert world.run("start").returncode == 0, world.journal()
    n = len(world.calls())
    r = world.run("rollback", "--run", first)
    assert r.returncode == 9, r.stdout + r.stderr
    assert "not the latest" in r.stderr
    assert world.head_tag() == "v0.3.0"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["stop"], ["up"]) for c in world.calls()[n:])


def test_rollback_of_a_done_run_with_unchanged_schema_goes_back(world: World):
    world.patch_state(schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    # A release without a migration: its head is the old one.
    _git(world.up, "tag", "-d", "v0.2.0")
    (world.up / "backend" / "alembic" / "versions" / "0002_more.py").unlink()
    _git(world.up, "add", "-A")
    _git(world.up, "commit", "-q", "-m", "v0.2.0 without migration")
    _git(world.up, "tag", "v0.2.0")
    assert world.run("start").returncode == 0
    r = world.run("rollback")
    # Its own exit code: a requested way back that worked is not "update failed".
    assert r.returncode == 5, r.stdout + r.stderr
    assert world.journal()["phase"] == "rolled_back"
    assert world.head_tag() == "v0.1.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert world.get_state()["containers"]["frontend"]["image"] == "sha256:mc-frontend-0.1.0"


def _release_without_migration(world: World) -> None:
    world.patch_state(schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    _git(world.up, "tag", "-d", "v0.2.0")
    (world.up / "backend" / "alembic" / "versions" / "0002_more.py").unlink()
    _git(world.up, "add", "-A")
    _git(world.up, "commit", "-q", "-m", "v0.2.0 without migration")
    _git(world.up, "tag", "v0.2.0")


def test_requested_rollback_drains_and_waits_for_running_work(world: World):
    """ADR-094 decision 7: every code switch drains first, agent work is never
    cut off — also the manual way back from a finished run."""
    _release_without_migration(world)
    assert world.run("start").returncode == 0
    world.patch_state(in_progress=[1, 1, 0], drain_history=[])
    n = len(world.calls())
    r = world.run("rollback")
    assert r.returncode == 5, r.stdout + r.stderr
    assert "waiting for 1 running task(s)" in r.stdout
    st = world.get_state()
    assert st["drain_history"][0] == "set" and st["drain_history"][-1] == "cleared"
    assert st["drain"] is None
    calls = world.calls()[n:]
    count_idx = max(i for i, c in enumerate(calls) if "in_progress" in c[-1])
    stop_idx = next(i for i, c in enumerate(calls) if c[:1] == ["compose"] and c[3:4] == ["stop"])
    assert count_idx < stop_idx


def test_requested_rollback_with_work_that_never_ends_is_refused_untouched(world: World):
    _release_without_migration(world)
    assert world.run("start").returncode == 0
    world.patch_state(in_progress=[1])
    n = len(world.calls())
    r = world.run("rollback", extra_env={"MC_UPDATE_DRAIN_TIMEOUT": "0.3"})
    assert r.returncode == 9, r.stdout + r.stderr
    assert "still running" in r.stderr
    assert world.journal()["phase"] == "done"
    assert world.head_tag() == "v0.2.0"
    assert world.get_state()["drain"] is None
    assert not any(c[:1] == ["compose"] and c[3:4] in (["stop"], ["up"]) for c in world.calls()[n:])


def test_hanging_up_counts_against_the_same_limit(world: World):
    """`compose up` blocks until the backend is healthy (depends_on), so its
    time is part of the 12/25-minute limit, not an extra 30 minutes."""
    world.patch_state(up_sleep_for_tag={"0.2.0": 30}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    t0 = time.monotonic()
    r = world.run("start", extra_env={"MC_UPDATE_VERIFY_TIMEOUT": "1.5"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert time.monotonic() - t0 < 20
    assert world.journal()["phase"] == "rolled_back"


def test_arm64_gets_the_long_limit_on_every_os(world: World, monkeypatch):
    mod = _load_engine_module()
    monkeypatch.delenv("MC_UPDATE_VERIFY_TIMEOUT", raising=False)
    for system, machine, want in (("Linux", "aarch64", 1500.0), ("Darwin", "arm64", 1500.0),
                                  ("Linux", "x86_64", 720.0)):
        monkeypatch.setattr(mod.platform, "system", lambda s=system: s)
        monkeypatch.setattr(mod.platform, "machine", lambda m=machine: m)
        assert mod.Engine(world.inst, world.state).verify_timeout == want, (system, machine)


def test_resume_after_switch_puts_a_lost_fleet_file_back(world: World):
    """Something removed the fleet file after the switch (an operator, a
    `git clean`) — resume writes it back before `up` recreates anything."""
    assert world.run("start", fault="up.intent").returncode == 137
    (world.inst / FLEET).unlink()
    r = world.run("resume")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (world.inst / FLEET).read_text() == USER_FLEET


def test_private_copies_go_with_the_run(world: World):
    """ADR-094 decision 4: the run's .env copy (secrets) is removed with the
    run — right away once it is not needed, after 14 days for a done run."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0001"})
    assert world.run("start").returncode == 1
    j = world.journal()
    rd = world.state / f"run-{j['run_id']}"
    assert not (rd / "env.before").exists() and not (rd / "fleet.before.yml").exists()
    # A done run keeps them for its 14-day way back …
    world.patch_state(health_for_tag={}, schema_for_tag={"0.1.0": "0001", "0.2.0": "0002"})
    assert world.run("start").returncode == 0
    j2 = world.journal()
    rd2 = world.state / f"run-{j2['run_id']}"
    assert (rd2 / "env.before").exists()
    # … and loses them once that window is over.
    path = world.state / "journal" / f"{j2['run_id']}.json"
    data = json.loads(path.read_text())
    data["finished_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    world.run("start")
    assert not (rd2 / "env.before").exists()


def test_unreadable_journal_is_a_clean_failed_check(world: World):
    (world.state / "journal").mkdir(parents=True)
    (world.state / "journal" / "20260101T000000Z-abcdef.json").write_text("{ torn")
    r = world.run("preflight", "--json")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    checks = json.loads(r.stdout)["checks"]
    assert any(c["id"] == "lock" and c["status"] == "fail" and "cannot be read" in c["message"] for c in checks)
    r = world.run("status")
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "cannot be read" in r.stderr


def test_failure_while_preparing_the_way_back_aborts(world: World):
    """No copy of .env can be made → the switch never starts → aborted, not
    "rollback failed" (nothing was changed). The fake backup.sh locks .env
    right after the backup, i.e. between backup and switch."""
    world.patch_state(lock_env_after_dump=True)
    r = world.run("start")
    (world.inst / ".env").chmod(0o600)
    assert r.returncode == 3, r.stdout + r.stderr
    j = world.journal()
    assert j["phase"] == "aborted"
    assert world.head_tag() == "v0.1.0"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["stop"], ["up"]) for c in world.calls())


def test_status_json_is_filtered(world: World):
    """What the bridge hands to the UI: no file paths of this machine."""
    home = {"HOME": str(world.tmp)}           # backups live under "home" here
    assert world.run("start", extra_env=home).returncode == 0
    st = json.loads(world.run("status", "--json", extra_env=home).stdout)
    assert st["phase"] == "done" and st["target"]["tag"] == "v0.2.0"
    assert st["backup"] and "/" not in st["backup"]
    assert "env_copy" not in json.dumps(st)
    assert str(world.tmp) not in json.dumps(st)
    assert any((e["detail"] or "").startswith("~/backups/") for e in st["events"])


def test_contract_names_the_journal_schema():
    out = subprocess.run([sys.executable, str(ENGINE), "--contract"], capture_output=True, text=True)
    assert json.loads(out.stdout)["journal_schema"] == 1
