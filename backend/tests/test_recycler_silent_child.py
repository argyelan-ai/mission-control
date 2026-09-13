"""G4 (Paritaets-Audit #521) — silent-child liveness for the recyclers.

recycler.sh kills the TUI process when the marker mtime says "idle >=
RECYCLER_IDLE_MIN". A long pytest/build run renders NOTHING in the pane, so
the marker stays stale and the recycler killed a working agent (incident
12.09.2026: "idle nach working, 36 Zyklen" + twice
``orphaned_run_redispatched``).

The fix lives in docker/shared/recycler-lib.sh (the SSoT both recycler
variants source): ``subtree_busy <pid>`` — CPU jiffies (utime+stime from
/proc/<pid>/stat) summed over the process tree, compared as a DELTA between
consecutive calls. Work = jiffies grew since the last recycler tick. The
counter-probe contract: a subtree with NO cpu progress must NOT count as
busy, or a wedged-but-warm process would block recycling forever.

Probe style: real /proc, real processes. The burner is the process UNDER
TEST itself (a bounded shell busy-loop, >=5 s lifetime) — no nested trees
with wall-clock-fragile lifetimes, no reliance on ``wait`` bookkeeping under
pytest's process handling: every script terminates on its own.

CPU measurement is inherently load-sensitive, so the assertions assert
DIRECTION (busy-child delta >= threshold vs sleeping-child delta = 0), with
generous windows, never exact counts.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "docker" / "shared" / "recycler-lib.sh"

pytestmark = pytest.mark.skipif(not LIB.exists(), reason="recycler-lib.sh not found")

BURNER = "timeout 6 bash -c 'while :; do :; done'"
SLEEPER = "timeout 6 bash -c 'sleep 5'"


@pytest.fixture()
def lib_env(tmp_path: Path, monkeypatch):
    """Point TMPDIR at the tmp_path — the delta state files must never leak
    between tests (each test needs a fresh baseline)."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return str(tmp_path)


def _run(lib_env, body: str) -> subprocess.CompletedProcess:
    script = f'source "{LIB}"\n' + body
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=45,
        env={**os.environ, "TMPDIR": lib_env},
    )


def test_proc_cpu_jiffies_reads_utime_stime(lib_env):
    res = _run(lib_env, f'proc_cpu_jiffies {os.getpid()}\n')
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().isdigit() and int(res.stdout) > 0


def test_proc_cpu_jiffies_empty_for_missing_pid(lib_env):
    res = _run(lib_env, 'out=$(proc_cpu_jiffies 4000000); [ -z "$out" ] && echo empty\n')
    assert "empty" in res.stdout


def test_subtree_busy_true_for_active_process(lib_env):
    """PROBE (red without the fix): a burner over two ticks = BUSY."""
    res = _run(
        lib_env,
        "BIN &\npid=$!\n"
        "sleep 0.5\n"
        'subtree_busy "$pid" && r1=busy || r1=idle\n'
        "sleep 1.5\n"
        'subtree_busy "$pid" && r2=busy || r2=idle\n'
        'echo "$r1 $r2"\n'
        'wait "$pid" 2>/dev/null || true\n'
        .replace("BIN", BURNER),
    )
    assert res.returncode == 0, res.stderr
    r1, r2 = res.stdout.split()
    assert r1 == "idle", "first call only primes the baseline"
    assert r2 == "busy", "a process burning CPU must count as work"


def test_subtree_busy_false_without_cpu_progress(lib_env):
    """COUNTER-PROBE: a sleeping process (zero jiffies delta) stays NOT busy —
    the recycler's purpose (memory hygiene / dead-session cleanup) survives."""
    res = _run(
        lib_env,
        "BIN &\npid=$!\n"
        "sleep 0.5\n"
        'subtree_busy "$pid" && r1=busy || r1=idle\n'
        "sleep 1.5\n"
        'subtree_busy "$pid" && r2=busy || r2=idle\n'
        'echo "$r1 $r2"\n'
        'wait "$pid" 2>/dev/null || true\n'
        .replace("BIN", SLEEPER),
    )
    assert res.returncode == 0, res.stderr
    r1, r2 = res.stdout.split()
    assert r1 == "idle" and r2 == "idle", "a process with no cpu delta is not work"


def test_subtree_pids_enumerates_children(lib_env):
    """Tree traversal: the burner runs as a CHILD of a wrapper — subtree_pids
    must list wrapper AND child (enumeration is deterministic; CPU timing is
    covered by the single-process probes above)."""
    res = _run(
        lib_env,
        "bash -c 'BIN; exit 0' &\npid=$!\n"
        "sleep 0.5\n"
        'echo "pids: $(subtree_pids "$pid" | wc -l)"\n'
        'wait "$pid" 2>/dev/null || true\n'
        .replace("BIN", BURNER),
    )
    assert res.returncode == 0, res.stderr
    line = next(l for l in res.stdout.splitlines() if l.startswith("pids:"))
    count = int(line.split(":")[1])
    assert count >= 2, f"subtree_pids must include the child (got {count})"


def test_subtree_busy_missing_pid_is_never_busy(lib_env):
    """A vanished root must not block the recycler (fail-open to recycle)."""
    res = _run(lib_env, 'subtree_busy 4000000 && echo busy || echo idle\n')
    assert "idle" in res.stdout


def test_busy_threshold_env_is_honored(lib_env):
    """RECYCLER_BUSY_MIN_JIFFIES raises the bar: a sleeping process can never
    reach an unreachable threshold (deterministic — no CPU timing involved)."""
    res = _run(
        lib_env,
        "export RECYCLER_BUSY_MIN_JIFFIES=100000000\n"
        "BIN &\npid=$!\n"
        "sleep 0.5\n"
        'subtree_busy "$pid" && r1=busy || r1=idle\n'
        "sleep 1.5\n"
        'subtree_busy "$pid" && r2=busy || r2=idle\n'
        'echo "$r2"\n'
        'wait "$pid" 2>/dev/null || true\n'
        .replace("BIN", SLEEPER),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "idle", "0 jiffies can never cross the threshold"


def test_subtree_pids_enumerates_children(lib_env):
    """Tree traversal: the burner runs as a CHILD of a wrapper — subtree_pids
    must list wrapper AND child (enumeration is deterministic; CPU timing is
    covered by the single-process probes above)."""
    burner = BURNER.replace("'", '"')
    res = _run(
        lib_env,
        f"bash -c '{burner}; exit 0' &\npid=$!\n"
        "sleep 0.5\n"
        'echo "pids: $(subtree_pids "$pid" | wc -l)"\n'
        'wait "$pid" 2>/dev/null || true\n',
    )
    assert res.returncode == 0, res.stderr
    line = next(l for l in res.stdout.splitlines() if l.startswith("pids:"))
    count = int(line.split(":")[1])
    assert count >= 2, f"subtree_pids must include the child (got {count})"


def test_subtree_busy_delta_not_cumulative(lib_env):
    """A process that burned CPU BEFORE priming (cumulative jiffies high) but
    is now asleep must NOT be busy — the delta, not the total, is the signal.
    Deterministic: the burner has fully exited before the first call."""
    res = _run(
        lib_env,
        "BIN &\npid=$!\n"
        'wait "$pid" 2>/dev/null || true\n'   # burner done — jiffies frozen high
        'subtree_busy "$pid" && r1=busy || r1=idle\n'  # primes with the HIGH total
        "sleep 0.3\n"
        'subtree_busy "$pid" && r2=busy || r2=idle\n'  # zero delta — must stay idle
        'echo "$r2"\n'
        .replace("BIN", BURNER),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "idle", "past work must not block today's recycle"


def test_both_recyclers_source_the_shared_lib():
    """The guard must exist in BOTH image variants (they drifted once before —
    that is why recycler-lib.sh exists at all)."""
    for variant in ("mc-agent-base", "mc-claude-agent"):
        script = (LIB.parents[1] / variant / "recycler.sh").read_text()
        assert "subtree_busy" in script, f"{variant}/recycler.sh lacks the G4 guard"
        assert "recycler-lib.sh" in script, f"{variant}/recycler.sh must source the shared lib"


def test_recycler_guard_samples_busy_every_tick():
    """WIRING PROBE (review blocker on 741d0ff): the recycler guard must call
    subtree_busy EVERY tick, not only once IDLE_MIN crosses the threshold.

    Behind the short-circuit AND (`idle && subtree_busy`), the very tick that
    crosses the threshold is the PRIMING call — it returns false by contract,
    so a genuinely busy silent child got recycled in exactly the tick the
    guard was supposed to save it. The fix calls subtree_busy unconditionally
    and the guard evaluates the cached verdict.

    Asserted on the script text (the real loop cannot run here — no tmux /
    claude / markers) with a positive AND a negative probe, so a regression
    to `IDLE_MIN ... && subtree_busy` goes red again.
    """
    for variant in ("mc-agent-base", "mc-claude-agent"):
        script = (LIB.parents[1] / variant / "recycler.sh").read_text()
        assert "SUBTREE_BUSY_NOW" in script, (
            f"{variant}/recycler.sh samples subtree_busy only behind the idle "
            "threshold — the threshold-crossing tick is the priming call and "
            "kills a working silent child (review blocker)"
        )
        assert 'subtree_busy "$PID" && SUBTREE_BUSY_NOW=true' in script, (
            f"{variant}/recycler.sh must sample the busy delta unconditionally"
        )
        assert '&& [ "$SUBTREE_BUSY_NOW" = "true" ]' in script, (
            f"{variant}/recycler.sh guard must evaluate the sampled verdict"
        )
        # Negative probe: no direct short-circuit sampling left in a guard.
        assert '&& subtree_busy "$PID"' not in script, (
            f"{variant}/recycler.sh still samples subtree_busy behind the "
            "threshold AND — priming tick kills the busy child"
        )
