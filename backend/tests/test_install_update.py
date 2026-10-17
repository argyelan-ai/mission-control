"""``install.sh --update`` tells the truth and always has a fresh backup.

Before: ``--update`` switched the code, ran ``docker compose up -d`` and
printed "Update complete" right away — without a backup, without waiting
for the backend, and while agents were in the middle of tasks. A migration
that failed on start still ended in "complete" with a dead app.

Now, in this order:
  1. tasks in progress → warn; without a terminal the update stops
     (``--ignore-running-tasks`` to go ahead anyway),
  2. ``./backup.sh`` before the switch; a failing backup stops the update
     (``--no-backup`` skips it on purpose),
  3. switch, ``setup.sh``, boot, then wait for ``/health`` as long as the
     backend's ``start_period`` allows,
  4. only a healthy backend is "Update complete"; anything else is
     "Update failed" with the backup file and the commands back.

The tests run the real installer end to end in a scratch checkout: a local
git "upstream" with two releases, a fake ``docker`` in PATH (records calls,
answers psql, can make ``up`` fail) and a ``file://`` health endpoint the
fake ``docker compose up`` creates when the backend is meant to come up.
No network, no real Docker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  manifest)
    tag="${3##*:}"
    for t in ${STUB_PUBLISHED:-}; do [ "$t" = "$tag" ] && exit 0; done
    exit 1 ;;
  compose)
    shift
    case "$1" in
      up)
        touch "$STUB_STATE/up"
        if [ "${STUB_HEALTHY:-1}" = 1 ]; then
          mkdir -p "$STUB_API"; printf '{"status":"ok"}' > "$STUB_API/health"
        fi
        exit "${STUB_UP_RC:-0}" ;;
      exec)
        # `docker compose exec -T` forwards stdin: under `curl | bash` it
        # would eat the rest of the script. Record anything it receives.
        if IFS= read -r -t 1 line; then echo "exec read stdin: $line" >> "$STUB_LOG"; fi
        [ -n "${STUB_DB_DOWN:-}" ] && exit 1
        sql="${!#}"
        case "$sql" in
          *in_progress*) echo "${STUB_IN_PROGRESS:-0}" ;;
          *alembic_version*)
            if [ -e "$STUB_STATE/up" ]; then echo "${STUB_REV_AFTER:-0002}"; else echo "${STUB_REV_BEFORE:-0001}"; fi ;;
        esac
        exit 0 ;;
      logs) echo "fake backend log" ;;
    esac ;;
esac
exit 0
"""

# Committed into every fake release. Records the release it ran on, so the
# test can prove the backup happened BEFORE the code switch.
FAKE_BACKUP = r"""#!/usr/bin/env bash
echo "backup.sh on $(git describe --tags --exact-match HEAD 2>/dev/null || echo none)" >> "$STUB_LOG"
if IFS= read -r -t 1 line; then echo "backup.sh read stdin: $line" >> "$STUB_LOG"; fi
if [ "${STUB_BACKUP_RC:-0}" != 0 ]; then echo "pg_dump: connection refused" >&2; exit "$STUB_BACKUP_RC"; fi
case "${STUB_BACKUP_STYLE:-old}" in
  old) dir=backups ;;                      # backup.sh up to 0.3.x
  new)                                     # MC_BACKUP_DIR: env, then .env, default ~/mc-backups
    dir="${MC_BACKUP_DIR:-$(grep -E '^MC_BACKUP_DIR=' .env | tail -n 1 | cut -d= -f2-)}"
    dir="${dir:-$HOME/mc-backups}"; dir="${dir/#\~/$HOME}" ;;
  elsewhere) dir="$STUB_BACKUP_ELSEWHERE" ;;
esac
mkdir -p "$dir"
f="$dir/mc_backup_$(date +%Y-%m-%d_%H-%M-%S).sql.gz"
echo dump > "$f"
echo "Database backup: $f"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def world(tmp_path: Path):
    """An upstream with releases v0.1.0 + v0.2.0, an install on v0.1.0."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    (up / "docker-compose.yml").write_text("services: {}\n")
    (up / "setup.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (up / "backup.sh").write_text(FAKE_BACKUP)
    (up / "setup.sh").chmod(0o755)
    (up / "backup.sh").chmod(0o755)
    versions = up / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    for i, tag in enumerate(["v0.1.0", "v0.2.0"], start=1):
        (versions / f"{i:04d}_m.py").write_text(f'revision: str = "{i:04d}"\n')
        _git(up, "add", "-A")
        _git(up, "commit", "-q", "-m", tag)
        _git(up, "tag", tag)

    inst = tmp_path / "mission-control"
    _git(tmp_path, "clone", "-q", "--branch", "v0.1.0", str(up), str(inst))
    (inst / ".env").write_text("MC_CHANNEL=stable\nMC_IMAGE_TAG=0.1.0\nMC_AGENT_IMAGE_TAG=0.1.0\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    api_json = tmp_path / "latest.json"
    api_json.write_text(json.dumps({"tag_name": "v0.2.0"}))
    log = tmp_path / "calls.log"
    log.write_text("")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MC_REPO_URL": str(up),
        "MC_RELEASE_API": api_json.as_uri(),
        "MC_API_URL": (tmp_path / "api").as_uri(),
        "STUB_API": str(tmp_path / "api"),
        "STUB_STATE": str(state),
        "STUB_LOG": str(log),
        "STUB_PUBLISHED": "0.2.0",
        "COMPOSE_PROJECT_NAME": "",
        "MC_HEALTH_TIMEOUT": "1",
        "MC_HEALTH_INTERVAL": "0.2",
        "HOME": str(tmp_path / "home"),
    }
    env.pop("MC_BACKUP_DIR", None)

    home = tmp_path / "home"
    home.mkdir()

    def run(*flags: str, stdin_data: str | None = None, **stub: str):
        # start_new_session: no controlling terminal → /dev/tty is not
        # usable, exactly like cron or `curl | bash` without a terminal.
        r = subprocess.run(
            ["bash", str(INSTALL_SH), "--update", *flags],
            cwd=inst, env={**env, **stub}, capture_output=True, text=True,
            timeout=120, start_new_session=True,
            input=stdin_data if stdin_data is not None else "",
        )
        r.out = r.stdout + r.stderr  # type: ignore[attr-defined]
        r.calls = log.read_text()  # type: ignore[attr-defined]
        return r

    def head_tag() -> str:
        return _git(inst, "describe", "--tags", "--exact-match", "HEAD")

    return run, head_tag, inst


def _backups(inst: Path) -> list[str]:
    d = inst / "backups"
    return sorted(p.name for p in d.glob("mc_backup_*")) if d.is_dir() else []


# ── success only when healthy ───────────────────────────────────────────────

def test_healthy_update_backs_up_first_then_reports_complete(world):
    run, head_tag, inst = world
    r = run()
    assert r.returncode == 0, r.out
    assert "Update complete" in r.out
    assert head_tag() == "v0.2.0"
    assert "MC_IMAGE_TAG=0.2.0" in (inst / ".env").read_text()
    # the backup ran on the OLD release, i.e. before the switch
    assert "backup.sh on v0.1.0" in r.calls
    assert r.calls.index("backup.sh on") < r.calls.index("docker compose up")
    (name,) = _backups(inst)
    assert name in r.out, "the success message names the backup"
    assert "Update failed" not in r.out


def test_backend_never_healthy_is_failed_with_backup_and_way_back(world):
    run, head_tag, inst = world
    r = run(STUB_HEALTHY="0")
    assert r.returncode != 0
    assert "Update complete" not in r.out
    assert "Update failed" in r.out
    (name,) = _backups(inst)
    assert name in r.out, "the failure names the backup taken before the switch"
    assert "git checkout -q v0.1.0" in r.out
    assert "cp .env.backup-pre-update .env" in r.out
    # the schema moved (0001 → 0002): the way back restores the database
    assert re.search(r"\./backup\.sh restore \S*" + re.escape(name), r.out)
    # the copy of the old .env is the pre-update one and owner-only
    copy = inst / ".env.backup-pre-update"
    assert "MC_IMAGE_TAG=0.1.0" in copy.read_text()
    assert copy.stat().st_mode & 0o077 == 0


def test_failed_update_with_unchanged_schema_needs_no_restore(world):
    run, _head, _inst = world
    r = run(STUB_HEALTHY="0", STUB_REV_AFTER="0001")
    assert r.returncode != 0 and "Update failed" in r.out
    assert "no restore needed" in r.out
    assert "backup.sh restore" not in r.out


def test_stack_that_does_not_start_is_failed_not_complete(world):
    run, _head, _inst = world
    r = run(STUB_UP_RC="1")
    assert r.returncode != 0
    assert "Update failed" in r.out
    assert "Update complete" not in r.out


# ── backup gate ─────────────────────────────────────────────────────────────

def test_failing_backup_stops_before_anything_changes(world):
    run, head_tag, inst = world
    r = run(STUB_BACKUP_RC="1")
    assert r.returncode != 0
    assert head_tag() == "v0.1.0", "no code switch without a backup"
    assert "docker compose up" not in r.calls
    assert "MC_IMAGE_TAG=0.1.0" in (inst / ".env").read_text()
    assert "backup failed" in r.out.lower()
    assert "--no-backup" in r.out
    assert "nothing was changed" in r.out
    assert "Update complete" not in r.out


def test_no_backup_skips_it_on_purpose(world):
    run, head_tag, inst = world
    r = run("--no-backup")
    assert r.returncode == 0, r.out
    assert "backup.sh" not in r.calls
    assert _backups(inst) == []
    assert "WITHOUT a backup" in r.out
    assert head_tag() == "v0.2.0"


def test_no_backup_failure_says_there_is_no_backup(world):
    run, _head, _inst = world
    r = run("--no-backup", STUB_HEALTHY="0")
    assert "Update failed" in r.out
    assert "skipped with --no-backup" in r.out


# ── running tasks ───────────────────────────────────────────────────────────

def test_running_tasks_without_a_terminal_stop_the_update(world):
    run, head_tag, inst = world
    r = run(STUB_IN_PROGRESS="2")
    assert r.returncode != 0
    assert "2 task(s) are in progress" in r.out
    assert "--ignore-running-tasks" in r.out
    assert head_tag() == "v0.1.0"
    assert "backup.sh" not in r.calls, "stops before the backup"
    assert "docker compose up" not in r.calls


def test_ignore_running_tasks_goes_ahead(world):
    run, head_tag, _inst = world
    r = run("--ignore-running-tasks", STUB_IN_PROGRESS="2")
    assert r.returncode == 0, r.out
    assert head_tag() == "v0.2.0"


def test_no_running_tasks_asks_nothing(world):
    run, head_tag, _inst = world
    r = run(STUB_IN_PROGRESS="0")
    assert r.returncode == 0, r.out
    assert "in progress" not in r.out


# ── the wait outlasts the backend's start period ────────────────────────────

def _seconds(value: str) -> int:
    return sum(int(a) * {"h": 3600, "m": 60, "s": 1}[u] for a, u in re.findall(r"(\d+)([hms])", value))


def test_default_health_wait_outlasts_the_backend_start_period():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    start_period = _seconds(str(compose["services"]["backend"]["healthcheck"]["start_period"]))
    m = re.search(r'^MC_HEALTH_TIMEOUT="\$\{MC_HEALTH_TIMEOUT:-(\d+)\}"', INSTALL_SH.read_text(), re.M)
    assert m, "install.sh must define a default MC_HEALTH_TIMEOUT"
    assert int(m.group(1)) > start_period


def test_fresh_install_and_update_share_the_health_wait():
    text = INSTALL_SH.read_text()
    update = text.split('if [ "$UPDATE" = 1 ]; then', 1)[1].split("\n  exit 0\nfi", 1)[0]
    fresh = text.split("# ── 4. Boot", 1)[1]
    assert "mc_wait_healthy" in update and "mc_wait_healthy" in fresh
    assert update.index('boot_stack "$CHANNEL"') < update.index("mc_wait_healthy")
    assert update.index("mc_wait_healthy") < update.index("Update complete")


# ── where backup.sh wrote: found, never guessed wrong ──────────────────────

def _files(d: Path) -> list[Path]:
    return sorted(d.glob("mc_backup_*")) if d.is_dir() else []


def test_backup_in_the_new_default_folder_is_named_on_failure(world):
    """backup.sh with MC_BACKUP_DIR writes to ~/mc-backups by default."""
    run, _head, inst = world
    r = run(STUB_HEALTHY="0", STUB_BACKUP_STYLE="new")
    assert "Update failed" in r.out
    (f,) = _files(inst.parent / "home" / "mc-backups")
    assert f"./backup.sh restore {f}" in r.out
    assert "no backup from this run" not in r.out


def test_backup_dir_from_dotenv_relative_to_the_install_is_found(world):
    run, _head, inst = world
    with (inst / ".env").open("a") as fh:
        fh.write("MC_BACKUP_DIR=../my-backups\n")
    r = run(STUB_BACKUP_STYLE="new")
    assert r.returncode == 0, r.out
    (f,) = _files(inst.parent / "my-backups")
    assert f.name in r.out


def test_backup_that_ran_but_cannot_be_found_is_not_called_missing(world, tmp_path):
    """A backup that succeeded must never be reported as 'no backup'."""
    run, _head, _inst = world
    r = run(STUB_HEALTHY="0", STUB_BACKUP_STYLE="elsewhere",
            STUB_BACKUP_ELSEWHERE=str(tmp_path / "odd-place"))
    assert "Update failed" in r.out
    assert "no backup from this run" not in r.out
    assert "none" not in r.out.split("Backup", 1)[1].split("\n", 1)[0]
    assert "./backup.sh restore <" in r.out, "points at restoring the file from backup.sh's output"
    assert "backup.sh" in r.out and "output above" in r.out


def test_no_backup_failure_with_changed_schema_says_there_is_no_backup(world):
    run, _head, _inst = world
    r = run("--no-backup", STUB_HEALTHY="0")
    assert "no backup from this run (--no-backup)" in r.out


# ── stdin: `curl | bash` must not lose the script to docker exec ───────────

def test_db_queries_and_backup_do_not_read_the_installers_stdin(world):
    run, _head, _inst = world
    r = run(stdin_data="echo rest-of-the-script\n")
    assert r.returncode == 0, r.out
    assert "read stdin" not in r.calls, r.calls


# ── one clear message when the database is down ────────────────────────────

def test_database_down_stops_once_before_the_backup(world):
    run, head_tag, _inst = world
    r = run(STUB_DB_DOWN="1")
    assert r.returncode != 0
    assert "continuing" not in r.out
    assert "database is not reachable" in r.out
    assert "docker compose up -d db" in r.out and "--no-backup" in r.out
    assert "backup.sh on" not in r.calls
    assert head_tag() == "v0.1.0"


def test_database_down_with_no_backup_goes_ahead(world):
    run, head_tag, _inst = world
    r = run("--no-backup", STUB_DB_DOWN="1")
    assert r.returncode == 0, r.out
    assert head_tag() == "v0.2.0"


# ── MC_HEALTH_TIMEOUT is checked ───────────────────────────────────────────

def test_health_timeout_that_is_not_a_number_is_a_clear_error(world):
    run, head_tag, _inst = world
    r = run(MC_HEALTH_TIMEOUT="5min")
    assert r.returncode != 0
    assert "MC_HEALTH_TIMEOUT" in r.out and "whole number of seconds" in r.out
    assert "syntax error" not in r.out
    assert head_tag() == "v0.1.0"
