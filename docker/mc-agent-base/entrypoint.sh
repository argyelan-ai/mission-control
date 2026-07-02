#!/bin/sh
# entrypoint.sh — Container PID 1
# Window 0: openclaude interaktiv (User sieht CLI-Header + Prompt live)
# Window 1: poll.sh pollt Tasks und sendet sie via tmux send-keys an Window 0

SESSION="${AGENT_NAME:-agent}"

# Bun in PATH (claude-mem Worker braucht bun:sqlite, installiert unter ~/.bun/bin)
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
    # JSON parsen und direkt exportieren (kein Funktions-Scope)
    _EXPORTS=$(echo "$BOOTSTRAP_RESPONSE" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k, v in d.items():
        if k in ("MC_AGENT_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "GH_TOKEN", "AGENT_RECYCLER_ENABLED", "CONTEXT_MAX"):
            print(f"{k}={v}")
except Exception:
    sys.exit(1)
' 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$_EXPORTS" ]; then
        # Bootstrap-Werte nur uebernehmen wenn sie nicht leer sind.
        # Agents ohne secret_id bekommen kein MC_AGENT_TOKEN vom Bootstrap —
        # dann darf der docker-compose MC_TOKEN NICHT ueberschrieben werden.
        _NEW_TOKEN=$(echo "$_EXPORTS" | grep '^MC_AGENT_TOKEN=' | cut -d= -f2-)
        _NEW_API_KEY=$(echo "$_EXPORTS" | grep '^OPENAI_API_KEY=' | cut -d= -f2-)
        _NEW_BASE_URL=$(echo "$_EXPORTS" | grep '^OPENAI_BASE_URL=' | cut -d= -f2-)
        _NEW_MODEL=$(echo "$_EXPORTS" | grep '^OPENAI_MODEL=' | cut -d= -f2-)
        _NEW_GH_TOKEN=$(echo "$_EXPORTS" | grep '^GH_TOKEN=' | cut -d= -f2-)
        _NEW_RECYCLER=$(echo "$_EXPORTS" | grep '^AGENT_RECYCLER_ENABLED=' | cut -d= -f2-)
        if [ -n "$_NEW_TOKEN" ]; then
            export MC_AGENT_TOKEN="$_NEW_TOKEN"
            export MC_TOKEN="$_NEW_TOKEN"
        else
            # Kein Token vom Bootstrap — docker-compose MC_TOKEN als Fallback
            export MC_AGENT_TOKEN="${MC_TOKEN:-}"
        fi
        if [ -n "$_NEW_API_KEY" ]; then
            export OPENAI_API_KEY="$_NEW_API_KEY"
        fi
        # Per-Agent runtime override — aus Bootstrap-Response. Wenn leer:
        # docker-compose env greift (OPENAI_BASE_URL/MODEL defaults).
        if [ -n "$_NEW_BASE_URL" ]; then
            export OPENAI_BASE_URL="$_NEW_BASE_URL"
        fi
        if [ -n "$_NEW_MODEL" ]; then
            export OPENAI_MODEL="$_NEW_MODEL"
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
            # Zwei Wege, den Token im Container wirksam zu machen:
            #   gh CLI  → nutzt GH_TOKEN env automatisch (keine Extra-Config)
            #   git     → braucht Credential-Helper mit Auth-Info
            # `gh auth login --with-token` weigert sich wenn GH_TOKEN env
            # gesetzt ist ("clear the value from the environment first"),
            # also konfigurieren wir git direkt via credential-store. Format:
            # https://<user>:<token>@github.com  — git liest das beim push.
            # Den User-Namen parst git selber nicht, nur das Token-Feld zaehlt,
            # aber ein korrekter User in der URL stoert nicht.
            GIT_CRED_FILE="${HOME}/.git-credentials"
            echo "https://oauth:${GH_TOKEN}@github.com" > "$GIT_CRED_FILE"
            chmod 600 "$GIT_CRED_FILE"
            git config --global credential.helper "store --file=${GIT_CRED_FILE}"
            git config --global user.email "${AGENT_NAME}@mc.local"
            git config --global user.name "${AGENT_NAME}"
            echo "[entrypoint] GH_TOKEN aktiv (git credential.helper + gh CLI env)"

            # MC pre-push hook global aktivieren. Der Hook prueft bei jedem
            # push ob die Origin-URL zur erwarteten Remote-URL passt (steht
            # als .mc-expected-remote im Workspace). Ohne expectation-file
            # ist er still. Verhindert "push auf fremdes Repo" (2026-04-19).
            if [ -d /home/agent/.git-hooks ]; then
                git config --global core.hooksPath /home/agent/.git-hooks
                echo "[entrypoint] mc pre-push guard aktiv (core.hooksPath=/home/agent/.git-hooks)"
            fi
        fi
        echo "[entrypoint] Bootstrap OK — Tokens aus Vault geladen"
    else
        echo "[entrypoint] Bootstrap JSON-Parse fehlgeschlagen — Fallback auf Env-Vars"
        export MC_AGENT_TOKEN="${MC_TOKEN:-}"
    fi
else
    echo "[entrypoint] Bootstrap fehlgeschlagen — Fallback auf Env-Vars"
    export MC_AGENT_TOKEN="${MC_TOKEN:-}"
fi

# Trust-Dialog pre-akzeptieren für /home/agent + /workspace.
# Ohne das zeigt claude-code beim ersten Start einen interaktiven Trust-Prompt
# der die Session blockiert. Gleicher Fix wie in mc-claude-agent/entrypoint.sh.
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
# mouse off — xterm.js im Browser macht native Text-Selection (macOS clipboard).
# Scrolling: Frontend-JS fuegt einen Custom-Wheel-Handler hinzu der tmux copy-mode
# keystrokes an den PTY sendet (siehe frontend-v2/src/app/sessions/page.tsx).
# Historie: 7426520 hatte mouse on (scroll ja, selection nein); 4f95fe2 mouse off
# (selection ja, scroll nein); der vorige Fix (mouse on + Shift-Drag) war Annahme-
# Fehler — xterm.js forwardet drag-events an tmux wenn mouse tracking aktiv ist.
# Dieser Fix: mouse off + JS-wheel-handler = beides funktioniert.
set -g mouse off
set -g aggressive-resize on
set -g history-limit 50000
set -g default-terminal "xterm-256color"
TMUX_CONF

# Hilfsfunktion: tmux-Session mit beiden Windows starten
# Backoff-Strategie (ADR-023 ultrareview): exponentiell 2,4,8,16,32,60s statt
# flat 5s — verhindert Restart-Cascade bei persistenten Auth/Connection-Errors.
start_tmux() {
    # Window 0: openclaude interaktiv
    tmux new-session -d -s "$SESSION" \
        'd=2; while true; do /home/agent/start-claude.sh; echo "[entrypoint] openclaude exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'

    # Window 1: poll.sh
    tmux new-window -t "$SESSION:1" \
        'd=2; while true; do bash /home/agent/poll.sh; echo "[entrypoint] poll.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'

    # Window 2: recycler.sh (Phase 3, ADR-024). Always created — recycler self-gates
    # via AGENT_RECYCLER_ENABLED env-var (exec sleep infinity if disabled). Keeps
    # the watchdog case-block simple (Pitfall 5).
    tmux new-window -t "$SESSION:2" \
        'd=2; while true; do bash /home/agent/recycler.sh; echo "[entrypoint] recycler.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'

    # User sieht Window 0 (openclaude) wenn er sich verbindet
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
        'd=2; while true; do /home/agent/start-claude.sh; echo "[entrypoint] openclaude exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'
    echo "[watchdog] claude window (0) neugestartet"
}

restart_recycler_window() {
    tmux new-window -t "$SESSION:2" \
        'd=2; while true; do bash /home/agent/recycler.sh; echo "[entrypoint] recycler.sh exited, restart in ${d}s..."; sleep "$d"; d=$(( d*2 > 60 ? 60 : d*2 )); done'
    echo "[watchdog] recycler window (2) neugestartet"
}

# PID 1: Watchdog — prueft alle 30s ob tmux-Session + beide Windows leben.
# Session allein reicht nicht: wenn nur Window 1 (poll) stirbt, bleibt die
# Session bestehen, Tasks haengen aber ohne poll.sh (kein Dispatch, kein Heartbeat).
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
