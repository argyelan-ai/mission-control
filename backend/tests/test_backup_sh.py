"""backup.sh never destroys data it cannot replace.

The old restore dropped the database *before* it looked at the archive: a
truncated ``.sql.gz`` left an empty database behind. These tests drive the
real ``backup.sh`` against a throwaway ``postgres:16-alpine`` container.

``docker compose`` is replaced by a small shim on ``PATH``: ``exec -T db …``
is forwarded to the throwaway container, every other compose call (stop,
start, ps, config) is only recorded in a log, so the tests can assert the
order "check archive → safety copy → stop services → drop". Nothing here
touches a running Mission Control stack.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP_SH = ROOT / "backup.sh"
REAL_DOCKER = shutil.which("docker")


def _docker_ok() -> bool:
    if REAL_DOCKER is None or shutil.which("bash") is None:
        return False
    try:
        return subprocess.run([REAL_DOCKER, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.skipif(not _docker_ok(), reason="needs bash and a docker daemon"),
    # Only ever touches the container this module starts (mc-test-backup-<random>).
    pytest.mark.throwaway_docker,
]

SHIM = r"""#!/bin/bash
# Test shim for `docker compose`: db exec goes to the throwaway container,
# everything else is only logged. Any other docker call is refused, so the
# script under test can never reach a real container.
if [[ "${1:-}" == "compose" ]]; then
    shift
    echo "compose $*" >> "$FAKE_LOG"
    case "${1:-}" in
        exec)
            shift
            [[ "${1:-}" == "-T" ]] && shift
            shift   # service name (db)
            exec "$REAL_DOCKER" exec -i "$PG_CONTAINER" "$@"
            ;;
        ps)
            printf 'db\nbackend\nmc-worker\n'
            exit 0
            ;;
        config)
            printf 'db\nbackend\nmc-worker\nfrontend\n'
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi
echo "test shim: refusing 'docker $*'" >&2
exit 97
"""


@pytest.fixture(scope="module")
def pg_container():
    name = f"mc-test-backup-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [REAL_DOCKER, "run", "--rm", "-d", "--name", name,
         "-e", "POSTGRES_USER=mc", "-e", "POSTGRES_DB=mission_control",
         "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16-alpine"],
        check=True, capture_output=True, timeout=180,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            ok = subprocess.run(
                [REAL_DOCKER, "exec", name, "psql", "-U", "mc", "-d", "mission_control",
                 "-tAc", "select 1"], capture_output=True, text=True,
            )
            if ok.returncode == 0 and ok.stdout.strip() == "1":
                break
            time.sleep(0.5)
        else:
            pytest.fail("throwaway postgres did not come up")
        yield name
    finally:
        subprocess.run([REAL_DOCKER, "rm", "-f", name], capture_output=True)


def _psql(container: str, sql: str, db: str = "mission_control") -> subprocess.CompletedProcess:
    return subprocess.run(
        [REAL_DOCKER, "exec", "-i", container, "psql", "-U", "mc", "-d", db, "-v", "ON_ERROR_STOP=1",
         "-tAc", sql], capture_output=True, text=True,
    )


def _rows(container: str) -> int | None:
    r = _psql(container, "select count(*) from notes")
    return int(r.stdout.strip()) if r.returncode == 0 else None


@pytest.fixture
def env(tmp_path: Path, pg_container: str):
    """A fake repo dir (holding a copy of backup.sh), HOME, backup dir and shim."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(BACKUP_SH, repo / "backup.sh")
    home = tmp_path / "home"
    (home / ".mc" / "agents" / "a1").mkdir(parents=True)
    (home / ".mc" / "agents" / "a1" / "agent.env").write_text("TOKEN=original\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(SHIM)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "compose.log"
    log.write_text("")

    # fresh database with known content
    _psql(pg_container, "DROP DATABASE IF EXISTS mission_control WITH (FORCE)", db="postgres")
    _psql(pg_container, "CREATE DATABASE mission_control OWNER mc", db="postgres")
    r = _psql(pg_container, "create table notes(id int primary key, body text);"
                            "insert into notes select g, 'n'||g from generate_series(1,5) g")
    assert r.returncode == 0, r.stderr

    run_env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(home),
        "REAL_DOCKER": REAL_DOCKER,
        "PG_CONTAINER": pg_container,
        "FAKE_LOG": str(log),
        "MC_BACKUP_DIR": str(tmp_path / "mc-backups"),
    }
    run_env.pop("MC_BACKUP_KEEP", None)
    run_env.pop("MC_BACKUP_PASSPHRASE", None)

    class E:
        pass

    e = E()
    e.repo, e.home, e.log, e.env, e.pg = repo, home, log, run_env, pg_container
    e.backups = tmp_path / "mc-backups"

    def run(*args: str, stdin: str = "y\n", extra: dict | None = None):
        return subprocess.run(
            ["bash", str(repo / "backup.sh"), *args], cwd=repo, env={**run_env, **(extra or {})},
            input=stdin, capture_output=True, text=True, timeout=180,
        )

    e.run = run
    return e


def _make_backup(e) -> Path:
    r = e.run()
    assert r.returncode == 0, r.stdout + r.stderr
    dumps = sorted(e.backups.glob("mc_backup_2*.sql.gz"))
    assert dumps, f"no dump written: {r.stdout}{r.stderr}"
    return dumps[-1]


# ── restore: check first, change later ──────────────────────────────────────

def _pg_dump_gz(e) -> bytes:
    """A real dump of the throwaway DB, made without backup.sh."""
    out = subprocess.run(
        [REAL_DOCKER, "exec", e.pg, "pg_dump", "-U", "mc", "mission_control"],
        check=True, capture_output=True,
    ).stdout
    return gzip.compress(out)


def test_truncated_archive_aborts_and_leaves_db_unchanged(env):
    data = _pg_dump_gz(env)
    broken = env.repo / "broken.sql.gz"
    broken.write_bytes(data[: len(data) // 2])

    r = env.run("restore", str(broken))

    assert _rows(env.pg) == 5, "database must be untouched"
    assert r.returncode != 0
    assert "damaged" in (r.stdout + r.stderr).lower()
    log = env.log.read_text()
    assert "stop" not in log and "DROP" not in log


def test_dump_without_completion_footer_aborts(env):
    """A pg_dump that died mid-way but whose gzip stream closed cleanly."""
    partial = env.repo / "partial.sql.gz"
    with gzip.open(partial, "wt") as fh:
        fh.write("--\n-- PostgreSQL database dump\n--\n\nCREATE TABLE half (id int);\n")

    r = env.run("restore", str(partial))

    assert r.returncode != 0
    assert _rows(env.pg) == 5


def test_restore_makes_safety_copy_then_stops_backend_and_worker(env):
    dump = _make_backup(env)
    _psql(env.pg, "insert into notes select g, 'new' from generate_series(6,9) g")
    (env.home / ".mc" / "agents" / "a1" / "agent.env").write_text("TOKEN=changed\n")
    env.log.write_text("")

    r = env.run("restore", str(dump), "--yes", stdin="")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(env.pg) == 5
    assert (env.home / ".mc" / "agents" / "a1" / "agent.env").read_text() == "TOKEN=original\n"

    safety = list(env.backups.glob("mc_backup_pre-restore_*.sql.gz"))
    assert len(safety) == 1, "the current state must be saved before anything is dropped"
    with gzip.open(safety[0], "rt") as fh:
        body = fh.read()
    assert "\tnew\n" in body, "safety copy holds the state from before the restore"
    assert list(env.backups.glob("mc_data_pre-restore_*.tar.gz"))

    lines = env.log.read_text().splitlines()
    idx_stop_backend = next(i for i, ln in enumerate(lines) if ln.startswith("compose stop") and "backend" in ln)
    idx_stop_worker = next(i for i, ln in enumerate(lines) if ln.startswith("compose stop") and "mc-worker" in ln)
    idx_drop = next(i for i, ln in enumerate(lines) if "DROP DATABASE" in ln)
    idx_dump = next(i for i, ln in enumerate(lines) if "pg_dump" in ln)
    assert idx_dump < idx_stop_backend < idx_drop
    assert idx_stop_worker < idx_drop
    assert any(ln.startswith("compose start") and "mc-worker" in ln for ln in lines[idx_drop:])


def test_newest_damaged_archive_is_never_picked(env):
    good = _make_backup(env)
    time.sleep(1.1)
    newer = env.backups / "mc_backup_2099-01-01_00-00-00.sql.gz"
    newer.write_bytes(good.read_bytes()[:100])
    _psql(env.pg, "insert into notes values (42, 'later')")

    r = env.run("restore", "--yes", stdin="")

    assert r.returncode == 0, r.stdout + r.stderr
    assert good.name in r.stdout
    assert _rows(env.pg) == 5


def test_damaged_data_archive_aborts_before_any_change(env):
    dump = _make_backup(env)
    data_archive = Path(str(dump).replace("mc_backup_", "mc_data_").replace(".sql.gz", ".tar.gz"))
    data_archive.write_bytes(data_archive.read_bytes()[:50])
    _psql(env.pg, "insert into notes values (42, 'later')")

    r = env.run("restore", str(dump), "--yes", stdin="")

    assert r.returncode != 0
    assert _rows(env.pg) == 6
    assert "DROP" not in env.log.read_text()


def test_legacy_backups_dir_is_still_read(env):
    """Installations from before MC_BACKUP_DIR keep their ./backups."""
    legacy = env.repo / "backups"
    legacy.mkdir()
    dump = _make_backup(env)
    moved = legacy / dump.name
    shutil.move(dump, moved)
    data_archive = Path(str(dump).replace("mc_backup_", "mc_data_").replace(".sql.gz", ".tar.gz"))
    shutil.move(data_archive, legacy / data_archive.name)

    listing = env.run("list", stdin="")
    assert listing.returncode == 0, listing.stderr
    assert moved.name in listing.stdout

    _psql(env.pg, "insert into notes values (42, 'later')")
    r = env.run("restore", "--yes", stdin="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(env.pg) == 5


# ── backup: honest and private ──────────────────────────────────────────────

def test_backup_files_are_private(env):
    dump = _make_backup(env)
    data_archive = Path(str(dump).replace("mc_backup_", "mc_data_").replace(".sql.gz", ".tar.gz"))
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert stat.S_IMODE(data_archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.backups.stat().st_mode) == 0o700


def test_keep_is_configurable_and_only_counts_daily_backups(env):
    env.backups.mkdir(mode=0o700)
    keepsake = env.backups / "mc_backup_pre-restore_2000-01-01_00-00-00.sql.gz"
    keepsake.write_bytes(b"x")
    for _ in range(3):
        r = env.run(extra={"MC_BACKUP_KEEP": "2"})
        assert r.returncode == 0, r.stdout + r.stderr
        time.sleep(1.1)
    assert len(list(env.backups.glob("mc_backup_2*.sql.gz"))) == 2
    assert len(list(env.backups.glob("mc_data_2*.tar.gz"))) == 2
    assert keepsake.exists(), "rotation must not touch safety copies"


def test_default_keep_is_seven(env):
    env.backups.mkdir(mode=0o700)
    for day in range(1, 8):
        (env.backups / f"mc_backup_2000-01-0{day}_00-00-00.sql.gz").write_bytes(b"x")
    _make_backup(env)
    left = sorted(p.name for p in env.backups.glob("mc_backup_2*.sql.gz"))
    assert len(left) == 7
    assert "mc_backup_2000-01-01_00-00-00.sql.gz" not in left, "the oldest one goes"


def test_failing_mc_data_archive_is_loud(env):
    secret = env.home / ".mc" / "unreadable"
    secret.write_text("x")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("running as root: cannot make a file unreadable")
        r = env.run()
    finally:
        secret.chmod(0o600)
    assert r.returncode != 0
    assert "mc data" in (r.stdout + r.stderr).lower()
    assert not list(env.backups.glob("mc_data_2*.tar.gz")), "no half archive that looks complete"


def test_backup_dir_inside_mc_home_is_refused(env):
    r = env.run(extra={"MC_BACKUP_DIR": str(env.home / ".mc" / "backups")})
    assert r.returncode != 0
    assert not (env.home / ".mc" / "backups").exists() or not list((env.home / ".mc" / "backups").iterdir())


def test_backup_refuses_when_disk_cannot_hold_another_one(env, tmp_path):
    _make_backup(env)
    before = sorted(p.name for p in env.backups.iterdir())
    fake = tmp_path / "fakedf"
    fake.mkdir()
    (fake / "df").write_text(
        "#!/bin/bash\necho 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
        "echo '/dev/fake 100 99 1 99% /'\n"
    )
    (fake / "df").chmod(0o755)
    time.sleep(1.1)

    r = env.run(extra={"PATH": f"{fake}:{env.env['PATH']}"})

    assert r.returncode != 0
    assert "not enough free space" in r.stderr
    assert sorted(p.name for p in env.backups.iterdir()) == before


def test_dir_prints_the_configured_folder_and_reads_env_file(env):
    (env.repo / ".env").write_text("MC_BACKUP_DIR=~/elsewhere\n")
    env_no_var = {k: v for k, v in env.env.items() if k != "MC_BACKUP_DIR"}
    r = subprocess.run(["bash", str(env.repo / "backup.sh"), "dir"], cwd=env.repo, env=env_no_var,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(env.home / "elsewhere")


def test_auto_pick_skips_a_pair_whose_data_archive_is_damaged(env):
    older = _make_backup(env)
    time.sleep(1.1)
    newer = _make_backup(env)
    assert newer != older
    newer_data = Path(str(newer).replace("mc_backup_", "mc_data_").replace(".sql.gz", ".tar.gz"))
    newer_data.write_bytes(newer_data.read_bytes()[:50])
    _psql(env.pg, "insert into notes values (42, 'later')")

    r = env.run("restore", "--yes", stdin="")

    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Database restored from: {older}" in r.stdout
    assert _rows(env.pg) == 5


# ── review fixes: modes, disk, old installs, transaction ────────────────────

def test_restore_keeps_the_file_modes_from_the_archive(env):
    """backup.sh runs under umask 077; extraction must not make ~/.mc private.

    Containers with another uid read shared files (e.g. screenshots) after a
    restore — a 0644 file must come back 0644, a 0755 folder 0755.
    """
    shared = env.home / ".mc" / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    (shared / "new.txt").write_text("a")
    (shared / "new.txt").chmod(0o644)
    kept = env.home / ".mc" / "kept.txt"
    kept.write_text("b")
    kept.chmod(0o644)
    dump = _make_backup(env)
    shutil.rmtree(shared)
    kept.write_text("changed")
    kept.chmod(0o644)

    r = env.run("restore", str(dump), "--yes", "--no-safety-copy", stdin="")

    assert r.returncode == 0, r.stdout + r.stderr
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert stat.S_IMODE((shared / "new.txt").stat().st_mode) == 0o644
    assert kept.read_text() == "b"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o644


def _fake_df(tmp_path: Path, free_kb: int) -> Path:
    fake = tmp_path / "fakedf"
    fake.mkdir(exist_ok=True)
    (fake / "df").write_text(
        "#!/bin/bash\necho 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
        f"echo '/dev/fake 999999999 1 {free_kb} 99% /'\n"
    )
    (fake / "df").chmod(0o755)
    return fake


def test_first_backup_still_keeps_a_free_space_reserve(env, tmp_path):
    """No earlier backup to measure: the reserve alone must still be checked."""
    fake = _fake_df(tmp_path, 1024 * 1024)  # 1 GB free, default reserve is larger

    r = env.run(extra={"PATH": f"{fake}:{env.env['PATH']}"})

    assert r.returncode != 0
    assert "not enough free space" in r.stderr
    assert not env.backups.exists() or not list(env.backups.glob("mc_*"))


def test_reserve_is_added_to_the_size_of_the_last_backup(env, tmp_path):
    _make_backup(env)
    before = sorted(p.name for p in env.backups.iterdir())
    fake = _fake_df(tmp_path, 3 * 1024 * 1024)  # room for the tiny backup, not for the reserve
    time.sleep(1.1)

    r = env.run(extra={"PATH": f"{fake}:{env.env['PATH']}", "MC_BACKUP_MIN_FREE_MB": "4096"})
    assert r.returncode != 0
    assert "not enough free space" in r.stderr
    assert sorted(p.name for p in env.backups.iterdir()) == before

    r = env.run(extra={"PATH": f"{fake}:{env.env['PATH']}", "MC_BACKUP_MIN_FREE_MB": "1024"})
    assert r.returncode == 0, r.stdout + r.stderr


def test_rotation_counts_the_old_backups_folder(env):
    """Old ./backups plus a new folder must not double the disk use."""
    legacy = env.repo / "backups"
    legacy.mkdir()
    for day in (1, 2, 3):
        (legacy / f"mc_backup_2000-01-0{day}_00-00-00.sql.gz").write_bytes(b"x")
        (legacy / f"mc_data_2000-01-0{day}_00-00-00.tar.gz").write_bytes(b"x")
    (legacy / "backup.log").write_text("log\n")
    (legacy / "mc_backup_pre-restore_2000-01-01_00-00-00.sql.gz").write_bytes(b"x")

    r = env.run(extra={"MC_BACKUP_KEEP": "2"})

    assert r.returncode == 0, r.stdout + r.stderr
    left = sorted(p.name for p in legacy.glob("mc_backup_2*.sql.gz"))
    assert left == ["mc_backup_2000-01-03_00-00-00.sql.gz"], left
    assert sorted(p.name for p in legacy.glob("mc_data_2*.tar.gz")) == ["mc_data_2000-01-03_00-00-00.tar.gz"]
    assert len(list(env.backups.glob("mc_backup_2*.sql.gz"))) == 1
    assert (legacy / "backup.log").exists()
    assert (legacy / "mc_backup_pre-restore_2000-01-01_00-00-00.sql.gz").exists()


def test_scheduler_that_runs_another_copy_uses_the_install_in_its_working_dir(env, tmp_path):
    """Old schedules run a backup.sh from elsewhere with the install as cwd.

    The old script worked relative to the working directory; settings in that
    install's .env (and its ./backups) must still be found.
    """
    elsewhere = tmp_path / "checkout"
    elsewhere.mkdir()
    shutil.copy(BACKUP_SH, elsewhere / "backup.sh")
    (env.repo / "docker-compose.yml").write_text("services: {}\n")
    (env.repo / ".env").write_text("MC_BACKUP_DIR=~/from-install-env\n")
    env_no_var = {k: v for k, v in env.env.items() if k != "MC_BACKUP_DIR"}

    r = subprocess.run(["bash", str(elsewhere / "backup.sh"), "dir"], cwd=env.repo, env=env_no_var,
                       capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(env.home / "from-install-env")


def test_relative_restore_path_is_taken_from_where_you_stand(env, tmp_path):
    dump = _make_backup(env)
    here = tmp_path / "here"
    (here / "sub").mkdir(parents=True)
    shutil.copy(dump, here / "sub" / "copy.sql.gz")
    _psql(env.pg, "insert into notes values (42, 'later')")

    r = subprocess.run(["bash", str(env.repo / "backup.sh"), "restore", "sub/copy.sql.gz", "--yes",
                        "--no-safety-copy"], cwd=here, env=env.env, input="",
                       capture_output=True, text=True, timeout=180)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(env.pg) == 5


def test_a_dump_that_fails_while_loading_leaves_no_half_database(env):
    """--single-transaction: a load error must not leave half the tables."""
    bad = env.repo / "fails_late.sql.gz"
    with gzip.open(bad, "wt") as fh:
        fh.write("--\n-- PostgreSQL database dump\n--\n\n"
                 "CREATE TABLE early (id int);\n"
                 "INSERT INTO early VALUES (1);\n"
                 "SELECT * FROM table_that_does_not_exist;\n"
                 "\n--\n-- PostgreSQL database dump complete\n--\n")

    r = env.run("restore", str(bad), "--yes", "--no-safety-copy", stdin="")

    assert r.returncode != 0
    assert "RESTORE FAILED" in r.stderr
    exists = _psql(env.pg, "select count(*) from pg_tables where tablename = 'early'")
    assert exists.returncode == 0, exists.stderr
    assert exists.stdout.strip() == "0", "the part before the error must be rolled back"


def test_backup_dir_equal_to_old_folder_lists_each_backup_once(env):
    dump = _make_backup(env)
    legacy = env.repo / "backups"
    legacy.mkdir()
    shutil.move(dump, legacy / dump.name)

    r = env.run("list", stdin="", extra={"MC_BACKUP_DIR": str(legacy)})

    assert r.returncode == 0, r.stderr
    assert r.stdout.count(dump.name) == 1, r.stdout
