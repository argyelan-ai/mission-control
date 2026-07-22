#!/bin/bash
# entrypoint.sh — Container PID 1 für mc-claude-agent
# Window 0: claude interaktiv (User sieht CLI-Header + Prompt live)
# Window 1: poll.sh pollt Tasks und sendet sie via tmux send-keys an Window 0
#
# Unterschiede zu mc-agent-base/entrypoint.sh (openclaude-Variant):
#   - Bootstrap liest CLAUDE_CODE_OAUTH_TOKEN (statt OPENAI_API_KEY shim)
#   - Kein OPENAI_BASE_URL / OPENAI_MODEL — claude-code nutzt native Anthropic API
#   - tmux Window 0 startet `claude` via start-claude.sh (nicht openclaude)
#
# WICHTIG: Debian-base hat dash als /bin/sh, brauchen bash für `source` etc.

SESSION="${AGENT_NAME:-agent}"

# Bun in PATH (claude-mem Worker braucht bun:sqlite, installiert unter ~/.bun/bin
# sobald der claude-mem Plugin installiert ist — läuft als Subprozess)
export PATH="/home/agent/.bun/bin:${PATH}"

# ── Bootstrap: Tokens vom Backend holen (Vault-dekryptiert, kein Klartext auf Disk) ──
# Retry-Loop: Backend braucht beim Kaltstart ein paar Sekunden.
BOOTSTRAP_URL="${MC_API_URL:-http://backend:8000}/api/v1/internal/bootstrap?agent_name=${AGENT_NAME}"
BOOTSTRAP_RESPONSE=""
for _attempt in 1 2 3 4 5 6; do
    BOOTSTRAP_RESPONSE=$(curl -sf --max-time 5 "$BOOTSTRAP_URL" 2>/dev/null) && break
    echo "[entrypoint] Bootstrap Versuch $_attempt fehlgeschlagen, retry in 3s..."
    sleep 3
done

if [ -n "$BOOTSTRAP_RESPONSE" ]; then
    # JSON parsen und direkt exportieren (kein Funktions-Scope).
    # Relevante Keys für claude-code agents:
    #   MC_AGENT_TOKEN         — Auth gegen MC Backend (poll, patch, comment)
    #   CLAUDE_CODE_OAUTH_TOKEN — Anthropic Pro/Max OAuth (1 Jahr gültig)
    #   GH_TOKEN               — GitHub (gh CLI + git credential.helper)
    _EXPORTS=$(echo "$BOOTSTRAP_RESPONSE" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k, v in d.items():
        if k in ("MC_AGENT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "AGENT_RECYCLER_ENABLED", "CONTEXT_MAX"):
            print(f"{k}={v}")
except Exception:
    sys.exit(1)
' 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$_EXPORTS" ]; then
        _NEW_TOKEN=$(echo "$_EXPORTS" | grep '^MC_AGENT_TOKEN=' | cut -d= -f2-)
        _NEW_OAUTH=$(echo "$_EXPORTS" | grep '^CLAUDE_CODE_OAUTH_TOKEN=' | cut -d= -f2-)
        _NEW_GH_TOKEN=$(echo "$_EXPORTS" | grep '^GH_TOKEN=' | cut -d= -f2-)
        _NEW_RECYCLER=$(echo "$_EXPORTS" | grep '^AGENT_RECYCLER_ENABLED=' | cut -d= -f2-)
        if [ -n "$_NEW_TOKEN" ]; then
            export MC_AGENT_TOKEN="$_NEW_TOKEN"
            export MC_TOKEN="$_NEW_TOKEN"
        else
            export MC_AGENT_TOKEN="${MC_TOKEN:-}"
        fi
        if [ -n "$_NEW_OAUTH" ]; then
            export CLAUDE_CODE_OAUTH_TOKEN="$_NEW_OAUTH"
        fi
        # Phase 3 (ADR-024) — recycler kill-switch from bootstrap response
        # (effective value computed by docker_agent_sync.py + internal/bootstrap;
        # default-on if backend omits the field). Caveat 3: must export here
        # so tmux Window 2 (recycler.sh) inherits the value.
        if [ -n "$_NEW_RECYCLER" ]; then
            export AGENT_RECYCLER_ENABLED="$_NEW_RECYCLER"
        else
            export AGENT_RECYCLER_ENABLED="${AGENT_RECYCLER_ENABLED:-true}"
        fi
        # CTX-01 (Phase 6): expose context_max so poll.sh has a fallback
        # denominator if the tmux statusline scrape returns no ctx%. Backend
        # bootstrap response includes CONTEXT_MAX = str(agent.context_max).
        _NEW_CONTEXT_MAX=$(echo "$_EXPORTS" | grep '^CONTEXT_MAX=' | cut -d= -f2-)
        if [ -n "$_NEW_CONTEXT_MAX" ]; then
            export CONTEXT_MAX="$_NEW_CONTEXT_MAX"
        else
            export CONTEXT_MAX="${CONTEXT_MAX:-200000}"
        fi
        if [ -n "$_NEW_GH_TOKEN" ]; then
            export GH_TOKEN="$_NEW_GH_TOKEN"
            # git credential.helper (siehe mc-agent-base/entrypoint.sh für
            # Details — warum `gh auth login --with-token` nicht mit GH_TOKEN
            # env zusammen funktioniert).
            GIT_CRED_FILE="${HOME}/.git-credentials"
            echo "https://oauth:${GH_TOKEN}@github.com" > "$GIT_CRED_FILE"
            chmod 600 "$GIT_CRED_FILE"
            git config --global credential.helper "store --file=${GIT_CRED_FILE}"
            git config --global user.email "${AGENT_NAME}@mc.local"
            git config --global user.name "${AGENT_NAME}"
            echo "[entrypoint] GH_TOKEN aktiv (git credential.helper + gh CLI env)"

            # MC pre-push hook — verhindert "push auf fremdes Repo"
            # (siehe PR #41 / Incident 2026-04-19)
            if [ -d /home/agent/.git-hooks ]; then
                git config --global core.hooksPath /home/agent/.git-hooks
                echo "[entrypoint] mc pre-push guard aktiv (core.hooksPath=/home/agent/.git-hooks)"
            fi
        fi
        if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
            echo "[entrypoint] Bootstrap OK — MC_AGENT_TOKEN + CLAUDE_CODE_OAUTH_TOKEN geladen"
        else
            echo "[entrypoint] Bootstrap OK — MC_AGENT_TOKEN geladen, OAUTH fehlt (docker-compose fallback?)"
        fi
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

# Guard: Ohne CLAUDE_CODE_OAUTH_TOKEN startet claude im interactive OAuth-Flow,
# was im Container hängt (kein Browser). Lieber fail-loud als hängen lassen.
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "[entrypoint] FEHLER: CLAUDE_CODE_OAUTH_TOKEN fehlt. Setze den Token"
    echo "[entrypoint] entweder im .env.agents, im Secrets-Vault, oder via"
    echo "[entrypoint] docker-compose env. Siehe docs: 'claude setup-token'."
    echo "[entrypoint] Container bleibt idle (sleep infinity) bis Token da ist."
    exec sleep infinity
fi

# Trust-Dialog pre-akzeptieren für /home/agent + /workspace.
# Ohne das zeigt claude-code beim ersten Start einen interaktiven Trust-Prompt
# ("Is this a project you trust?") der die Session blockiert — ein Agent kann
# das nicht mit Enter beantworten. Muss VOR dem ersten `claude`-Aufruf passieren.
CLAUDE_JSON="/home/agent/.claude.json"
python3 - <<'PY' || echo "[entrypoint] warn: konnte Trust-Dialog nicht pre-akzeptieren"
import json, os
p = "/home/agent/.claude.json"
d = {}
if os.path.exists(p):
    try:
        with open(p) as f:
            d = json.load(f)
    except Exception:
        d = {}
projects = d.setdefault("projects", {})
for path in ("/home/agent", "/workspace"):
    e = projects.setdefault(path, {})
    e["hasTrustDialogAccepted"] = True
    e.setdefault("history", [])
    e.setdefault("allowedTools", [])
    e.setdefault("mcpServers", {})
    e.setdefault("enabledMcpjsonServers", [])
    e.setdefault("disabledMcpjsonServers", [])
    e.setdefault("hasClaudeMdExternalIncludesApproved", False)
    e.setdefault("hasClaudeMdExternalIncludesWarningShown", False)
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("[entrypoint] trust-dialog pre-accepted for /home/agent + /workspace")
PY

# tmux config — wird automatisch beim server start geladen
cat > /home/agent/.tmux.conf <<'TMUX_CONF'
# mouse on — tmux handles wheel events natively via mouse button 4/5 CSI sequences
# that xterm.js generates and the backend forwards through the PTY.
# Text selection: Shift+drag (standard tradeoff with tmux mouse on).
# History: 7426520 mouse on (scroll ok, selection broken without shift);
#          4f95fe2 mouse off + JS copy-mode keystrokes (selection ok, scroll broken —
#          Ctrl-B prefix byte via PTY never triggers copy-mode in tmux client);
#          this commit reverts to mouse on as the only working scroll approach.
set -g mouse on
set -g aggressive-resize on
set -g history-limit 50000
set -g default-terminal "xterm-256color"
TMUX_CONF

# W2.1 Turn-Signal (Phase A): frische Signal-Datei beim Boot. Ein `stop` aus
# einem frueheren Container-Leben hat keine Staleness-Grenze und wuerde sonst
# nach docker restart/respawn als frisches idle gelesen. poll.sh leert die
# Datei zusaetzlich beim Startup + jedem Session-Reset (Belt-and-Suspenders).
# Muster analog omp-bridge/entrypoint.sh (`: > "$OMP_TURN_SIGNAL_FILE"`).
: > /home/agent/.turn-signal

# Hilfsfunktion: tmux-Session mit beiden Windows starten
# Backoff-Strategie (ADR-023 ultrareview): exponentiell statt flat 5s — sonst
# baut sich bei persistenten Auth-Failures (OAuth ausgelaufen, Token verdreht)
# ein 12/min Restart-Cascade auf und frisst CPU. Cap bei 60s.
start_tmux() {
    # Window 0: claude interaktiv
    tmux new-session -d -s "$SESSION" \
        'd=2; while true; do /home/agent/start-claude.sh; echo "[entrypoint] claude exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'

    # Window 1: poll.sh
    tmux new-window -t "$SESSION:1" \
        'd=2; while true; do bash /home/agent/poll.sh; echo "[entrypoint] poll.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'

    # Window 2: recycler.sh (Phase 3, ADR-024). Always created — recycler self-gates
    # via AGENT_RECYCLER_ENABLED env-var (exec sleep infinity if disabled). Keeps
    # the watchdog case-block simple (Pitfall 5).
    tmux new-window -t "$SESSION:2" \
        'd=2; while true; do bash /home/agent/recycler.sh; echo "[entrypoint] recycler.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'

    # User sieht Window 0 (claude) wenn er sich verbindet
    tmux select-window -t "$SESSION:0"
}

# Erster Start
start_tmux

# Einzelnes Fenster nachstarten (Window-weiser Watchdog)
restart_poll_window() {
    tmux new-window -t "$SESSION:1" \
        'd=2; while true; do bash /home/agent/poll.sh; echo "[entrypoint] poll.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'
    echo "[watchdog] poll window (1) neugestartet"
}

restart_claude_window() {
    tmux new-window -t "$SESSION:0" \
        'd=2; while true; do /home/agent/start-claude.sh; echo "[entrypoint] claude exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'
    echo "[watchdog] claude window (0) neugestartet"
}

restart_recycler_window() {
    tmux new-window -t "$SESSION:2" \
        'd=2; while true; do bash /home/agent/recycler.sh; echo "[entrypoint] recycler.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'
    echo "[watchdog] recycler window (2) neugestartet"
}

# PID 1: Watchdog — prüft alle 30s ob tmux-Session + beide Windows leben.
# Siehe mc-agent-base/entrypoint.sh für Details (identische Logik).
while true; do
    sleep 30
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "[watchdog] tmux session '$SESSION' weg — neustart"
        start_tmux
        continue
    fi
    WINDOWS=$(tmux list-windows -t "$SESSION" -F '#{window_index}' 2>/dev/null | tr '\n' ' ')
    case " $WINDOWS " in
        *" 0 "*) ;;
        *) restart_claude_window ;;
    esac
    case " $WINDOWS " in
        *" 1 "*) ;;
        *) restart_poll_window ;;
    esac
    case " $WINDOWS " in
        *" 2 "*) ;;
        *) restart_recycler_window ;;
    esac
done
