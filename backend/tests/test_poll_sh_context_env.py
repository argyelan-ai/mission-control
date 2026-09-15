"""W5 (2026-09-13): per-agent mc-context.env for the shared poll.sh.

Host agents (boss-host, kimi-host, ...) share the host's /tmp — poll.sh's
hardcoded `cat > /tmp/mc-context.env` meant every host poll.sh loop, bridge
and mc CLI wrote and read ONE file. Last writer wins set TASK_ID and
X_DISPATCH_ATTEMPT_ID for everyone (13.09. incident: Hermes' card id
4c9bb492 repointed other agents' `mc` calls; a progress comment landed on
Hermes' card).

Two guarantees under test (same harness as test_poll_sh_host_paths.py):
  1. Default (unset) stays byte-identical to the live fleet: /tmp path.
  2. When overridden (host entrypoints set MC_CONTEXT_ENV_PATH=$BASE/...),
     run_task writes the 3-key context there (0600 — attempt ids), exports
     the path into the tmux session env, and stop_task_session truncates it.
"""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POLL_SH = REPO_ROOT / "docker" / "shared" / "poll.sh"

BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    not POLL_SH.exists(), reason="canonical poll.sh not found"
)

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
export MSG_QUEUE_DIR="$WORK/q"
export MSG_ACK_DIR="$WORK/ack"
export NUDGE_STATE_FILE="$WORK/nudge-state"
export NUDGE_TMP_FILE="$WORK/nudge.txt"
export RECYCLER_MARKER_FILE="$WORK/marker"
export COMMENTS_PROMPT_FILE="$WORK/comments-prompt.txt"
export TASK_PROMPT_FILE="$WORK/task-prompt.txt"
export TASK_LOCK_FILE="$WORK/task.lock"
export MC_API_URL=http://example.invalid MC_TOKEN=t SESSION_NAME=test
export READY_TIMEOUT_SEC=0 READY_POLL_INTERVAL_SEC=0
export PASTE_VERIFY_DELAY_SEC=0 PASTE_RETRY_DELAY_SEC=0
export PATH="$WORK/bin:$PATH"
export TMUX_LOG="$WORK/tmux.log"

source "$POLLSH"

heartbeat() { return 0; }
turn_activity_hash() { echo hash; }
paste_and_submit() {
    local f="$1"; [ "$1" = "--no-fail-open" ] && f="$2"
    echo "$f" >> "$WORK/pasted.log"
    return 0
}
"""


def _make_workspace(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "lib").mkdir(parents=True)
    (work / "q").mkdir()
    (work / "ack").mkdir()
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
        [BASH, "-c", script], capture_output=True, text=True, timeout=60
    )


def _task_resp(task_id="task-1", attempt="att-1") -> str:
    return json.dumps(
        {
            "task": {
                "id": task_id,
                "board_id": "board-1",
                "dispatch_attempt_id": attempt,
                "status": "inbox",
                "prompt": "arbeite",
            }
        }
    )


# ── Default: unset variable keeps the legacy /tmp path (container behavior) ───
def test_default_context_env_path_is_legacy_tmp(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(work, 'echo "R=$MC_CONTEXT_ENV_PATH"\n')
    assert res.returncode == 0, res.stderr
    assert "R=/tmp/mc-context.env" in res.stdout


# ── Override: run_task writes the per-agent file, 0600, and exports the path ──
def test_run_task_writes_context_to_override_path(tmp_path):
    work = _make_workspace(tmp_path)
    ctx = work / "agents" / "kimi" / "mc-context.env"
    ctx.parent.mkdir(parents=True)  # provisioned by the host entrypoint in real life
    resp = _task_resp().replace('"', '\\"')
    res = _run(
        work,
        f'export MC_CONTEXT_ENV_PATH="{ctx}"\n'
        f'run_task "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert ctx.exists(), "run_task must write the per-agent context file"
    assert ctx.read_text() == (
        "TASK_ID=task-1\n"
        "BOARD_ID=board-1\n"
        "X_DISPATCH_ATTEMPT_ID=att-1\n"
    )
    # Attempt ids are not world material: per-agent file is owner-only.
    assert stat.S_IMODE(ctx.stat().st_mode) == 0o600
    # The agent's own `mc` shells must resolve the SAME file: tmux env carries it.
    tmux_log = (work / "tmux.log").read_text()
    assert f"MC_CONTEXT_ENV_PATH {ctx}" in tmux_log


# ── Stop: truncates the per-agent file, not the shared one ────────────────────
def test_stop_task_session_truncates_override_path(tmp_path):
    work = _make_workspace(tmp_path)
    ctx = work / "mc-context.env"
    ctx.write_text("TASK_ID=task-1\nBOARD_ID=board-1\nX_DISPATCH_ATTEMPT_ID=att-1\n")
    res = _run(
        work,
        f'export MC_CONTEXT_ENV_PATH="{ctx}"\n'
        f'stop_task_session task-1\n',
    )
    assert res.returncode == 0, res.stderr
    assert ctx.read_text() == ""
