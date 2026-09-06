#!/usr/bin/env bash
# docker/omp-bridge/render-omp-config.sh — omp's Modell-Konfiguration schreiben
# (ADR-078, „Reload statt Neustart").
#
# WARUM ES DIESE DATEI GIBT
# -------------------------
# Bis 05.09.2026 rendert NUR der Entrypoint `models.yml` und `omp.env`. Ändert
# sich das Modell hinter der Box-URL (Rezeptwechsel), musste MC den ganzen
# Container neu starten (`runtime_propagation._sync_one` → `docker restart`) —
# mit 60 s Health-Frist je Agent, Trust-Dialog und Rollback-Pfad. Bei einem
# einzigen Agenten war das teuer; seit alle Agenten einer Box an DERSELBEN
# Slot-Zeile hängen, wäre es eine Viertelstunde Stillstand pro Wechsel.
#
# Nötig ist der Neustart nicht: `launch-omp.sh` liest `omp.env` bei JEDEM
# Window-Respawn neu ein, und die Bridge respawnt Window 0 für jede Aufgabe.
# Es genügt also, die Dateien im laufenden Container neu zu schreiben:
#
#     docker exec <container> render-omp-config.sh
#
# Die nächste Aufgabe fährt dann auf dem neuen Modell — ohne Neustart, ohne
# Health-Frist, ohne verlorene Sitzung.
#
# Benutzung
# ---------
#   render-omp-config.sh                 # Werte frisch von MC holen, dann rendern
#   render-omp-config.sh --no-bootstrap  # Werte aus der Umgebung nehmen
#                                        # (so ruft der Entrypoint es auf — er
#                                        #  hat den Bootstrap schon gemacht)
#   render-omp-config.sh --wait <sek>    # bis zu <sek> auf ein Modell warten
#
# Rückgabe: 0 = gerendert. 1 = kein Modell bekommen (nach Ablauf der Wartezeit).
set -eu

# ── Argumente ────────────────────────────────────────────────────────────────
DO_BOOTSTRAP=1
# Standard-Wartezeit 0: ein `docker exec` soll schnell antworten oder ehrlich
# scheitern. Der Entrypoint gibt seine eigene, lange Wartezeit mit.
WAIT_SECONDS=0
WAIT_STEP=20

while [ $# -gt 0 ]; do
    case "$1" in
        --no-bootstrap) DO_BOOTSTRAP=0 ;;
        --wait) shift; WAIT_SECONDS="${1:-0}" ;;
        *) echo "[render-omp-config] unbekanntes Argument: $1" >&2; exit 2 ;;
    esac
    shift
done

HOME="${HOME:-/home/agent}"
OMP_PROFILE="${OMP_PROFILE:-mc-agent}"
OMP_HOME="${OMP_HOME:-${HOME}/.omp}"
OMP_ENV_FILE="${OMP_ENV_FILE:-${OMP_HOME}/omp.env}"
MODELS_DIR="${HOME}/.omp/profiles/${OMP_PROFILE}/agent"

# ── Werte von MC holen ───────────────────────────────────────────────────────
# Genau die Schlüssel, die das Modell beschreiben. Tokens (MC_AGENT_TOKEN,
# GH_TOKEN) fasst dieses Skript NICHT an — die gehören dem Entrypoint, und ein
# Reload soll die laufende Anmeldung nicht anrühren.
fetch_bootstrap() {
    _url="${MC_API_URL:-http://backend:8000}/api/v1/internal/bootstrap?agent_name=${AGENT_NAME:-}"
    _response=$(curl -sf --max-time 5 "$_url" 2>/dev/null) || return 1
    _exports=$(printf '%s' "$_response" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
              "OMP_CONTEXT_WINDOW", "OMP_MAX_TOKENS"):
        v = d.get(k)
        if v not in (None, ""):
            print(f"{k}={v}")
except Exception:
    sys.exit(1)
' 2>/dev/null) || return 1
    [ -n "$_exports" ] || return 1
    while IFS= read -r _line; do
        [ -n "$_line" ] || continue
        export "${_line%%=*}=${_line#*=}"
    done <<EOF
$_exports
EOF
    return 0
}

if [ "$DO_BOOTSTRAP" = "1" ]; then
    _waited=0
    while true; do
        if fetch_bootstrap && [ -n "${OPENAI_BASE_URL:-}" ] && [ -n "${OPENAI_MODEL:-}" ]; then
            break
        fi
        if [ "$_waited" -ge "$WAIT_SECONDS" ]; then
            break
        fi
        echo "[render-omp-config] warte auf ein Modell von MC (${_waited}s/${WAIT_SECONDS}s)…" >&2
        sleep "$WAIT_STEP"
        _waited=$((_waited + WAIT_STEP))
    done
fi

if [ -z "${OPENAI_BASE_URL:-}" ] || [ -z "${OPENAI_MODEL:-}" ]; then
    echo "[render-omp-config] kein Modell bekannt (OPENAI_BASE_URL/OPENAI_MODEL leer)" >&2
    exit 1
fi

_BASE_URL="${OPENAI_BASE_URL}"
_MODEL="${OPENAI_MODEL}"
OMP_MODEL_SELECTOR="mc-openai/${_MODEL}"

# ── models.yml ───────────────────────────────────────────────────────────────
# omp löst Modelle PROFIL-ZUERST auf: mit OMP_PROFILE=mc-agent liest es
# $HOME/.omp/profiles/mc-agent/agent/models.yml. Der eingebaute `openai`-
# Provider findet ein vLLM-serviertes Modell aus OPENAI_BASE_URL nicht von
# selbst — die models.yml ist also Pflicht, nicht Kür.
mkdir -p "$MODELS_DIR"
if [ -n "${OPENAI_API_KEY:-}" ]; then
    _AUTH_LINE="    apiKey: ${OPENAI_API_KEY}"
else
    _AUTH_LINE="    auth: none"
fi
cat > "${MODELS_DIR}/models.yml" <<YAML
providers:
  mc-openai:
    name: MC OpenAI-compatible endpoint
    baseUrl: ${_BASE_URL}
    api: openai-completions
${_AUTH_LINE}
    models:
      - id: ${_MODEL}
        name: MC model
        # Ohne diese Fahne rendert omp die getrennten Reasoning-Deltas von vLLM
        # als gewöhnlichen Text statt als einklappbaren Denk-Block. Für Modelle
        # ohne Reasoning schadet sie nicht (das Feld kommt dann nie an).
        reasoning: true
        contextWindow: ${OMP_CONTEXT_WINDOW:-262144}
        maxTokens: ${OMP_MAX_TOKENS:-32768}
YAML

# ── omp.env ──────────────────────────────────────────────────────────────────
# Wird von launch-omp.sh bei JEDEM Respawn neu eingelesen — das ist der Hebel,
# der den Container-Neustart überflüssig macht.
mkdir -p "$(dirname "$OMP_ENV_FILE")"
cat > "$OMP_ENV_FILE" <<ENVFILE
OPENAI_BASE_URL=${_BASE_URL}
OPENAI_MODEL=${_MODEL}
OPENAI_API_KEY=${OPENAI_API_KEY:-sk-noauth}
OMP_MODEL_SELECTOR=${OMP_MODEL_SELECTOR}
OMP_PROFILE=${OMP_PROFILE}
OMP_HOME=${OMP_HOME}
PI_CODING_AGENT_DIR=${PI_CODING_AGENT_DIR:-${OMP_HOME}/agent}
OMP_HOOK_FILE=${OMP_HOOK_FILE:-/opt/omp-bridge/turn-end-hook.mjs}
OMP_TURN_SIGNAL_FILE=${OMP_TURN_SIGNAL_FILE:-${OMP_HOME}/turn-signal.ndjson}
OMP_DEFAULT_CWD=${OMP_DEFAULT_CWD:-/workspace}
HOME=${HOME}
PATH=${PATH}
ENVFILE
chmod 600 "$OMP_ENV_FILE"

# ── Laufende tmux-Sitzung nachziehen ─────────────────────────────────────────
# Gürtel und Hosenträger: `launch-omp.sh` liest zwar omp.env, aber die
# tmux-Server-Umgebung ist der zweite Weg, auf dem ein Respawn seine Werte
# bekommt. Steht sie noch auf dem alten Modell, hinge das Ergebnis davon ab,
# welcher Weg zuerst greift. Fehlt tmux (Aufruf ausserhalb des Containers),
# ist das kein Fehler.
if command -v tmux >/dev/null 2>&1; then
    _session="${AGENT_NAME:-omp-agent}"
    if tmux has-session -t "$_session" 2>/dev/null; then
        for _kv in \
            "OPENAI_BASE_URL=${_BASE_URL}" "OPENAI_MODEL=${_MODEL}" \
            "OPENAI_API_KEY=${OPENAI_API_KEY:-sk-noauth}" \
            "OMP_MODEL_SELECTOR=${OMP_MODEL_SELECTOR}"; do
            tmux set-environment -g "${_kv%%=*}" "${_kv#*=}" 2>/dev/null || true
        done
    fi
fi

echo "[render-omp-config] models.yml + omp.env geschrieben (${_BASE_URL}, Modell ${_MODEL})"
