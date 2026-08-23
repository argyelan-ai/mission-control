"""Recycler-Schutz fuer Chat-Turns in poll.sh (Fix 2026-08-23).

recycler.sh misst "untaetig" ausschliesslich an der mtime von
/home/agent/.claude/last-task.marker. Vor diesem Fix beruehrte poll.sh den
Marker NUR im Board-Task-Pfad (run_task + state=working) — eine comm_v2-
Zustellung (Nudge, Message-Flush, Kommentare) startete einen Turn ohne jedes
Aktivitaets-Signal. Folge: Agenten wurden mitten im Chat-Turn gekillt
(Live-Beleg Gruppenlauf 22.08.: zwei Sprecher wurden 2s nach ihrem
Suchergebnis gekillt, ein dritter kam mit schnellerem Werkzeug durch.)

Der Fix ist bewusst SELBSTBEGRENZEND — das unterscheidet ihn vom (falschen)
naiven Weg "Marker bei jeder Nachricht anfassen", der den Recycler dauerhaft
ausgehebelt haette (Speicherleck):
  1. Touch nur bei ERFOLGREICH gepasteter Zustellung (Turn-Start).
  2. protect_chat_turn_if_working: Touch nur solange detect_turn_state
     tatsaechlich "working" meldet UND kein Board-Task laeuft. Endet der
     Turn, enden die Touches — nach RECYCLER_IDLE_MIN echter Untaetigkeit
     wird weiterhin recycelt.

Gleiche Harness wie test_poll_sh_gate.py / test_poll_sh_nudge.py: poll.sh mit
POLL_SH_SOURCE_ONLY=1 sourcen, tmux via PATH-Shim stubben, Turn-State /
Clean-Prompt / Paste-Verify deterministisch ueberschreiben. Der Marker-Pfad
ist via RECYCLER_MARKER_FILE in die Tmpdir umgelenkt (auf macOS existiert
/home/agent nicht).
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
        timeout=60,
    )


def _msg(seq: int, thread_id: str = "T1") -> dict:
    return {
        "id": f"m{seq}", "thread_id": thread_id, "seq": seq, "sender": "user",
        "message_type": "chat", "body": "hallo", "question_meta": None,
    }


def _resp(*messages: dict) -> str:
    return json.dumps({"state": "idle", "new_messages": list(messages)})


def _comments_resp() -> str:
    return json.dumps({
        "state": "idle",
        "new_comments": [{
            "source": "user",
            "task_title": "T",
            "task_id": "tid-1",
            "created_at": "2026-08-23T09:00:00Z",
            "content": "bitte weitermachen",
        }],
    })


def _marker(work: Path) -> Path:
    return work / "marker"


# ── Turn-Start: erfolgreiche Zustellung nullt die Recycler-Uhr ─────────────

def test_nudge_paste_touches_marker(tmp_path):
    """Der Kern des Live-Vorfalls: Nudge gepastet → Marker MUSS frisch sein.

    Ohne den Touch startet der Chat-Turn mit der alten Idle-Zeit — war der
    Agent vorher >= RECYCLER_IDLE_MIN untaetig, killt der Recycler ihn
    Sekunden nach dem Nudge (gemessen 09:20:44 und 09:20:46 am 22.08.).
    """
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _marker(work).exists(), "nudge paste must touch the recycler marker"


def test_nudge_gate_closed_does_not_touch_marker(tmp_path):
    """Gate zu (Agent arbeitet) → kein Paste → KEIN Touch.

    Blosser Nachrichteneingang darf die Recycler-Uhr nicht nullen — sonst
    waere ein gelegentlich angeschriebener Agent nie mehr recycelbar (der
    ausdruecklich verbotene naive Weg)."""
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'FAKE_TS=working\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert not _marker(work).exists(), "no paste happened — marker must stay untouched"


def test_failed_nudge_paste_does_not_touch_marker(tmp_path):
    """Fehlgeschlagener Paste (Verify rot) → kein Turn gestartet → kein Touch."""
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'export MSG_DELIVERY_MODE=nudge\n'
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        'paste_and_submit() { return 1; }\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert not _marker(work).exists(), "failed paste must not touch the marker"


def test_paste_mode_flush_touches_marker(tmp_path):
    """Default-Modus (paste): Volltext-Flush an der Turn-Grenze → Touch."""
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _marker(work).exists(), "flushed message must touch the recycler marker"


def test_paste_mode_queued_does_not_touch_marker(tmp_path):
    """paste-Modus, Agent busy → Message nur gequeued → KEIN Touch."""
    work = _make_workspace(tmp_path)
    resp = _resp(_msg(5)).replace('"', '\\"')
    res = _run(
        work,
        f'FAKE_TS=working\n'
        f'deliver_messages "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert sorted(p.name for p in (work / "q").iterdir()) == ["5__T1.msg"]
    assert not _marker(work).exists(), "queueing alone is not activity"


def test_comments_delivery_touches_marker(tmp_path):
    """new_comments-Zustellung startet ebenfalls einen Turn → Touch."""
    work = _make_workspace(tmp_path)
    resp = _comments_resp().replace('"', '\\"')
    res = _run(
        work,
        f'FAKE_TS=idle\nFAKE_CLEAN=1\n'
        f'deliver_comments "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _marker(work).exists(), "delivered comments must touch the recycler marker"


# ── Turn-Dauer: Schutz nur solange der Turn wirklich laeuft ────────────────

def test_working_chat_turn_touches_marker(tmp_path):
    """Laufender Chat-Turn ohne Board-Task → Marker wird nachgetoucht.

    Deckt Turns > RECYCLER_IDLE_MIN ab und auch Turns, die nicht poll.sh
    gestartet hat (Sessions-Chat-Eingabe direkt in den Container)."""
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        f'CURRENT_TASK_ID=""\n'
        f'FAKE_TS=working\n'
        f'protect_chat_turn_if_working\n',
    )
    assert res.returncode == 0, res.stderr
    assert _marker(work).exists(), "a running chat turn must refresh the marker"


def test_idle_agent_is_not_protected(tmp_path):
    """Recycler behaelt seinen Zweck: echter Idle → KEIN Touch → recycelbar."""
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        f'CURRENT_TASK_ID=""\n'
        f'FAKE_TS=idle\n'
        f'protect_chat_turn_if_working\n',
    )
    assert res.returncode == 0, res.stderr
    assert not _marker(work).exists(), "an idle agent must stay recyclable"


def test_crashed_turn_is_not_protected(tmp_path):
    """Crashed Turn → kein Touch: der Recycler-Respawn IST hier die Heilung."""
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        f'CURRENT_TASK_ID=""\n'
        f'FAKE_TS=crashed\n'
        f'protect_chat_turn_if_working\n',
    )
    assert res.returncode == 0, res.stderr
    assert not _marker(work).exists(), "a crashed turn must not block recycling"


def test_active_board_task_leaves_protection_to_task_path(tmp_path):
    """Board-Task aktiv → early return, kein Touch aus dem Chat-Schutz.

    Der Task-Pfad signalisiert selbst (TASK_LOCK_FILE + working-Touch im
    Poll-Loop) und bleibt dadurch byte-identisch zum Verhalten vor dem Fix."""
    work = _make_workspace(tmp_path)
    res = _run(
        work,
        f'CURRENT_TASK_ID="task-123"\n'
        f'FAKE_TS=working\n'
        f'protect_chat_turn_if_working\n',
    )
    assert res.returncode == 0, res.stderr
    assert not _marker(work).exists(), "task path must keep its own signalling"
