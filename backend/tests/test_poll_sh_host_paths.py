"""Host-redirectable temp-file paths for the shared poll.sh (comm_v2 Boss-Host).

The shared poll.sh writes the dispatched task prompt and the new-comments prompt
to temp files before pasting them into the harness pane. In a container /tmp is
isolated per container, so a fixed `/tmp/current_task_prompt.txt` /
`/tmp/new_comments_prompt.txt` is safe. Host agents (Boss, kimi-host) share the
host's /tmp — two poll.sh loops would overwrite each other's prompt and paste
the wrong task. The host entrypoint therefore redirects both files into the
agent's own config dir via TASK_PROMPT_FILE / COMMENTS_PROMPT_FILE.

Two guarantees under test:
  1. Default (unset) stays byte-identical to the live fleet: /tmp paths.
  2. When overridden, poll.sh writes to — and pastes from — the override path.

Same harness as test_poll_sh_nudge.py: source poll.sh with POLL_SH_SOURCE_ONLY=1
(functions only), stub tmux via a PATH shim, override paste_and_submit to record
the path it was handed.
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
exit 0
"""

PRELUDE = r"""
set -uo pipefail
export POLL_SH_SOURCE_ONLY=1
export POLL_LIB_DIR="$WORK/lib"
export MC_API_URL=http://example.invalid MC_TOKEN=t SESSION_NAME=test
export PATH="$WORK/bin:$PATH"

source "$POLLSH"

# Record every path handed to paste_and_submit (skip the --no-fail-open flag),
# so tests can assert which temp file poll.sh pasted from.
paste_and_submit() {
    local f="$1"; [ "$1" = "--no-fail-open" ] && f="$2"
    echo "$f" >> "$WORK/pasted.log"
    return 0
}
"""


def _make_workspace(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "lib").mkdir(parents=True)
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
        [BASH, "-c", script], capture_output=True, text=True, timeout=60
    )


def _pasted(work: Path) -> list[str]:
    log = work / "pasted.log"
    return log.read_text().splitlines() if log.exists() else []


def _comment_resp(body: str = "bitte weiter") -> str:
    return json.dumps(
        {
            "state": "idle",
            "new_comments": [
                {
                    "task_id": "task-1",
                    "task_title": "Demo",
                    "created_at": "2026-07-27T10:00:00",
                    "content": body,
                    "source": "user",
                }
            ],
        }
    )


# ── Default: byte-identical /tmp paths (the live fleet runs on this) ──────────
def test_comments_default_path_is_tmp(tmp_path):
    work = _make_workspace(tmp_path)
    resp = _comment_resp().replace('"', '\\"')
    res = _run(work, f'deliver_comments "{resp}"\n')
    assert res.returncode == 0, res.stderr
    assert _pasted(work) == ["/tmp/new_comments_prompt.txt"]


# ── Override: comments prompt is written to and pasted from the host path ─────
def test_comments_prompt_honors_override(tmp_path):
    work = _make_workspace(tmp_path)
    target = work / ".new-comments-prompt.txt"
    resp = _comment_resp("host-isolated comment").replace('"', '\\"')
    res = _run(
        work,
        f'export COMMENTS_PROMPT_FILE="{target}"\n'
        f'deliver_comments "{resp}"\n',
    )
    assert res.returncode == 0, res.stderr
    assert _pasted(work) == [str(target)]
    assert target.exists()
    assert "host-isolated comment" in target.read_text()


# ── Source-only: TASK_PROMPT_FILE defaults to /tmp, honors override ───────────
@pytest.mark.parametrize(
    "override, expected",
    [
        (None, "/tmp/current_task_prompt.txt"),
        ("/srv/boss/.current-task-prompt.txt", "/srv/boss/.current-task-prompt.txt"),
    ],
)
def test_task_prompt_file_resolution(tmp_path, override, expected):
    work = _make_workspace(tmp_path)
    exp = f'export TASK_PROMPT_FILE="{override}"\n' if override else ""
    res = _run(work, exp + 'echo "R=$TASK_PROMPT_FILE"\n')
    assert res.returncode == 0, res.stderr
    assert f"R={expected}" in res.stdout
