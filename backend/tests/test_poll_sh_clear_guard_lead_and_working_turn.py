"""`/clear` gate in docker/shared/poll.sh — Board Lead und laufender Turn.

Incident 2026-09-18: der Lead legt seine Karten SELBST an. Jede davon kam als
`state=new_task` zurueck, traf `task_id != LAST_DISPATCHED_TASK_ID` und feuerte
`/clear` in den laufenden Orchestrierungs-Turn — der ganze Context des Leads
war weg. Zwei Gruende muessen den `send-keys /clear` deshalb ueberspringen:

  * Board Lead  — Rolle aus dem Top-Level-Key `is_board_lead` der Poll-Response.
  * laufender Turn — `detect_turn_state "$SESSION_NAME"` = `working`.

Beide Faelle muessen den Grund LOGGEN (sonst sieht der Operator nur ein
fehlendes /clear ohne Erklaerung), und beide duerfen nur den Clear-Triple
ueberspringen — Dispatch, `LAST_DISPATCHED_TASK_ID` und attempt-dedup laufen
weiter. Gegenprobe: ein Worker im idle-Turn wird weiterhin gecleart.

Gleiche Harness wie test_poll_sh_guard1_done_foreign_card.py (PATH-tmux-Shim,
POLL_SH_SOURCE_ONLY=1, Stubs fuer die UI-Proben).
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

# run_task() schreibt /tmp/mc-context.env UNBEDINGT (hardcoded). Snapshot +
# Restore, damit dieser Test den echten, host-geteilten Context nie ueberschreibt.
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
scrape_context_pct() { echo ""; }
turn_activity_hash() { echo "stub-hash"; }
"""

AGENT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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


def _dispatch(
    work: Path,
    task: dict,
    *,
    is_board_lead: bool | None = None,
    turn_state: str = "idle",
) -> subprocess.CompletedProcess:
    """Frischer Task, der die Gates erreicht: `task_id != LAST_DISPATCHED_TASK_ID`,
    `current_task_status != in_progress`, kein Quick-Restart-Fenster; der
    `curl` auf den vorherigen Task scheitert (example.invalid) → `prev_status`
    leer. stdout wird GELESEN (die Grund-Log-Zeile ist der Nachweis)."""
    payload: dict = {"state": "new_task", "task": task, "my_agent_id": AGENT_A}
    if is_board_lead is not None:
        payload["is_board_lead"] = is_board_lead
    response_json = json.dumps(payload).replace("'", "'\\''")
    return _run(
        work,
        f"export FAKE_TS={turn_state}\n"
        f"response_json='{response_json}'\n"
        f'run_task "$response_json"\n',
    )


def _task() -> dict:
    # `inbox` (nicht in_progress): nur so laeuft run_task an der
    # "bereits in_progress"-Fruehauskunft vorbei bis zu den Gates — und genau so
    # sieht der Incident aus, eine frisch angelegte, noch nicht geclaimte Karte.
    return {
        "id": "t1", "board_id": "b1", "prompt": "DO X",
        "dispatch_attempt_id": "a1", "status": "inbox",
        "assigned_agent_id": AGENT_A,
    }


def _clear_sent(work: Path) -> bool:
    log = work / "tmux.log"
    if not log.exists():
        return False
    return any("/clear" in line for line in log.read_text().splitlines())


def test_board_lead_context_is_not_cleared(tmp_path):
    """Kernfall des Incidents: Lead bekommt eine selbst angelegte Karte."""
    work = _make_workspace(tmp_path)
    res = _dispatch(work, _task(), is_board_lead=True)
    assert res.returncode == 0, res.stderr
    assert not _clear_sent(work), (
        "Board-Lead-Gate unwirksam: /clear lief in den Orchestrierungs-Turn"
    )
    assert "/clear UEBERSPRUNGEN (Board Lead" in res.stdout, res.stdout


def test_working_turn_is_not_cleared(tmp_path):
    """Worker im laufenden Turn (detect_turn_state=working) ebenfalls nicht."""
    work = _make_workspace(tmp_path)
    res = _dispatch(work, _task(), is_board_lead=False, turn_state="working")
    assert res.returncode == 0, res.stderr
    assert not _clear_sent(work), (
        "Turn-Gate unwirksam: /clear lief in einen laufenden Turn"
    )
    assert "Turn laeuft (detect_turn_state=working)" in res.stdout, res.stdout


def test_idle_worker_context_is_still_cleared(tmp_path):
    """Gegenprobe: ohne Lead-Rolle und ohne laufenden Turn bleibt /clear."""
    work = _make_workspace(tmp_path)
    res = _dispatch(work, _task(), is_board_lead=False)
    assert res.returncode == 0, res.stderr
    assert _clear_sent(work), (
        "Gegenrichtung verletzt: ein idler Worker bekommt keinen frischen Context"
    )
    assert "UEBERSPRUNGEN" not in res.stdout, res.stdout


def test_missing_role_key_still_clears(tmp_path):
    """Aelteres Backend ohne den Key darf den Dispatch nicht brechen
    (fail-open): Worker idle → /clear wie vorher."""
    work = _make_workspace(tmp_path)
    res = _dispatch(work, _task())
    assert res.returncode == 0, res.stderr
    assert _clear_sent(work), "fehlender is_board_lead-Key hat den Clear unterdrueckt"
    assert "UEBERSPRUNGEN" not in res.stdout, res.stdout
