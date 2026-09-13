"""G6 (dispatch-path-parity.md #31/#32): poll.sh's heartbeat had no `task_id`
and never read the `control` field of the heartbeat response — the bridge
path (bridge.py `_build_heartbeat_payload` / `_on_control`) has both. Two
concrete gaps closed here:

1. `build_heartbeat_payload` now includes `task_id` (+ `attempt_id`) while a
   turn is genuinely tracked (gated on a non-empty TASK_ID, mirroring
   bridge.py's `turn_ctx is not None` gate).
2. `handle_heartbeat_control` reacts to a HARD interrupt in the heartbeat
   response (backend: `_collect_heartbeat_control` / `_withdrawn_task_reason`,
   agents.py:3798/3767) by sending a single Escape — the withdrawn-task notice
   (row 9) was previously fully absent on this path, and a blocked-foreign
   hard interrupt (row 8) only ever reached poll.sh via the next 5s poll
   cycle's own state, never through the heartbeat's own control channel.

Deliberately NOT covered here (scope, see poll.sh comments at the call site):
SOFT interrupts stay a no-op — that mirrors bridge.py's own semantics
(bridge.py:2916, a soft interrupt only comments and lets the turn finish) and
is not a gap in the parity table.

Same harness as test_poll_sh_gate.py: source poll.sh with POLL_SH_SOURCE_ONLY=1
(functions only), stub tmux via a PATH shim so the ESC-sending call is
observable without a real terminal, and never touch the network (heartbeat's
own urllib POST is out of scope for these tests — they call
build_heartbeat_payload/handle_heartbeat_control directly, both pure/no-I/O
functions).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POLL_SH = REPO_ROOT / "docker" / "shared" / "poll.sh"

BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    not POLL_SH.exists(), reason="canonical poll.sh not found"
)

# tmux shim: record every invocation (one line, argv space-joined) to $TMUX_LOG
# and no-op — same shim test_poll_sh_gate.py uses to observe paste-buffer
# calls; here we watch for `send-keys ... Escape`.
TMUX_SHIM = """#!/usr/bin/env bash
if [ -n "${TMUX_LOG:-}" ]; then
    echo "$*" >> "$TMUX_LOG"
fi
exit 0
"""

PRELUDE = r"""
set -uo pipefail
export POLL_SH_SOURCE_ONLY=1
export POLL_LIB_DIR="$WORK/lib"
export MC_API_URL=http://example.invalid MC_TOKEN=t SESSION_NAME=test
export READY_TIMEOUT_SEC=0 READY_POLL_INTERVAL_SEC=0
export PASTE_VERIFY_DELAY_SEC=0 PASTE_RETRY_DELAY_SEC=0
export TASK_PROMPT_FILE="$WORK/task_prompt.txt"
export TASK_LOCK_FILE="$WORK/task.lock"
export RECYCLER_MARKER_FILE="$WORK/marker"
export PATH="$WORK/bin:$PATH"
export TMUX_LOG="$WORK/tmux.log"

source "$POLLSH"

# Only run_task() needs these (paste_and_submit / detect_turn_state) — the
# build_heartbeat_payload and handle_heartbeat_control tests never call them.
detect_turn_state() { echo "${FAKE_TS:-idle}"; }
wait_for_clean_prompt() { [ "${FAKE_CLEAN:-1}" = "1" ]; }
verify_paste_landed() { return 0; }
classify_paste_outcome() { echo 0; }
"""


def _make_workspace(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "lib").mkdir(parents=True)
    (work / "bin").mkdir()
    for lib in ("turn-state", "ui-detect", "paste-verify", "context-detect"):
        (work / "lib" / f"{lib}.sh").write_text(": # stub\n")
    shim = work / "bin" / "tmux"
    shim.write_text(TMUX_SHIM)
    shim.chmod(0o755)
    return work


def _run(work: Path, body: str) -> subprocess.CompletedProcess:
    script = (
        f'export WORK="{work}"\n'
        f'export POLLSH="{POLL_SH}"\n'
        + PRELUDE
        + "\n"
        + body
    )
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _escape_count(tmux_log: Path) -> int:
    if not tmux_log.exists():
        return 0
    return sum(
        1
        for line in tmux_log.read_text().splitlines()
        if line.startswith("send-keys ") and "Escape" in line
    )


# ── build_heartbeat_payload: task_id/attempt_id gating ─────────────────────

def test_payload_omits_task_id_when_no_active_turn(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(work, 'build_heartbeat_payload "idle" "" "" ""\n')
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout.strip())
    assert payload == {"status": "idle"}


def test_payload_includes_task_id_and_attempt_id_while_turn_active(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        'build_heartbeat_payload "working" "42" "task-123" "attempt-9"\n',
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout.strip())
    assert payload == {
        "status": "working",
        "context_pct": 42.0,
        "task_id": "task-123",
        "attempt_id": "attempt-9",
    }


def test_payload_omits_attempt_id_when_task_id_set_but_attempt_empty(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(work, 'build_heartbeat_payload "working" "" "task-123" ""\n')
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout.strip())
    assert payload == {"status": "working", "task_id": "task-123"}


# ── handle_heartbeat_control: the actual G6 fix ─────────────────────────────

def test_hard_interrupt_sends_escape(tmp_path):
    work = _make_workspace(tmp_path)
    resp = json.dumps(
        {"ok": True, "control": {"interrupt": "hard", "reason": "Task entzogen"}}
    ).replace('"', '\\"')
    res = _run(
        work,
        f'CURRENT_TASK_ID="task-123"\n'
        f'handle_heartbeat_control "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _escape_count(work / "tmux.log") == 1
    assert "Task entzogen" in res.stderr or "Task entzogen" in res.stdout


def test_soft_interrupt_does_not_send_escape(tmp_path):
    # G1 (dispatch-path-parity.md) is a known false-positive risk on soft
    # interrupts (unread own-comments) — poll.sh must NOT cancel the turn for
    # "soft", mirroring bridge.py:2916 (soft only comments, never cancels).
    work = _make_workspace(tmp_path)
    resp = json.dumps(
        {"ok": True, "control": {"interrupt": "soft", "reason": "Ungelesene Kommentare"}}
    ).replace('"', '\\"')
    res = _run(
        work,
        f'CURRENT_TASK_ID="task-123"\n'
        f'handle_heartbeat_control "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _escape_count(work / "tmux.log") == 0


def test_no_control_field_is_noop(tmp_path):
    work = _make_workspace(tmp_path)
    resp = json.dumps({"ok": True, "agent": "test"}).replace('"', '\\"')
    res = _run(
        work,
        f'CURRENT_TASK_ID="task-123"\n'
        f'handle_heartbeat_control "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _escape_count(work / "tmux.log") == 0


def test_empty_or_malformed_response_is_noop_and_does_not_crash(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        'CURRENT_TASK_ID="task-123"\n'
        'handle_heartbeat_control ""\n'
        'handle_heartbeat_control "not json"\n'
        'echo SURVIVED\n',
    )
    assert res.returncode == 0, res.stderr
    assert "SURVIVED" in res.stdout
    assert _escape_count(work / "tmux.log") == 0


def test_hard_interrupt_escape_is_idempotent_per_task(tmp_path):
    # Same task, same heartbeat cycle firing repeatedly (30s cadence) must
    # send Escape ONCE — not once per cycle. Mirrors LAST_CANCELLED_TASK_ID /
    # LAST_STOPPED_TASK_ID's existing dedup pattern.
    work = _make_workspace(tmp_path)
    resp = json.dumps(
        {"control": {"interrupt": "hard", "reason": "Task blocked durch Fremdakteur"}}
    ).replace('"', '\\"')
    res = _run(
        work,
        f'CURRENT_TASK_ID="task-123"\n'
        f'handle_heartbeat_control "{resp}"\n'
        f'handle_heartbeat_control "{resp}"\n'
        f'handle_heartbeat_control "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _escape_count(work / "tmux.log") == 1


def test_hard_interrupt_escape_fires_again_for_a_new_task(tmp_path):
    # Dedup is per-task, not global — a second, DIFFERENT task's hard
    # interrupt must not be swallowed by the first task's marker.
    work = _make_workspace(tmp_path)
    resp = json.dumps(
        {"control": {"interrupt": "hard", "reason": "Task entzogen"}}
    ).replace('"', '\\"')
    res = _run(
        work,
        f'CURRENT_TASK_ID="task-A"\n'
        f'handle_heartbeat_control "{resp}"\n'
        f'CURRENT_TASK_ID="task-B"\n'
        f'handle_heartbeat_control "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _escape_count(work / "tmux.log") == 2


# ── run_task() resets the dedup marker for a fresh dispatch ────────────────

def test_run_task_resets_hard_interrupt_marker(tmp_path):
    # Sabotage probe target: if run_task() forgot this reset (or someone
    # deletes the reset line), a task that was withdrawn once and later
    # legitimately re-dispatched under the SAME task_id would stay
    # permanently suppressed — verified directly against the state variable
    # so the assertion fails if the reset line is missing, not just if the
    # observable Escape-count happens to still work out.
    #
    # run_task() writes /tmp/mc-context.env unconditionally (poll.sh has no
    # env-var override for that path, unlike TASK_PROMPT_FILE/TASK_LOCK_FILE)
    # — save/restore it so this test can't clobber a real session's context
    # file if it's ever run on a machine with a live poll.sh agent.
    context_env = Path("/tmp/mc-context.env")
    backup = context_env.read_bytes() if context_env.exists() else None
    try:
        work = _make_workspace(tmp_path)
        res = _run(
            work,
            'LAST_HARD_INTERRUPT_TASK_ID="task-123"\n'
            'response_json=\'{"task":{"id":"task-123","prompt":"hi","dispatch_attempt_id":"a1"}}\'\n'
            'run_task "$response_json" >/dev/null 2>&1 || true\n'
            'echo "MARKER=[$LAST_HARD_INTERRUPT_TASK_ID]"\n',
        )
        assert res.returncode == 0, res.stderr
        assert "MARKER=[]" in res.stdout, res.stdout
    finally:
        if backup is not None:
            context_env.write_bytes(backup)
        elif context_env.exists():
            context_env.unlink()
