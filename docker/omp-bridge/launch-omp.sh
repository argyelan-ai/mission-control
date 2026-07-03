#!/usr/bin/env bash
# docker/omp-bridge/launch-omp.sh — single source of truth for the native omp
# TUI invocation (ADR-049). Used in TWO places so the command never drifts:
#   1. entrypoint.sh — initial Window-0 launch at boot.
#   2. bridge.py     — per-task relaunch (`tmux respawn-window`) for isolation +
#                      the correct per-task --cwd.
#
# Usage:  launch-omp.sh [<cwd>]
#   <cwd>  directory omp starts in (a task's container-view workspace). Defaults
#          to $OMP_DEFAULT_CWD or /workspace.
#
# Env is read from /opt/omp-bridge/omp.env (rendered by entrypoint.sh from the
# Vault-bootstrapped OPENAI_* values) so a `tmux respawn-window` — which does not
# inherit the poller's shell env — still gets the provider/model/profile. This is
# belt-and-suspenders alongside `tmux set-environment -g` in the entrypoint.
set -eu

ENV_FILE="${OMP_ENV_FILE:-/opt/omp-bridge/omp.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

CWD="${1:-${OMP_DEFAULT_CWD:-/workspace}}"
# A vanished per-task worktree must never crash the pane into a respawn loop.
[ -d "$CWD" ] || CWD="${OMP_DEFAULT_CWD:-/workspace}"
[ -d "$CWD" ] || CWD="$HOME"

HOOK="${OMP_HOOK_FILE:-/opt/omp-bridge/turn-end-hook.mjs}"
SELECTOR="${OMP_MODEL_SELECTOR:-qwen-spark/${OPENAI_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}}"

# --approval-mode yolo: the agent runs unattended (no human at the pane to
#   approve tool calls). --allow-home: some ad-hoc tasks have no workspace and
#   land in $HOME. We deliberately do NOT pass --no-session or --mode json: this
#   is the real interactive TUI a human can read and scroll on the Sessions page.
exec omp \
    --hook "$HOOK" \
    --model "$SELECTOR" \
    --cwd "$CWD" \
    --approval-mode yolo \
    --allow-home
