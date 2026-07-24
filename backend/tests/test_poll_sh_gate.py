"""Turn-boundary gate for message delivery in poll.sh (Interaction Model 2.0).

poll.sh delivers comm_v2 `new_messages` (Task 4) through a turn-boundary gate
instead of pasting immediately: while claude is working / the prompt is not
clean the message is queued (one seq-named file per message); at the next idle
turn boundary the queue is flushed in seq order. Only a message that was
actually PASTED advances the per-thread ack high-water (`acked_seq`), which the
next poll sends back — matching the backend's at-least-once cursor semantics.

This harness sources poll.sh with `POLL_SH_SOURCE_ONLY=1` (functions only, no
poll loop), redirects the lib dir + queue/ack dirs into a tmpdir, stubs `tmux`
via a PATH shim, and overrides the turn-state / clean-prompt / paste-verify
helpers so the gate decision is deterministic.

Requires bash >= 4 for nothing in particular any more (ack storage is file-based
so bash 3.2 works too), but flush relies on `sort -n` + parameter expansion that
are portable. macOS ships bash 3.2 which is sufficient here.
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
# and no-op. paste_and_submit calls `tmux load-buffer <file>` first, so the
# load-buffer lines reveal which queue files were pasted and in what order.
TMUX_SHIM = """#!/usr/bin/env bash
if [ -n "${TMUX_LOG:-}" ]; then
    echo "$*" >> "$TMUX_LOG"
fi
exit 0
"""

# Prelude sourced into every scenario: neutralise container paths, point the
# lib/queue/ack dirs at the tmpdir, zero out all sleeps, and override the three
# helpers that touch the real terminal so the gate is fully controllable.
#   FAKE_TS      -> what detect_turn_state reports (working|idle|crashed)
#   FAKE_CLEAN   -> 1 = wait_for_clean_prompt succeeds, else fails
PRELUDE = r"""
set -uo pipefail
export POLL_SH_SOURCE_ONLY=1
export POLL_LIB_DIR="$WORK/lib"
export MSG_QUEUE_DIR="$WORK/q"
export MSG_ACK_DIR="$WORK/ack"
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


def _msg(seq: int, thread_id: str = "T1", body: str = "hallo") -> dict:
    return {
        "id": f"m{seq}",
        "thread_id": thread_id,
        "seq": seq,
        "sender": "user",
        "message_type": "chat",
        "body": body,
        "question_meta": None,
    }


def _resp(*messages: dict) -> str:
    return json.dumps({"state": "idle", "new_messages": list(messages)})


def _paste_order(tmux_log: Path) -> list[str]:
    """Basenames of queue files pasted, in the order tmux load-buffer saw them."""
    if not tmux_log.exists():
        return []
    order = []
    for line in tmux_log.read_text().splitlines():
        if line.startswith("load-buffer "):
            order.append(Path(line.split(" ", 1)[1]).name)
    return order


# ── Scenario (a): agent busy → message queued, nothing pasted ──────────────
def test_busy_queues_without_paste(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'FAKE_TS=working\n'
        f'deliver_messages "{resp}"\n'
        f'echo "ACKPARAM=$(build_acked_seq_param)"\n',
    )
    assert res.returncode == 0, res.stderr

    queued = sorted(p.name for p in (work / "q").iterdir())
    assert queued == ["5__T1.msg"], f"expected message queued, got {queued}"

    # No paste happened while busy.
    assert _paste_order(work / "tmux.log") == []

    # And nothing acked — pasting is the consumption the ack waits for.
    ackline = [l for l in res.stdout.splitlines() if l.startswith("ACKPARAM=")][0]
    assert ackline == "ACKPARAM=", f"expected empty ack while queued, got {ackline!r}"


# ── Scenario (b): idle → flush pastes in seq order and empties queue ───────
def test_idle_flushes_in_seq_order_and_empties(tmp_path):
    work = _make_workspace(tmp_path)
    # Deliver seq 12 BEFORE seq 5 in the payload to prove sort -n ordering.
    resp = _resp(_msg(12), _msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'deliver_messages "{resp}"\n'
        f'echo "ACKPARAM=$(build_acked_seq_param)"\n',
    )
    assert res.returncode == 0, res.stderr

    # Queue emptied.
    assert list((work / "q").iterdir()) == []

    # Pasted in seq order 5 then 12, regardless of payload order.
    assert _paste_order(work / "tmux.log") == ["5__T1.msg", "12__T1.msg"]

    # Ack high-water is the highest pasted seq for the thread.
    ackline = [l for l in res.stdout.splitlines() if l.startswith("ACKPARAM=")][0]
    from urllib.parse import unquote

    acked = json.loads(unquote(ackline[len("ACKPARAM=") :]))
    assert acked == {"T1": 12}


# ── Scenario (c): queued-not-acked → acked only after flush ────────────────
def test_ack_only_after_flush(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(7)).replace('"', '\\"')
    # First poll: agent busy → queued, NOT acked. Then a later poll (backend
    # re-delivers because unacked) with the agent idle → flush → ack.
    res = _run(
        work,
        f'FAKE_TS=working\n'
        f'deliver_messages "{resp}"\n'
        f'echo "PHASE1_QUEUE=$(ls "$MSG_QUEUE_DIR")"\n'
        f'echo "PHASE1_ACK=$(build_acked_seq_param)"\n'
        f'FAKE_TS=idle\n'
        f'deliver_messages "{resp}"\n'
        f'echo "PHASE2_QUEUE=[$(ls "$MSG_QUEUE_DIR")]"\n'
        f'echo "PHASE2_ACK=$(build_acked_seq_param)"\n',
    )
    assert res.returncode == 0, res.stderr
    out = {k: v for k, v in (l.split("=", 1) for l in res.stdout.splitlines() if "=" in l)}

    # Phase 1: queued, no ack yet.
    assert out["PHASE1_QUEUE"] == "7__T1.msg"
    assert out["PHASE1_ACK"] == ""

    # Phase 2: flushed (queue empty) and now acked.
    assert out["PHASE2_QUEUE"] == "[]"
    from urllib.parse import unquote

    assert json.loads(unquote(out["PHASE2_ACK"])) == {"T1": 7}

    # Exactly one paste total across the two polls (no double-paste on redelivery).
    assert _paste_order(work / "tmux.log") == ["7__T1.msg"]


# ── Scenario (d): --no-fail-open stops flush mid-way (agent goes busy) ─────
def test_no_fail_open_stops_flush_and_keeps_rest_queued(tmp_path):
    work = _make_workspace(tmp_path)
    # Two queued messages. The prompt is clean for the first paste but goes
    # non-clean for the second (claude started a new turn). --no-fail-open must
    # then NOT paste the second: seq 5 gets pasted+acked+removed, seq 12 stays
    # queued and unacked, and flush_msg_queue returns non-zero.
    resp = _resp(_msg(12), _msg(5)).replace('"', '\\"')
    res = _run(
        work,
        # wait_for_clean_prompt: succeed only the first CLEAN_OK_TIMES calls.
        'echo 0 > "$WORK/clean_calls"\n'
        'wait_for_clean_prompt() {\n'
        '    local n; n=$(cat "$WORK/clean_calls"); n=$((n+1)); echo "$n" > "$WORK/clean_calls"\n'
        '    [ "$n" -le "${CLEAN_OK_TIMES:-1}" ]\n'
        '}\n'
        'export CLEAN_OK_TIMES=1\n'
        f'queue_or_deliver "{resp}"\n'
        'if flush_msg_queue; then FLUSH_RC=0; else FLUSH_RC=$?; fi\n'
        'echo "FLUSH_RC=$FLUSH_RC"\n'
        'echo "QUEUE=$(ls "$MSG_QUEUE_DIR")"\n'
        'echo "ACKPARAM=$(build_acked_seq_param)"\n',
    )
    assert res.returncode == 0, res.stderr
    out = {k: v for k, v in (l.split("=", 1) for l in res.stdout.splitlines() if "=" in l)}
    assert out["FLUSH_RC"] == "1"
    assert out["QUEUE"] == "12__T1.msg"        # second message still queued
    assert _paste_order(work / "tmux.log") == ["5__T1.msg"]  # only the first pasted

    from urllib.parse import unquote

    assert json.loads(unquote(out["ACKPARAM"])) == {"T1": 5}  # acked only what pasted


# ── Sanity: non-pilot response (no new_messages key) is ignored ────────────
def test_response_without_new_messages_key_is_noop(tmp_path):
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        'if response_has_new_messages \'{"state":"idle","new_comments":[]}\'; then '
        'echo HAS; else echo NONE; fi\n',
    )
    assert res.returncode == 0, res.stderr
    assert "NONE" in res.stdout
