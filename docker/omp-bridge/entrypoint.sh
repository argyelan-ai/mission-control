#!/usr/bin/env bash
# docker/omp-bridge/entrypoint.sh — Container PID 1 (ADR-049, supersedes the
# ADR-045 headless-one-shot boot).
#
# 3-window tmux, same bootstrap-token pattern as mc-agent-base, but now:
#   Window 0 = the NATIVE omp TUI  (`launch-omp.sh` -> a real, scrollable omp
#              chat session the Sessions page attaches to; loads the turn-end
#              hook and boots STRAIGHT to chat via setupVersion:1).
#   Window 1 = bridge.py --serve   (the poll driver: injects tasks into Window 0
#              via `tmux send-keys @file` and reads the hook signal).
#   Window 2 = omp-recycler.sh     (keeps BOTH the TUI and the bridge alive).
#
# omp is the persistent Window-0 process (not a bridge subprocess). The bridge
# relaunches it per task (`tmux respawn-window`) for isolation + the correct
# --cwd, and SIGKILLs+relaunches it on a watchdog trip.
#
# Do NOT `docker build` this from the omp-runtime workflow — image build is GATED
# (the operator runs scripts/build-agent-images.sh mc-omp-agent). This file is authored
# real so the build, when run, works.
set -eu

SESSION="${AGENT_NAME:-omp-agent}"
BRIDGE=/opt/omp-bridge/bridge.py
HOOK_FILE=/opt/omp-bridge/turn-end-hook.mjs
LAUNCHER=/opt/omp-bridge/launch-omp.sh
OMP_DEFAULT_CWD="${OMP_DEFAULT_CWD:-/workspace}"

# ── 1. Bootstrap tokens from MC (analog to mc-agent-base entrypoint) ─────────
# GET /api/v1/internal/bootstrap returns MC_AGENT_TOKEN + OPENAI_BASE_URL +
# OPENAI_MODEL (+ OPENAI_API_KEY, GH_TOKEN, AGENT_RECYCLER_ENABLED) — Vault-
# decrypted, no plaintext on disk. The omp runtime is OpenAI-compatible: NO
# anthropic token is exported here (the old sketch's ANTHROPIC_OAUTH_TOKEN
# re-export was wrong for Qwen routing and is DROPPED).
BOOTSTRAP_URL="${MC_API_URL:-http://backend:8000}/api/v1/internal/bootstrap?agent_name=${AGENT_NAME}"
BOOTSTRAP_RESPONSE=""
for _attempt in 1 2 3 4 5 6; do
    BOOTSTRAP_RESPONSE=$(curl -sf --max-time 5 "$BOOTSTRAP_URL" 2>/dev/null) && break
    echo "[entrypoint] Bootstrap Versuch $_attempt fehlgeschlagen, retry in 3s..."
    sleep 3
done

if [ -n "$BOOTSTRAP_RESPONSE" ]; then
    _EXPORTS=$(echo "$BOOTSTRAP_RESPONSE" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k, v in d.items():
        if k in ("MC_AGENT_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "GH_TOKEN", "AGENT_RECYCLER_ENABLED"):
            print(f"{k}={v}")
except Exception:
    sys.exit(1)
' 2>/dev/null) || _EXPORTS=""
    if [ -n "$_EXPORTS" ]; then
        _NEW_TOKEN=$(echo "$_EXPORTS" | grep '^MC_AGENT_TOKEN=' | cut -d= -f2-)
        _NEW_API_KEY=$(echo "$_EXPORTS" | grep '^OPENAI_API_KEY=' | cut -d= -f2-)
        _NEW_BASE_URL=$(echo "$_EXPORTS" | grep '^OPENAI_BASE_URL=' | cut -d= -f2-)
        _NEW_MODEL=$(echo "$_EXPORTS" | grep '^OPENAI_MODEL=' | cut -d= -f2-)
        _NEW_GH_TOKEN=$(echo "$_EXPORTS" | grep '^GH_TOKEN=' | cut -d= -f2-)
        _NEW_RECYCLER=$(echo "$_EXPORTS" | grep '^AGENT_RECYCLER_ENABLED=' | cut -d= -f2-)
        if [ -n "$_NEW_TOKEN" ]; then
            export MC_AGENT_TOKEN="$_NEW_TOKEN"; export MC_TOKEN="$_NEW_TOKEN"
        else
            export MC_AGENT_TOKEN="${MC_TOKEN:-}"
        fi
        [ -n "$_NEW_API_KEY" ]  && export OPENAI_API_KEY="$_NEW_API_KEY"
        [ -n "$_NEW_BASE_URL" ] && export OPENAI_BASE_URL="$_NEW_BASE_URL"
        [ -n "$_NEW_MODEL" ]    && export OPENAI_MODEL="$_NEW_MODEL"
        export AGENT_RECYCLER_ENABLED="${_NEW_RECYCLER:-${AGENT_RECYCLER_ENABLED:-true}}"
        if [ -n "$_NEW_GH_TOKEN" ]; then
            export GH_TOKEN="$_NEW_GH_TOKEN"
            GIT_CRED_FILE="${HOME}/.git-credentials"
            echo "https://oauth:${GH_TOKEN}@github.com" > "$GIT_CRED_FILE"
            chmod 600 "$GIT_CRED_FILE"
            git config --global credential.helper "store --file=${GIT_CRED_FILE}"
            git config --global user.email "${AGENT_NAME}@mc.local"
            git config --global user.name "${AGENT_NAME}"
        fi
        echo "[entrypoint] Bootstrap OK — Tokens aus Vault geladen (OpenAI/Qwen routing)"
    else
        echo "[entrypoint] Bootstrap JSON-Parse fehlgeschlagen — Fallback auf Env-Vars"
        # Defense layer 2: resolve MC_TOKEN from per-agent env var (loaded by
        # env_file: docker/.env.agents) when MC_TOKEN is still blank.
        if [ -z "${MC_TOKEN:-}" ]; then
            _agent_upper=$(printf '%s' "${AGENT_NAME:-}" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
            _tok_var="MC_TOKEN_${_agent_upper}"
            eval "_tok_val=\${${_tok_var}:-}"
            if [ -n "$_tok_val" ]; then
                export MC_TOKEN="$_tok_val"
                echo "[entrypoint] MC_TOKEN resolved from ${_tok_var} (env_file fallback)"
            fi
        fi
        export MC_AGENT_TOKEN="${MC_TOKEN:-}"
    fi
else
    echo "[entrypoint] Bootstrap fehlgeschlagen — Fallback auf Env-Vars"
    # Defense layer 2: resolve MC_TOKEN from per-agent env var (loaded by
    # env_file: docker/.env.agents) when MC_TOKEN is still blank.
    if [ -z "${MC_TOKEN:-}" ]; then
        _agent_upper=$(printf '%s' "${AGENT_NAME:-}" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
        _tok_var="MC_TOKEN_${_agent_upper}"
        eval "_tok_val=\${${_tok_var}:-}"
        if [ -n "$_tok_val" ]; then
            export MC_TOKEN="$_tok_val"
            echo "[entrypoint] MC_TOKEN resolved from ${_tok_var} (env_file fallback)"
        fi
    fi
    export MC_AGENT_TOKEN="${MC_TOKEN:-}"
fi

# ── 2. Render omp's native models.yml provider from the OpenAI-style env ─────
# omp resolves models PROFILE-FIRST: with OMP_PROFILE=mc-agent it reads
# $HOME/.omp/profiles/mc-agent/agent/models.yml (a file at $PI_CODING_AGENT_DIR
# is IGNORED once OMP_PROFILE is set). omp's built-in `openai` provider does NOT
# resolve a vLLM-served model from OPENAI_BASE_URL, so a models.yml is mandatory.
# We render a dedicated `mc-openai` provider (auth: none = keyless vLLM) so
# runtime.endpoint stays the single source of truth and no token routing is
# duplicated. bridge.py selects `mc-openai/${OPENAI_MODEL}`.
OMP_PROFILE="${OMP_PROFILE:-mc-agent}"
MODELS_DIR="${HOME}/.omp/profiles/${OMP_PROFILE}/agent"
mkdir -p "$MODELS_DIR"
# No baked-in defaults (ADR-054): the model/endpoint MUST come from the MC
# bootstrap (runtime row) or the rendered .env. A silent fallback to a stale
# model caused exactly the drift bug this feature removes — fail loudly.
if [ -z "${OPENAI_BASE_URL:-}" ] || [ -z "${OPENAI_MODEL:-}" ]; then
    echo "[entrypoint] FATAL: OPENAI_BASE_URL/OPENAI_MODEL not set (bootstrap failed and no env fallback) — refusing to boot with an unknown model" >&2
    exit 1
fi
_BASE_URL="${OPENAI_BASE_URL}"
_MODEL="${OPENAI_MODEL}"
cat > "${MODELS_DIR}/models.yml" <<YAML
providers:
  mc-openai:
    name: MC OpenAI-compatible endpoint
    baseUrl: ${_BASE_URL}
    api: openai-completions
    auth: none
    models:
      - id: ${_MODEL}
        name: MC model
        # Without this flag omp renders vLLM's separated reasoning deltas
        # as plain assistant text instead of a collapsible thinking block.
        # Harmless for non-reasoning models (the field simply never arrives).
        reasoning: true
        contextWindow: 262144
        maxTokens: 65536
YAML
export OMP_PROFILE
export OMP_MODEL_SELECTOR="mc-openai/${_MODEL}"
echo "[entrypoint] models.yml rendered at ${MODELS_DIR}/models.yml (provider mc-openai -> ${_BASE_URL}, model ${_MODEL})"

# ── 2b. Skip the first-run setup wizard so the TUI boots STRAIGHT to chat ────
# Verified in-container (omp v16.2.13): a hand-written config.yml is NOT honored
# — omp normalizes its own config store. Use `omp config set`, which persists to
# the profile's config.yml where omp actually reads it. BOTH keys are required:
# `startup.setupWizard=false` skips onboarding and `setupVersion` marks it done.
omp config set startup.setupWizard false >/dev/null 2>&1 \
    && omp config set setupVersion 1 >/dev/null 2>&1 \
    && echo "[entrypoint] wizard skipped (startup.setupWizard=false, setupVersion=1)" \
    || echo "[entrypoint] WARN: omp config set failed — TUI may show the setup wizard"

# ── 2c. Signal file + env file so the hook and the per-task relaunch agree ───
export OMP_HOME="${OMP_HOME:-${HOME}/.omp}"
export OMP_HOOK_FILE="$HOOK_FILE"
export OMP_TURN_SIGNAL_FILE="${OMP_TURN_SIGNAL_FILE:-${OMP_HOME}/turn-signal.ndjson}"
export OMP_DEFAULT_CWD OMP_LAUNCHER="$LAUNCHER"
mkdir -p "$(dirname "$OMP_TURN_SIGNAL_FILE")" "${OMP_HOME}/tasks"
: > "$OMP_TURN_SIGNAL_FILE"   # fresh signal on boot

# omp.env — sourced by launch-omp.sh so a `tmux respawn-window` (which does not
# inherit the poller's shell) still gets provider/model/profile. Belt-and-
# suspenders with the `tmux set-environment -g` below.
export OMP_ENV_FILE="${OMP_HOME}/omp.env"
cat > "$OMP_ENV_FILE" <<ENVFILE
OPENAI_BASE_URL=${_BASE_URL}
OPENAI_MODEL=${_MODEL}
OPENAI_API_KEY=${OPENAI_API_KEY:-sk-noauth}
OMP_MODEL_SELECTOR=${OMP_MODEL_SELECTOR}
OMP_PROFILE=${OMP_PROFILE}
OMP_HOME=${OMP_HOME}
PI_CODING_AGENT_DIR=${PI_CODING_AGENT_DIR:-${OMP_HOME}/agent}
OMP_HOOK_FILE=${HOOK_FILE}
OMP_TURN_SIGNAL_FILE=${OMP_TURN_SIGNAL_FILE}
OMP_DEFAULT_CWD=${OMP_DEFAULT_CWD}
HOME=${HOME}
PATH=${PATH}
ENVFILE
chmod 600 "$OMP_ENV_FILE"

# ── 3. tmux layout (~/.tmux.conf keeps a large scrollback for the pane SSE) ──
# mouse on: the Sessions-page web terminal forwards wheel events as mouse CSI
# sequences; tmux (mouse on) forwards them to the native omp TUI in Window 0 so
# the user can scroll the session history. Matches the fleet-wide scroll fix for
# mc-agent-base/mc-claude-agent (text selection uses Shift+drag).
cat > "${HOME}/.tmux.conf" <<'TMUXCONF'
set -g history-limit 20000
set -g status off
set -g mouse on
TMUXCONF

# start-native: (re)build the 3-window layout. Window 0 = the native TUI the
# Sessions page attaches to; Window 1 = the bridge poll driver; Window 2 = the
# recycler. Env is pushed to the tmux server so respawn-window inherits it.
start_native() {
    tmux new-session -d -s "$SESSION" -n win0
    for _kv in \
        "OPENAI_BASE_URL=${_BASE_URL}" "OPENAI_MODEL=${_MODEL}" \
        "OPENAI_API_KEY=${OPENAI_API_KEY:-sk-noauth}" \
        "OMP_MODEL_SELECTOR=${OMP_MODEL_SELECTOR}" "OMP_PROFILE=${OMP_PROFILE}" \
        "OMP_HOME=${OMP_HOME}" "PI_CODING_AGENT_DIR=${PI_CODING_AGENT_DIR:-${OMP_HOME}/agent}" \
        "OMP_HOOK_FILE=${HOOK_FILE}" "OMP_TURN_SIGNAL_FILE=${OMP_TURN_SIGNAL_FILE}" \
        "OMP_DEFAULT_CWD=${OMP_DEFAULT_CWD}" "OMP_ENV_FILE=${OMP_ENV_FILE}" \
        "OMP_LAUNCHER=${LAUNCHER}" "AGENT_NAME=${SESSION}" "HOME=${HOME}"; do
        tmux set-environment -g "${_kv%%=*}" "${_kv#*=}"
    done
    # Window 0: the visible native TUI (loads the hook, boots to chat).
    tmux send-keys -t "$SESSION":0 "exec ${LAUNCHER} ${OMP_DEFAULT_CWD}" C-m
    # Window 1: the persistent poll driver. It injects tasks into Window 0 and
    # reads the hook signal; it prints OMP_BRIDGE_READY into ITS pane (a Window-1
    # liveness log — the health-gate now anchors on Window 0's TUI glyph).
    tmux new-window -t "$SESSION" -n win1 "exec python3 $BRIDGE --serve"
    # Window 2: recycler tracking BOTH the TUI and the bridge.
    tmux new-window -t "$SESSION" -n win2 "exec /usr/local/bin/omp-recycler.sh"
    tmux select-window -t "$SESSION":0
}

start_native

# ── 4. PID-1 watchdog: keep the session (and its 3 windows) alive ───────────
while true; do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "[entrypoint] tmux session gone — recreating"
        start_native
    fi
    sleep 30
done
