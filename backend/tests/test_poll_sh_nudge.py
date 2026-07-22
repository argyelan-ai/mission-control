"""Nudge+Pull delivery mode for poll.sh (W2.1).

`MSG_DELIVERY_MODE=nudge` replaces full-text pasting of comm_v2 `new_messages`
with a single fixed wake-up line ("📬 …"); the agent then fetches the content
itself via `mc inbox`. The default (`paste`, or unset) must stay byte-identical
to today's behaviour — the live fleet runs on it.

Same harness as test_poll_sh_gate.py: source poll.sh with POLL_SH_SOURCE_ONLY=1
(functions only), stub tmux via a PATH shim, override the turn-state /
clean-prompt / paste-verify helpers so the gate decision is deterministic.
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

TMUX_SHIM = """#!/usr/bin/env bash
if [ -n "${TMUX_LOG:-}" ]; then
    echo "$*" >> "$TMUX_LOG"
fi
exit 0
"""

# Adds MSG_DELIVERY_MODE + nudge state/tmp files on top of the gate harness.
PRELUDE = r"""
set -uo pipefail
export POLL_SH_SOURCE_ONLY=1
export POLL_LIB_DIR="$WORK/lib"
export MSG_QUEUE_DIR="$WORK/q"
export MSG_ACK_DIR="$WORK/ack"
export NUDGE_STATE_FILE="$WORK/nudge-state"
export NUDGE_TMP_FILE="$WORK/nudge.txt"
export MC_API_URL=http://example.invalid MC_TOKEN=t SESSION_NAME=test
export READY_TIMEOUT_SEC=0 READY_POLL_INTERVAL_SEC=0
export PASTE_VERIFY_DELAY_SEC=0 PASTE_RETRY_DELAY_SEC=0
export PATH="$WORK/bin:$PATH"
export TMUX_LOG="$WORK/tmux.log"

source "$POLLSH"

detect_turn_state() { echo "${FAKE_TS:-idle}"; }
wait_for_clean_prompt() { [ "${FAKE_CLEAN:-1}" = "1" ]; }
verify_paste_landed() { return 0; }
"""


def _make_workspace(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "lib").mkdir(parents=True)
    (work / "q").mkdir()
    (work / "ack").mkdir()
    (work / "bin").mkdir()
    for lib in ("turn-state", "ui-detect", "paste-verify"):
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
        timeout=60,
    )


def _msg(seq: int, thread_id: str = "T1") -> dict:
    return {
        "id": f"m{seq}", "thread_id": thread_id, "seq": seq, "sender": "user",
        "message_type": "chat", "body": "hallo", "question_meta": None,
    }


def _resp(*messages: dict) -> str:
    return json.dumps({"state": "idle", "new_messages": list(messages)})


def _paste_order(tmux_log: Path) -> list[str]:
    if not tmux_log.exists():
        return []
    order = []
    for line in tmux_log.read_text().splitlines():
        if line.startswith("load-buffer "):
            order.append(Path(line.split(" ", 1)[1]).name)
    return order


# ── Default mode (paste) stays byte-identical: queues, no nudge ────────────
def test_default_mode_is_paste_and_queues(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    # No MSG_DELIVERY_MODE exported → default "paste". Agent busy → queued.
    res = _run(
        work,
        f'FAKE_TS=working\n'
        f'deliver_messages "{resp}"\n'
        f'echo "MODE=$MSG_DELIVERY_MODE"\n',
    )
    assert res.returncode == 0, res.stderr
    assert "MODE=paste" in res.stdout
    # paste-mode queue file written, no nudge state, nudge line never pasted.
    assert sorted(p.name for p in (work / "q").iterdir()) == ["5__T1.msg"]
    assert not (work / "nudge-state").exists()
    assert _paste_order(work / "tmux.log") == []


# ── Nudge mode: new higher seq at idle → one wake-up pasted, state written ──
def test_nudge_pastes_wakeup_and_records_state(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5), _msg(6)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    # The fixed wake-up file was pasted (basename nudge.txt), nothing queued.
    assert _paste_order(work / "tmux.log") == ["nudge.txt"]
    assert list((work / "q").iterdir()) == []
    # State high-water = highest seq seen.
    state = (work / "nudge-state").read_text().split()
    assert state[0] == "6"
    # The pasted content is the constant wake-up line.
    assert "mc inbox" in (work / "nudge.txt").read_text()


# ── Nudge mode: same seq within remind window → no second nudge ────────────
def test_nudge_dedups_same_seq(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(6)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge NUDGE_REMIND_SECONDS=600\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        # Pre-existing state: seq 6 already nudged just now.
        f'printf "6 %s\\n" "$(date +%s)" > "$NUDGE_STATE_FILE"\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _paste_order(work / "tmux.log") == []  # no re-nudge


# ── Nudge mode: remind timer elapsed → re-nudge same seq ───────────────────
def test_nudge_reminds_after_timeout(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(6)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge NUDGE_REMIND_SECONDS=600\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        # State says seq 6 was nudged at epoch 0 → now-0 >> 600 → remind.
        f'printf "6 0\\n" > "$NUDGE_STATE_FILE"\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _paste_order(work / "tmux.log") == ["nudge.txt"]


# ── Nudge mode: gate closed (agent busy) → no paste, state unchanged ───────
def test_nudge_holds_when_gate_closed(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(9)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'FAKE_TS=working\n'  # busy → msg_gate_open returns closed
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _paste_order(work / "tmux.log") == []
    assert not (work / "nudge-state").exists()  # nothing recorded, retry next poll


# ── Nudge mode: empty new_messages → state cleared ─────────────────────────
def test_nudge_clears_state_when_empty(tmp_path):
    work = _make_workspace(tmp_path)
    empty = json.dumps({"state": "idle", "new_messages": []}).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'printf "6 0\\n" > "$NUDGE_STATE_FILE"\n'
        f'deliver_messages "{empty}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert not (work / "nudge-state").exists()
    assert _paste_order(work / "tmux.log") == []


# ── Nudge mode: acked_seq param never built from local ack files ───────────
def test_nudge_never_builds_acked_seq_param(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        # Even with a stale local ack file present, nudge mode must emit nothing.
        f'echo 42 > "$MSG_ACK_DIR/T1"\n'
        f'echo "ACKPARAM=[$(build_acked_seq_param)]"\n',
    )
    assert res.returncode == 0, res.stderr
    ackline = [l for l in res.stdout.splitlines() if l.startswith("ACKPARAM=")][0]
    assert ackline == "ACKPARAM=[]", f"nudge mode must not build acked_seq, got {ackline!r}"


# ── Paste mode still builds acked_seq from local files (regression) ────────
def test_paste_mode_still_builds_acked_seq(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        # Default paste mode: local ack file must still drive acked_seq.
        f'echo 42 > "$MSG_ACK_DIR/T1"\n'
        f'echo "ACKPARAM=$(build_acked_seq_param)"\n',
    )
    assert res.returncode == 0, res.stderr
    from urllib.parse import unquote
    ackline = [l for l in res.stdout.splitlines() if l.startswith("ACKPARAM=")][0]
    acked = json.loads(unquote(ackline[len("ACKPARAM=") :]))
    assert acked == {"T1": 42}
