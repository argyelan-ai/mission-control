#!/bin/bash
# statusline-mc.sh — Claude Code statusLine hook.
#
# Wired into settings.json as `"statusLine": {"type":"command","command":
# "bash /home/agent/.claude/statusline-mc.sh"}` for claude-harness agents
# (see backend/templates/cli_agent_settings.json.j2, plugin_manager.py's
# `status_line` param). Claude Code invokes this on every prompt, piping a
# JSON object on stdin that includes `session_id` and the CLI's own live
# token accounting (`context_window.used_percentage`,
# `context_window.current_usage.{input_tokens,output_tokens,
# cache_read_input_tokens,cache_creation_input_tokens}`).
#
# This script mirrors that JSON, verbatim and atomically, to
# $HOME/.claude/statusline-state/<session_id>.json so MC's backend can read
# it as ground truth for the chat context meter (see
# transcript_chat.read_statusline_state) instead of guessing from a static
# model->context-window map. It then prints a minimal one-line status back
# to stdout — that's what Claude Code renders as the actual statusline text.
#
# Every failure mode is swallowed: a broken statusline must never break the
# CLI turn itself. Worst case, MC's backend just falls back to its estimate.
#
# No jq — not installed in the agent images (mc-claude-agent's Dockerfile
# has no jq layer). python3 is already guaranteed present and is how
# poll.sh itself parses JSON (see its bootstrap-response handling), so this
# reuses that same portable pattern instead of adding a new dependency.
# No network calls — purely local disk I/O.

set -u

STATE_DIR="${HOME:-/home/agent}/.claude/statusline-state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

INPUT="$(cat 2>/dev/null)" || true

PY_SCRIPT="$(mktemp 2>/dev/null)" || exit 0
trap 'rm -f "$PY_SCRIPT"' EXIT

cat > "$PY_SCRIPT" <<'PYEOF'
import glob
import json
import os
import sys
import tempfile

state_dir = sys.argv[1]

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if not isinstance(data, dict):
    sys.exit(0)

session_id = data.get("session_id") or "unknown"
ctx = data.get("context_window") or {}
used_pct = ctx.get("used_percentage")

model = data.get("model") or {}
if isinstance(model, dict):
    model_name = model.get("display_name") or model.get("name")
else:
    model_name = None
model_name = model_name or data.get("model_display_name") or "?"

# Atomic write: tmp file in the same dir, then os.replace (same-filesystem
# rename is atomic on POSIX) — a reader mid-write never sees a truncated or
# partial JSON file (matters since the backend polls this on every usage
# event while a turn is in flight).
try:
    target = os.path.join(state_dir, f"{session_id}.json")
    fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
except Exception:
    pass

# Prune to the newest 20 state files — one per session, otherwise this
# directory accumulates forever across rollovers/restarts.
try:
    files = sorted(
        glob.glob(os.path.join(state_dir, "*.json")),
        key=os.path.getmtime,
        reverse=True,
    )
    for stale in files[20:]:
        try:
            os.remove(stale)
        except OSError:
            pass
except Exception:
    pass

if isinstance(used_pct, (int, float)):
    print(f"{model_name} · ctx {used_pct:.0f}%")
else:
    print(model_name)
PYEOF

printf '%s' "$INPUT" | python3 "$PY_SCRIPT" "$STATE_DIR" 2>/dev/null || true
