"""Crash matrix for the update engine (scripts/mc_update.py, ADR-094) — PR 2c.

The engine promises that a run killed at *any* point — power cut, OOM kill,
``kill -9`` — ends in a clean state after ``resume``: ``done``, ``aborted``
(nothing changed) or ``rolled_back``. Never half switched, never without the
operator's fleet file, never with the lock or the drain left behind.

This file proves it instead of sampling it. Every journal write is a
boundary; the engine is killed right **before** and right **after** each of
them (``MC_UPDATE_TEST_CRASH_SAVE=<n>:before|after``, counted across the
hand-over to the target's engine). "Before" is the dangerous side: the change
already happened, the journal does not say so yet. The named points *inside*
a phase (``MC_UPDATE_TEST_FAULT``, between parking the fleet file and the
checkout, between the restore and the checkout back, …) come on top.

The boundaries are not listed by hand: each scenario is run once without a
crash and the engine logs every journal write (``MC_UPDATE_TEST_SAVE_LOG``).
A new phase or a new journal write is in the matrix automatically; a journal
write that goes missing turns the matrix red, because some crash then leaves
a journal that tells ``resume`` the wrong story.

Scenarios:

* ``update``   — the normal update v0.1.0 → v0.2.0;
* ``r1``       — the new version never gets healthy, the schema did not move
                 → way back R1;
* ``r2``       — the new version never gets healthy after its migration
                 → way back R2 (database restore from the pre-update backup);
* ``undo``     — the manual "back to the previous version" of a finished
                 update (``rollback`` of a ``done`` run, R2).

On top, the resume itself is killed at every one of *its* boundaries for one
crash per region (before the switch, in the switch, in verify, on the way
back), then resumed again.

Same scratch world as test_mc_update.py: local git upstream, real backup.sh,
fake docker, no network, no real Docker.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mc_update import FLEET, USER_FLEET, World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ENDS = {"done", "aborted", "rolled_back"}

# Per scenario: what the stub world does differently, and which command runs.
SCENARIOS: Dict[str, Dict] = {
    "update": {"state": {}, "command": "start"},
    "r1": {"state": {"health_for_tag": {"0.2.0": "unhealthy"},
                     "schema_for_tag": {"0.1.0": "0001", "0.2.0": "0001"}}, "command": "start"},
    "r2": {"state": {"health_for_tag": {"0.2.0": "unhealthy"}}, "command": "start"},
    "undo": {"state": {}, "command": "rollback", "after_update": True},
}

# Where a scenario can end without any crash — a crash must not invent a
# new outcome (an update that aborts or rolls back for no reason is a bug,
# but so is a failing update that ends "done").
CLEAN_END = {"update": "done", "r1": "rolled_back", "r2": "rolled_back", "undo": "rolled_back"}
ALLOWED_END = {
    "update": {"done", "aborted"},          # killed before the switch → aborted
    "r1": {"rolled_back", "aborted"},
    "r2": {"rolled_back", "aborted"},
    "undo": {"rolled_back", "done"},        # killed before the way back started → stays done
}

# Named points inside a phase (MC_UPDATE_TEST_FAULT) that each scenario passes.
NAMED_POINTS = {
    "update": ["switch.worker_stopped", "switch.parked", "switch.checked_out",
               "switch.fleet_back", "switch.pins"],
    "r1": ["rolling_back.parked", "rolling_back.checked_out", "rolling_back.fleet_back"],
    "r2": ["rolling_back.r2_stopped", "rolling_back.restored", "rolling_back.parked",
           "rolling_back.checked_out", "rolling_back.fleet_back"],
    "undo": ["rolling_back.r2_stopped", "rolling_back.restored", "rolling_back.checked_out"],
}


# ── building a scenario ─────────────────────────────────────────────────────

def _world(tmp: Path, scenario: str) -> World:
    w = World(tmp).build()
    spec = SCENARIOS[scenario]
    if spec.get("after_update"):
        r = w.run("start")
        assert r.returncode == 0, r.stdout + r.stderr
    if spec["state"]:
        w.patch_state(**spec["state"])
    return w


def _run(w: World, args: Tuple[str, ...], extra: Dict[str, str]):
    return w.run(*args, extra_env=extra, timeout=120)


def _probe(tmp: Path, scenario: str) -> List[str]:
    """The journal writes of one clean run of the scenario, in order."""
    w = _world(tmp, scenario)
    log = tmp / "saves.log"
    r = _run(w, (SCENARIOS[scenario]["command"],), {"MC_UPDATE_TEST_SAVE_LOG": str(log)})
    assert r.returncode in (0, 1, 5), r.stdout + r.stderr
    assert w.journal()["phase"] == CLEAN_END[scenario]
    assert log.exists(), "the engine logged no journal writes (MC_UPDATE_TEST_SAVE_LOG)"
    return [line.split(" ", 1)[1] for line in log.read_text().splitlines() if line.strip()]


# Collection-time probe: the boundary count decides the parametrisation. One
# clean run per scenario (and one resume per first crash, below), done once
# and shared by all xdist workers through a cache file keyed by the code under
# test — every worker must collect exactly the same cases.

def _discover_single(root: Path) -> List[Tuple[str, int, str, str]]:
    cases = []
    for scenario in SCENARIOS:
        d = root / scenario
        d.mkdir(parents=True)
        for n, label in enumerate(_probe(d, scenario), start=1):
            for stage in ("before", "after"):
                cases.append((scenario, n, stage, label))
    return cases


def _cache_key() -> str:
    import hashlib
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in (here.parents[1] / "scripts" / "mc_update.py", here.parents[1] / "backup.sh",
              here / "fake_docker.py", here / "test_mc_update.py", Path(__file__).resolve()):
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _discover() -> Dict[str, list]:
    import tempfile
    cache = Path(tempfile.gettempdir()) / f"mc-update-crash-matrix-{_cache_key()}.json"
    with open(str(cache) + ".lock", "w") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        if cache.exists():
            data = json.loads(cache.read_text())
            return {k: [tuple(c) for c in v] for k, v in data.items()}
        root = Path(tempfile.mkdtemp(prefix="mc-update-crash-probe-"))
        try:
            single = _discover_single(root / "single")
            double = _discover_double(root / "double")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        data = {"single": single, "double": double}
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(str(tmp), str(cache))
        return data


# ── a crash during the resume ───────────────────────────────────────────────
# One first crash per region; then the resume is killed at each of its own
# journal writes, and a second resume has to finish the job.

FIRST_CRASH = [
    ("update", "pull.intent"),          # before the switch → resume aborts
    ("update", "switch.checked_out"),   # half way through the switch
    ("update", "verify.intent"),        # new version up, not yet proven
    ("r2", "rolling_back.restored"),    # database restored, old code not back yet
]


def _resume_boundaries(tmp: Path, scenario: str, point: str) -> List[str]:
    w = _world(tmp, scenario)
    r = _run(w, (SCENARIOS[scenario]["command"],), {"MC_UPDATE_TEST_FAULT": point})
    assert r.returncode == 137
    log = tmp / "resume-saves.log"
    _run(w, ("resume",), {"MC_UPDATE_TEST_SAVE_LOG": str(log)})
    return [line.split(" ", 1)[1] for line in log.read_text().splitlines() if line.strip()]


def _discover_double(root: Path) -> List[Tuple[str, str, int, str]]:
    cases = []
    for i, (scenario, point) in enumerate(FIRST_CRASH):
        d = root / str(i)
        d.mkdir(parents=True)
        for n, label in enumerate(_resume_boundaries(d, scenario, point), start=1):
            cases.append((scenario, point, n, label))
    return cases


try:
    _FOUND = _discover() if shutil.which("git") else {"single": [], "double": []}
    PROBE_ERROR = ""
except Exception as e:  # noqa: BLE001 — collection must not hide the reason
    _FOUND = {"single": [], "double": []}
    PROBE_ERROR = repr(e)
CASES = _FOUND["single"]
DOUBLE = _FOUND["double"]


# ── what every end state must look like ─────────────────────────────────────

def _lock_is_free(w: World) -> None:
    st = json.loads(w.run("status", "--json").stdout)
    assert st["lock"]["held"] is False, st
    lock = w.state / "lock"
    if lock.exists():
        fd = os.open(str(lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises when held
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def assert_clean_end(w: World, scenario: str, env_inode: int) -> str:
    runs = sorted((w.state / "journal").glob("*.json")) if (w.state / "journal").is_dir() else []
    if not runs:
        # Killed before the run's first journal write: no run exists, so
        # nothing may have changed.
        j = {"phase": "aborted", "run_id": None}
    else:
        j = w.journal()
    end = j["phase"]
    assert end in ENDS, f"run ended in {end!r}: {j.get('error')}"
    assert end in ALLOWED_END[scenario], f"{scenario}: ended {end}, allowed {ALLOWED_END[scenario]}"
    st = w.get_state()
    on_new = end == "done"
    tag = "0.2.0" if on_new else "0.1.0"
    # Code, pins and running containers belong to ONE version.
    assert w.head_tag() == f"v{tag}", (end, w.head_tag())
    assert w.env_value("MC_IMAGE_TAG") == tag, (end, w.env_value("MC_IMAGE_TAG"))
    assert w.env_value("MC_AGENT_IMAGE_TAG") == tag
    for svc, img in (("backend", "mc-backend"), ("mc-worker", "mc-backend"), ("frontend", "mc-frontend")):
        c = st["containers"][svc]
        assert c["state"] == "running", (svc, c)
        assert c["image"] == f"sha256:{img}-{tag}", (svc, c, end)
    if end == "done":
        assert st["schema"] == "0002" or scenario == "r1"
    else:
        assert st["schema"] == "0001", f"{end} but schema {st['schema']}"
    # The operator's data that lives next to the code.
    assert (w.inst / FLEET).read_text() == USER_FLEET, "fleet file lost or changed"
    assert w.env_value("DB_PASSWORD") == "keep-me"
    assert (w.inst / ".env").stat().st_ino == env_inode, ".env was replaced, not written in place"
    # Nothing left behind.
    assert st.get("drain") is None, f"drain left on after {end}: {st.get('drain')}"
    _lock_is_free(w)
    status = json.loads(w.run("status", "--json").stdout)
    assert status.get("interrupted", False) is False
    if end in ("aborted", "rolled_back") and j["run_id"]:
        rd = w.state / f"run-{j['run_id']}"
        assert not (rd / "env.before").exists(), "the run's secret .env copy outlived it"
    # Exactly one run is open at most — and none now.
    for p in runs:
        assert json.loads(p.read_text())["phase"] in ENDS | {"rollback_failed"}, p.name
    return end


def _resume_until_end(w: World, extra: Dict[str, str] = None, rounds: int = 3):
    r = None
    for _ in range(rounds):
        r = _run(w, ("resume",), extra or {})
        assert r.returncode != 137 or extra, r.stdout + r.stderr
        extra = None
        if not list((w.state / "journal").glob("*.json")) or w.journal()["phase"] in ENDS:
            return r
    return r


# ── the matrix ──────────────────────────────────────────────────────────────

def test_probe_found_every_phase_boundary():
    """The matrix is only as good as its boundaries: every phase must have
    journaled its intent and its result in the clean run."""
    assert not PROBE_ERROR, PROBE_ERROR
    by = {}
    for scenario, _n, _stage, label in CASES:
        by.setdefault(scenario, set()).add(label)
    for phase in ("drain", "fetch", "verify_release", "pull", "backup", "switch", "up", "verify"):
        assert f"{phase}.intent" in by["update"], phase
        assert f"{phase}.done" in by["update"], phase
    assert "done.done" in by["update"]
    for scenario in ("r1", "r2", "undo"):
        assert "rolling_back.intent" in by[scenario], scenario
        assert "rolled_back.done" in by[scenario], scenario
    assert "rolling_back.restored" in by["r2"] and "rolling_back.restored" in by["undo"]


@pytest.mark.parametrize("scenario,n,stage,label", CASES,
                         ids=[f"{s}-{n:02d}-{st}-{lb}" for s, n, st, lb in CASES])
def test_kill_at_every_journal_boundary_then_resume(tmp_path: Path, scenario: str, n: int,
                                                    stage: str, label: str):
    w = _world(tmp_path, scenario)
    inode = (w.inst / ".env").stat().st_ino
    r = _run(w, (SCENARIOS[scenario]["command"],), {"MC_UPDATE_TEST_CRASH_SAVE": f"{n}:{stage}"})
    assert r.returncode == 137, f"no crash at {n}:{stage} ({label}): {r.stdout}{r.stderr}"
    r = _resume_until_end(w)
    assert_clean_end(w, scenario, inode)


NAMED = [(s, p) for s, points in NAMED_POINTS.items() for p in points]


@pytest.mark.parametrize("scenario,point", NAMED, ids=[f"{s}-{p}" for s, p in NAMED])
def test_kill_inside_a_phase_then_resume(tmp_path: Path, scenario: str, point: str):
    w = _world(tmp_path, scenario)
    inode = (w.inst / ".env").stat().st_ino
    r = _run(w, (SCENARIOS[scenario]["command"],), {"MC_UPDATE_TEST_FAULT": point})
    assert r.returncode == 137, f"{point} never fired: {r.stdout}{r.stderr}"
    _resume_until_end(w)
    assert_clean_end(w, scenario, inode)


# ── the resume killed, then resumed again ───────────────────────────────────

def test_double_crash_matrix_is_not_empty():
    assert not PROBE_ERROR, PROBE_ERROR
    regions = {(s, p) for s, p, _n, _l in DOUBLE}
    assert regions == set(FIRST_CRASH), regions


@pytest.mark.parametrize("scenario,point,n,label", DOUBLE,
                         ids=[f"{s}-{p}-resume{n:02d}-{lb}" for s, p, n, lb in DOUBLE])
def test_kill_the_resume_at_every_boundary_then_resume_again(tmp_path: Path, scenario: str, point: str,
                                                             n: int, label: str):
    w = _world(tmp_path, scenario)
    inode = (w.inst / ".env").stat().st_ino
    r = _run(w, (SCENARIOS[scenario]["command"],), {"MC_UPDATE_TEST_FAULT": point})
    assert r.returncode == 137
    r = _run(w, ("resume",), {"MC_UPDATE_TEST_CRASH_SAVE": f"{n}:before"})
    assert r.returncode == 137, f"resume did not crash at {n} ({label}): {r.stdout}{r.stderr}"
    _resume_until_end(w)
    assert_clean_end(w, scenario, inode)
