"""``install.sh --update / --rollback / --resume`` — the terminal door to the
update engine (ADR-094 decision 2, update plan PR 2c).

On the stable channel the installer no longer updates by itself: it checks
python3, looks for an unfinished update run, and hands over to
``scripts/mc_update.py`` — the same engine the Settings button starts. The
terminal shows the engine's own lines, so a terminal user sees what the UI
shows and gets the same safety nets (backup, drain, four checks, way back).

Every test runs the real ``install.sh`` with the real engine in the scratch
world of test_mc_update.py (local git upstream, real backup.sh, fake docker,
``file://`` release list and ``/health``). No network, no real Docker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mc_update import FLEET, USER_FLEET, World  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="needs git + bash")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path).build()


def door(w: World, *flags: str, extra: dict = None, tty: str = None, cwd: Path = None,
         stdin: str = "") -> subprocess.CompletedProcess:
    """install.sh as a user runs it: no controlling terminal (like cron or
    ``curl | bash``) unless ``tty`` names a file with the typed answers."""
    env = dict(w.env)
    env.pop("MC_TTY", None)
    if tty is not None:
        answers = w.tmp / "tty"
        answers.write_text(tty)
        env["MC_TTY"] = str(answers)
    if extra:
        env.update(extra)
    r = subprocess.run(["bash", str(INSTALL_SH), *flags], cwd=str(cwd or w.inst), env=env,
                       capture_output=True, text=True, timeout=180, start_new_session=True, input=stdin)
    r.out = r.stdout + r.stderr  # type: ignore[attr-defined]
    return r


def runs(w: World) -> list:
    d = w.state / "journal"
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


# ── --update hands over to the engine ───────────────────────────────────────

def test_update_runs_the_engine_end_to_end(world: World):
    r = door(world, "--update")
    assert r.returncode == 0, r.out
    j = world.journal()
    assert j["phase"] == "done" and j["source"] == "terminal"
    assert world.head_tag() == "v0.2.0"
    assert world.env_value("MC_IMAGE_TAG") == "0.2.0"
    assert (world.inst / FLEET).read_text() == USER_FLEET
    assert "✅ Update complete — release v0.2.0 is running and verified." in r.stdout
    # Only the engine touched Docker: MC images pulled, nothing else.
    pulls = world.compose_calls("pull")
    assert [c[3:] for c in pulls] == [["pull", "backend", "frontend"]], pulls
    ups = world.compose_calls("up")
    assert ups and all(c[3:] == ["up", "-d", "--pull", "never", "backend", "mc-worker", "frontend"] for c in ups)


def _normalise(text: str, w: World) -> list:
    out = []
    for line in text.splitlines():
        line = line.replace(str(w.tmp), "<tmp>")
        line = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{6}", "<run>", line)
        line = re.sub(r"\d{4}-\d{2}-\d{2}[_T]\d{2}[-:]\d{2}[-:]\d{2}Z?", "<time>", line)
        out.append(line)
    return out


def test_terminal_shows_exactly_what_the_engine_says(tmp_path: Path):
    """"Terminal output equivalent": the door adds a line before and after,
    but every line of the engine reaches the terminal unchanged, in order."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = World(tmp_path / "a").build()
    b = World(tmp_path / "b").build()
    via_door = door(a, "--update")
    direct = b.run("start")
    assert via_door.returncode == 0 == direct.returncode, via_door.out + direct.stdout + direct.stderr
    engine_lines = _normalise(direct.stdout, b)
    door_lines = _normalise(via_door.stdout, a)
    assert engine_lines, "the engine said nothing"
    start = door_lines.index(engine_lines[0])
    assert door_lines[start:start + len(engine_lines)] == engine_lines


def test_failed_update_rolls_back_and_says_where_the_logs_are(world: World):
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"})
    r = door(world, "--update")
    assert r.returncode == 1, r.out
    assert world.journal()["phase"] == "rolled_back"
    assert world.head_tag() == "v0.1.0"
    assert "set back to v0.1.0" in r.stdout
    assert "docker compose logs backend" in r.out
    assert "engine.log" in r.out


def test_failed_backup_is_aborted_and_nothing_changed(world: World):
    world.patch_state(dump_fail=True)
    r = door(world, "--update")
    assert r.returncode == 3, r.out
    assert world.journal()["phase"] == "aborted"
    assert world.head_tag() == "v0.1.0" and world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert "nothing was changed" in r.out
    assert "./install.sh --update" in r.out


def test_version_is_passed_to_the_engine(world: World):
    r = door(world, "--update", "--version", "0.2.0")
    assert r.returncode == 0, r.out
    assert world.journal()["target"]["tag"] == "v0.2.0"


def test_already_on_the_latest_release_is_nothing_to_do(world: World):
    assert door(world, "--update").returncode == 0
    n = len(world.calls())
    r = door(world, "--update")
    assert r.returncode == 0, r.out
    assert "already on v0.2.0" in r.out
    assert not any(c[:1] == ["compose"] and c[3:4] in (["up"], ["pull"], ["stop"]) for c in world.calls()[n:])


@pytest.mark.parametrize("flag", ["--no-backup", "--ignore-running-tasks"])
def test_stable_update_never_skips_the_backup_or_the_wait(world: World, flag: str):
    """ADR-094: the backup is mandatory and running work is never cut off —
    the old escape hatches only exist on the edge channel."""
    r = door(world, "--update", flag)
    assert r.returncode != 0
    assert flag in r.out and "--channel edge" in r.out
    assert runs(world) == []
    assert world.head_tag() == "v0.1.0"


def test_managed_install_is_refused_with_the_reason(world: World):
    with (world.inst / ".env").open("a") as f:
        f.write("MC_UPDATE_MANAGED=deploy\n")
    r = door(world, "--update")
    assert r.returncode == 9, r.out
    assert "deploy procedure" in r.out
    assert world.head_tag() == "v0.1.0"


def test_engine_does_not_read_the_installers_stdin(world: World):
    """`curl | bash`: stdin is the rest of the script — the engine and
    everything it starts get /dev/null."""
    r = door(world, "--update", stdin="echo SHOULD-NOT-BE-READ\n" * 1000)
    assert r.returncode == 0, r.out


# ── an unfinished run is found ──────────────────────────────────────────────

def _interrupt(world: World, point: str = "up.intent") -> str:
    r = door(world, "--update", extra={"MC_UPDATE_TEST_FAULT": point})
    assert r.returncode == 137, r.out
    assert "./install.sh --resume" in r.out, "a crashed engine must name the way on"
    return world.journal()["run_id"]


def test_update_refuses_while_a_run_is_interrupted(world: World):
    run_id = _interrupt(world)
    n = len(world.calls())
    r = door(world, "--update")
    assert r.returncode != 0
    assert run_id in r.out and "./install.sh --resume" in r.out
    assert runs(world) == [run_id], "no second run next to an unfinished one"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["up"], ["pull"], ["stop"]) for c in world.calls()[n:])


def test_update_offers_the_resume_at_a_terminal(world: World):
    _interrupt(world)
    r = door(world, "--update", tty="y\n")
    assert r.returncode == 0, r.out
    assert world.journal()["phase"] == "done"
    assert world.head_tag() == "v0.2.0"
    assert len(runs(world)) == 1


def test_update_at_a_terminal_answered_no_changes_nothing(world: World):
    run_id = _interrupt(world)
    r = door(world, "--update", tty="\n")
    assert r.returncode != 0
    assert world.journal(run_id)["phase"] == "up"
    assert runs(world) == [run_id]


def test_resume_finishes_an_interrupted_update(world: World):
    _interrupt(world)
    r = door(world, "--resume")
    assert r.returncode == 0, r.out
    assert world.journal()["phase"] == "done"
    assert "Update complete after resume" in r.stdout
    assert (world.inst / FLEET).read_text() == USER_FLEET


def test_resume_before_the_switch_ends_aborted(world: World):
    _interrupt(world, "pull.done")
    r = door(world, "--resume")
    assert r.returncode == 3, r.out
    assert world.journal()["phase"] == "aborted"
    assert world.head_tag() == "v0.1.0"
    assert "./install.sh --update" in r.out


def test_resume_without_an_open_run_says_so(world: World):
    r = door(world, "--resume")
    assert r.returncode == 0, r.out
    assert "nothing to resume" in r.out


def test_installer_rerun_does_not_boot_a_half_switched_install(world: World):
    """Re-running the plain installer on an existing checkout boots the whole
    stack — never on top of an interrupted update."""
    run_id = _interrupt(world)
    n = len(world.calls())
    r = door(world, "--dir", str(world.inst), "--non-interactive", cwd=world.tmp)
    assert r.returncode != 0
    assert run_id in r.out and "./install.sh --resume" in r.out
    assert not any(c[:1] == ["compose"] and "up" in c for c in world.calls()[n:])


def test_a_failed_way_back_blocks_a_new_update(world: World):
    """rollback_failed ends the run, but the install is in no state for a new
    one: the door sends the operator to --rollback first."""
    world.patch_state(health_for_tag={"0.2.0": "unhealthy"}, restore_fail=True)
    r = door(world, "--update")
    assert r.returncode == 4, r.out
    assert world.journal()["phase"] == "rollback_failed"
    assert "./install.sh --rollback" in r.out
    run_id = world.journal()["run_id"]
    r = door(world, "--update")
    assert r.returncode != 0
    assert "./install.sh --rollback" in r.out
    assert runs(world) == [run_id]
    # The operator fixes the cause and goes back.
    world.patch_state(restore_fail=False)
    r = door(world, "--rollback")
    assert r.returncode == 0, r.out
    assert world.journal()["phase"] == "rolled_back"
    assert world.head_tag() == "v0.1.0"


# ── --rollback ──────────────────────────────────────────────────────────────

def test_rollback_of_a_finished_update_asks_first(world: World):
    assert door(world, "--update").returncode == 0
    n = len(world.calls())
    r = door(world, "--rollback")
    assert r.returncode != 0
    assert "--yes" in r.out and "lost" in r.out
    assert world.journal()["phase"] == "done" and world.head_tag() == "v0.2.0"
    assert not any(c[:1] == ["compose"] and c[3:4] in (["up"], ["stop"]) for c in world.calls()[n:])


def test_rollback_confirmed_at_the_terminal_goes_back(world: World):
    assert door(world, "--update").returncode == 0
    r = door(world, "--rollback", tty="y\n")
    assert r.returncode == 0, r.out
    assert world.journal()["phase"] == "rolled_back"
    assert world.head_tag() == "v0.1.0" and world.env_value("MC_IMAGE_TAG") == "0.1.0"
    assert world.get_state()["schema"] == "0001"
    assert "as requested" in r.stdout


def test_rollback_with_yes_needs_no_terminal(world: World):
    assert door(world, "--update").returncode == 0
    r = door(world, "--rollback", "--yes")
    assert r.returncode == 0, r.out
    assert world.journal()["phase"] == "rolled_back"


def test_rollback_without_any_run_says_so(world: World):
    r = door(world, "--rollback", "--yes")
    assert r.returncode != 0
    assert "nothing to go back to" in r.out
    assert world.head_tag() == "v0.1.0"


def test_door_flags_do_not_mix(world: World):
    for flags in (("--update", "--rollback"), ("--resume", "--version", "v0.2.0"), ("--rollback", "--channel", "edge")):
        r = door(world, *flags)
        assert r.returncode != 0, flags
        assert runs(world) == []


# ── python3 ─────────────────────────────────────────────────────────────────

def test_missing_python_is_explained_before_anything_happens(world: World):
    r = door(world, "--update", extra={"MC_PYTHON": "python3-that-does-not-exist"})
    assert r.returncode != 0
    assert "python3" in r.out and "install" in r.out.lower()
    assert re.search(r"(apt|dnf) install -y python3|xcode-select --install", r.out), r.out
    assert runs(world) == []
    # Only the prerequisite probes reached Docker (`docker info`, `docker compose version`).
    assert all(c in (["info"], ["compose", "version"]) for c in world.calls()), world.calls()


def test_too_old_python_is_explained(world: World):
    old = world.tmp / "python-old"
    old.write_text("#!/bin/sh\nexit 1\n")   # fails the version check like python 3.8 would
    old.chmod(0o755)
    r = door(world, "--update", extra={"MC_PYTHON": str(old)})
    assert r.returncode != 0
    assert "3.9" in r.out
    assert runs(world) == []


def test_resume_and_rollback_check_python_too(world: World):
    for flag in ("--resume", "--rollback"):
        r = door(world, flag, extra={"MC_PYTHON": "python3-that-does-not-exist"})
        assert r.returncode != 0 and "python3" in r.out, flag


# ── which engine runs ───────────────────────────────────────────────────────

def _lib(cwd: Path, snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'MC_INSTALL_LIB_ONLY=1 . "{INSTALL_SH}"; {snippet}'], cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)


def test_the_installs_own_engine_runs_first(world: World):
    r = _lib(world.inst, "mc_update_engine")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(world.inst / "scripts" / "mc_update.py")


def test_an_install_without_an_engine_uses_the_one_next_to_the_installer(tmp_path: Path):
    """A release from before the engine, updated with the current installer:
    its engine takes over, and from the switch on the target's own."""
    r = _lib(tmp_path, "mc_update_engine")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(ROOT / "scripts" / "mc_update.py")
