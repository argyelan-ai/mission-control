"""Guard 1 (docker/shared/poll.sh) — Zwillingsstelle zu
scripts/hermes-bridge.py's `_task_is_dispatchable_for_me` (siehe
test_hermes_bridge_guard1_done_foreign_card.py). Backend-Fix (Guard 2,
agents.py `_task_still_dispatchable`) ist das eigentliche Netz; dies ist die
client-seitige Verteidigungslinie fuer den docker-Runtime-Dispatcher, falls
eine erledigte oder fremde Karte trotzdem als state=new_task ankommt.

Gleiche Harness wie test_poll_sh_heartbeat_control.py / test_poll_sh_gate.py:
poll.sh mit POLL_SH_SOURCE_ONLY=1 sourcen, tmux via PATH-Shim stubben,
run_task() direkt mit einem gefaketen /me/poll response_json aufrufen.
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

# run_task() writes /tmp/mc-context.env UNCONDITIONALLY (hardcoded path, no
# env-var override — unlike TASK_PROMPT_FILE/TASK_LOCK_FILE/RECYCLER_MARKER_FILE
# above, which the tests already redirect into $WORK). Live-hit during this
# fix's own development: running these tests overwrote the REAL, host-shared
# context file with the fixture's fake t1/b1 IDs, which then made every `mc`
# CLI call on this very container fail with 422 (same incident class as #579,
# which fixed the analogous mc-cli-side hardcoding — poll.sh's own copy of the
# bug was untouched by that PR). Snapshot + restore around every test here so
# this file can never repeat that; a proper poll.sh-side fix (env override,
# mirroring #579) is a separate, out-of-scope change.
REAL_CONTEXT_FILE = Path("/tmp/mc-context.env")


@pytest.fixture(autouse=True)
def _protect_real_context_file():
    backup = REAL_CONTEXT_FILE.read_bytes() if REAL_CONTEXT_FILE.exists() else None
    try:
        yield
    finally:
        if backup is not None:
            REAL_CONTEXT_FILE.write_bytes(backup)
        elif REAL_CONTEXT_FILE.exists():
            REAL_CONTEXT_FILE.unlink()

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

detect_turn_state() { echo "${FAKE_TS:-idle}"; }
wait_for_clean_prompt() { [ "${FAKE_CLEAN:-1}" = "1" ]; }
verify_paste_landed() { return 0; }
classify_paste_outcome() { echo 0; }
"""

AGENT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AGENT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


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


def _dispatch(work: Path, task: dict, my_agent_id: str | None) -> subprocess.CompletedProcess:
    payload = {"state": "new_task", "task": task}
    if my_agent_id is not None:
        payload["my_agent_id"] = my_agent_id
    response_json = json.dumps(payload).replace("'", "'\\''")
    return _run(
        work,
        f"response_json='{response_json}'\n"
        f'run_task "$response_json" >/dev/null 2>&1 || true\n',
    )


def _pasted(work: Path) -> bool:
    log = work / "tmux.log"
    return log.exists() and log.read_text().strip() != ""


def test_done_card_is_not_pasted(tmp_path):
    work = _make_workspace(tmp_path)
    task = {
        "id": "t1", "board_id": "b1", "prompt": "DO X",
        "dispatch_attempt_id": "a1", "status": "done",
        "assigned_agent_id": AGENT_A,
    }
    res = _dispatch(work, task, AGENT_A)
    assert res.returncode == 0, res.stderr
    assert not _pasted(work), "Guard 1 nicht wirksam: done-Karte wurde gepastet"
    assert not (work / "task.lock").exists(), "task.lock darf fuer eine done-Karte nicht geschrieben werden"


def test_failed_card_is_not_pasted(tmp_path):
    work = _make_workspace(tmp_path)
    task = {
        "id": "t1", "board_id": "b1", "prompt": "DO X",
        "dispatch_attempt_id": "a1", "status": "failed",
        "assigned_agent_id": AGENT_A,
    }
    res = _dispatch(work, task, AGENT_A)
    assert res.returncode == 0, res.stderr
    assert not _pasted(work), "Guard 1 nicht wirksam: failed-Karte wurde gepastet"


def test_foreign_card_is_not_pasted(tmp_path):
    work = _make_workspace(tmp_path)
    task = {
        "id": "t1", "board_id": "b1", "prompt": "DO X",
        "dispatch_attempt_id": "a1", "status": "in_progress",
        "assigned_agent_id": AGENT_B,
    }
    res = _dispatch(work, task, AGENT_A)
    assert res.returncode == 0, res.stderr
    assert not _pasted(work), "Guard 1 nicht wirksam: fremde Karte wurde gepastet"


def test_missing_guard_fields_fail_open(tmp_path):
    """Aeltere Backend-Antwort ohne assigned_agent_id/my_agent_id darf den
    Dispatch nicht blockieren — Guard 1 muss OPEN failen, nicht CLOSED."""
    work = _make_workspace(tmp_path)
    task = {"id": "t1", "board_id": "b1", "prompt": "DO X", "dispatch_attempt_id": "a1"}
    res = _dispatch(work, task, None)
    assert res.returncode == 0, res.stderr
    assert _pasted(work), "Guard 1 faellt CLOSED bei fehlenden Feldern — muss OPEN failen"


def test_gegenrichtung_open_own_task_still_pasted(tmp_path):
    """Gegenprobe: eine offene, eigene Karte (status in_progress, gleiche
    assigned_agent_id) muss weiterhin gepastet werden — auch nach einem
    Bridge-Neustart mit leerem Dedup-Cache."""
    work = _make_workspace(tmp_path)
    task = {
        "id": "t1", "board_id": "b1", "prompt": "DO X",
        "dispatch_attempt_id": "a1", "status": "in_progress",
        "assigned_agent_id": AGENT_A,
    }
    res = _dispatch(work, task, AGENT_A)
    assert res.returncode == 0, res.stderr
    assert _pasted(work), "Gegenrichtung verletzt: offene eigene Karte wurde nicht gepastet"
