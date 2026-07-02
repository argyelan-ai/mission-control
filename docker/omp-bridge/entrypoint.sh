#!/usr/bin/env bash
# docker/omp-bridge/entrypoint.sh — Container PID 1 (ADR-045).
#
# Boots the same 3-window tmux + bootstrap-token pattern as mc-agent-base, but
# Window 0 runs the PERSISTENT bridge.py driver instead of an interactive
# openclaude pane. omp is a short-lived SUBPROCESS of bridge.py, never its own
# pane — so the health-check, Sessions live-terminal and recycler all track one
# stable process.
#
# Do NOT `docker build` this from the omp-runtime workflow — image build is GATED
# (the operator runs scripts/build-agent-images.sh mc-omp-agent). This file is authored
# real so the build, when run, works.
set -eu

SESSION="${AGENT_NAME:-omp-agent}"
BRIDGE=/opt/omp-bridge/bridge.py

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
        export MC_AGENT_TOKEN="${MC_TOKEN:-}"
    fi
else
    echo "[entrypoint] Bootstrap fehlgeschlagen — Fallback auf Env-Vars"
    export MC_AGENT_TOKEN="${MC_TOKEN:-}"
fi

# ── 2. Render omp's native models.yml provider from the OpenAI-style env ─────
# omp resolves models PROFILE-FIRST: with OMP_PROFILE=mc-agent it reads
# $HOME/.omp/profiles/mc-agent/agent/models.yml (a file at $PI_CODING_AGENT_DIR
# is IGNORED once OMP_PROFILE is set). omp's built-in `openai` provider does NOT
# resolve a vLLM-served model from OPENAI_BASE_URL, so a models.yml is mandatory.
# We render a dedicated `qwen-spark` provider (auth: none = keyless vLLM) so
# runtime.endpoint stays the single source of truth and no token routing is
# duplicated. bridge.py selects `qwen-spark/${OPENAI_MODEL}`.
OMP_PROFILE="${OMP_PROFILE:-mc-agent}"
MODELS_DIR="${HOME}/.omp/profiles/${OMP_PROFILE}/agent"
mkdir -p "$MODELS_DIR"
_BASE_URL="${OPENAI_BASE_URL:-http://192.0.2.20:8000/v1}"
_MODEL="${OPENAI_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}"
cat > "${MODELS_DIR}/models.yml" <<YAML
providers:
  qwen-spark:
    name: Qwen Spark vLLM
    baseUrl: ${_BASE_URL}
    api: openai-completions
    auth: none
    models:
      - id: ${_MODEL}
        name: Qwen Spark
        contextWindow: 262144
        maxTokens: 65536
YAML
export OMP_PROFILE
export OMP_MODEL_SELECTOR="qwen-spark/${_MODEL}"
echo "[entrypoint] models.yml rendered at ${MODELS_DIR}/models.yml (provider qwen-spark -> ${_BASE_URL}, model ${_MODEL})"

# ── 3. tmux layout (~/.tmux.conf keeps a large scrollback for the pane SSE) ──
cat > "${HOME}/.tmux.conf" <<'TMUXCONF'
set -g history-limit 20000
set -g status off
TMUXCONF

tmux new-session -d -s "$SESSION" -n win0
# Window 0: the persistent driver. bridge.py --serve prints OMP_BRIDGE_READY
# ITSELF once its first /me/poll round-trip completes — NOT a pre-exec echo
# (that would leave the sentinel in the pane even while --serve crash-loops, so
# the switch health-gate would false-positive). exec so the pane == PID of the
# bridge (recycler + health scrape both track it).
tmux send-keys -t "$SESSION":0 "exec python3 $BRIDGE --serve" C-m
# Window 1: reserved (the old poll + screen-scrape split collapses into Window 0).
tmux new-window -t "$SESSION" -n win1
# Window 2: forked recycler that tracks bridge.py (never the short-lived omp).
tmux new-window -t "$SESSION" -n win2 "exec /usr/local/bin/omp-recycler.sh"
tmux select-window -t "$SESSION":0

# ── 4. PID-1 watchdog: keep the session (and its 3 windows) alive ───────────
while true; do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "[entrypoint] tmux session gone — recreating"
        tmux new-session -d -s "$SESSION" -n win0
        tmux send-keys -t "$SESSION":0 "exec python3 $BRIDGE --serve" C-m
        tmux new-window -t "$SESSION" -n win1
        tmux new-window -t "$SESSION" -n win2 "exec /usr/local/bin/omp-recycler.sh"
    fi
    sleep 30
done
