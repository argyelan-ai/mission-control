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


# Capture the CONTENT of each nudge paste (not just the filename) so tests can
# assert consecutive nudges carry distinct fingerprints. Overrides
# paste_and_submit to append the pasted file's first line to $NUDGE_PASTE_LOG.
CAPTURE_PASTE = r"""
paste_and_submit() {
    local f="$1"; [ "$1" = "--no-fail-open" ] && f="$2"
    if [ -n "${NUDGE_PASTE_LOG:-}" ]; then head -1 "$f" >> "$NUDGE_PASTE_LOG"; fi
    return 0
}
"""


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
    # The wake-up file was pasted (basename nudge.txt), nothing queued.
    assert _paste_order(work / "tmux.log") == ["nudge.txt"]
    assert list((work / "q").iterdir()) == []
    # Per-thread state line: "<thread_id> <seq> <epoch>" with the thread's max seq.
    state = (work / "nudge-state").read_text().split()
    assert state[0] == "T1" and state[1] == "6"
    # The pasted wake-up carries the unique token + the mc inbox instruction.
    pasted = (work / "nudge.txt").read_text()
    assert "mc inbox" in pasted and "bis seq 6" in pasted


# ── Nudge mode: same seq within remind window → no second nudge ────────────
def test_nudge_dedups_same_seq(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(6)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge NUDGE_REMIND_SECONDS=600\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        # Pre-existing per-thread state: T1 seq 6 already nudged just now.
        f'printf "T1 6 %s\\n" "$(date +%s)" > "$NUDGE_STATE_FILE"\n'
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
        # State says T1 seq 6 was nudged at epoch 0 → now-0 >> 600 → remind.
        f'printf "T1 6 0\\n" > "$NUDGE_STATE_FILE"\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _paste_order(work / "tmux.log") == ["nudge.txt"]


# ── Finding 1: per-thread dedup — new low-seq thread nudges past acked high one
def test_nudge_new_thread_fires_despite_higher_acked_thread(tmp_path):
    work = _make_workspace(tmp_path)
    # Thread A (seq 8) already nudged; a fresh Thread B arrives with seq 2.
    # Global-max dedup would hide B behind A's 8 (2 < 8) until the remind timer.
    # Per-thread dedup must fire immediately because B seq 2 > B's stored 0.
    resp = _resp(_msg(8, "A"), _msg(2, "B")).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge NUDGE_REMIND_SECONDS=600\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        # A already nudged just now (fresh timestamp → A alone would not remind).
        f'printf "A 8 %s\\n" "$(date +%s)" > "$NUDGE_STATE_FILE"\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _paste_order(work / "tmux.log") == ["nudge.txt"], "new thread B must nudge now"
    # State now carries both threads at their current max seq.
    lines = sorted(
        l.split()[0:2] for l in (work / "nudge-state").read_text().splitlines() if l.strip()
    )
    assert lines == [["A", "8"], ["B", "2"]]


# ── Finding 2: two consecutive nudges carry DISTINCT fingerprints ──────────
def test_consecutive_nudges_have_distinct_fingerprints(tmp_path):
    work = _make_workspace(tmp_path)
    # First nudge at seq 6, then a higher seq 7 arrives → second nudge. The
    # pasted first lines must differ, so a stale scrollback fingerprint can't
    # false-pass the second paste-verify.
    resp1 = _resp(_msg(6)).replace('"', '\\"')
    resp2 = _resp(_msg(7)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'export NUDGE_PASTE_LOG="$WORK/paste-content.log"\n'
        + CAPTURE_PASTE
        + f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'deliver_messages "{resp1}"\n'
        f'deliver_messages "{resp2}"\n',
    )
    assert res.returncode == 0, res.stderr
    lines = (work / "paste-content.log").read_text().splitlines()
    assert len(lines) == 2, f"expected two nudges, got {lines}"
    assert lines[0] != lines[1], f"fingerprints must differ: {lines}"
    assert "bis seq 6" in lines[0] and "bis seq 7" in lines[1]


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
        f'printf "T1 6 0\\n" > "$NUDGE_STATE_FILE"\n'
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
