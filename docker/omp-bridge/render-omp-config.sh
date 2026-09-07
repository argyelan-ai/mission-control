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
#
# Der Aufruf braucht den Bootstrap-Schluessel im Kopf: `/internal/bootstrap`
# verlangt `Authorization: Bearer <INTERNAL_BOOTSTRAP_SECRET>`, sonst 401
# (backend/app/routers/internal.py::_check_bootstrap_secret). Bis 06.09.2026
# fehlte der Kopf hier — jeder Reload endete mit „kein Modell bekannt", und MC
# startete den Container ersatzweise neu. Darum steht der HTTP-Code jetzt auch
# im Fehlertext: 401 heisst „Schluessel", nicht „Modell".
fetch_bootstrap() {
    _url="${MC_API_URL:-http://backend:8000}/api/v1/internal/bootstrap?agent_name=${AGENT_NAME:-}"
    _body="/tmp/.render-omp-bootstrap-$$.json"
    _code=$(curl -s -o "$_body" -w '%{http_code}' --max-time 5 \
        -H "Authorization: Bearer ${INTERNAL_BOOTSTRAP_SECRET:-}" \
        "$_url" 2>/dev/null) || _code=""
    [ -n "$_code" ] || _code="000"
    if [ "$_code" != "200" ]; then
        echo "[render-omp-config] bootstrap HTTP ${_code} — 401/403 heisst: INTERNAL_BOOTSTRAP_SECRET fehlt oder passt nicht zum Backend; 000 heisst: Backend nicht erreichbar" >&2
        rm -f "$_body"
        return 1
    fi
    _response=$(cat "$_body" 2>/dev/null)
    rm -f "$_body"
    [ -n "$_response" ] || return 1
    _exports=$(printf '%s' "$_response" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
              "OMP_CONTEXT_WINDOW", "OMP_MAX_TOKENS", "OMP_MODEL_INPUT"):
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

# ── Anmeldung behalten ───────────────────────────────────────────────────────
# `docker exec` sieht nur die Umgebung, mit der der Container GESTARTET wurde —
# den OPENAI_API_KEY, den der Entrypoint zur Laufzeit vom Bootstrap holt, also
# nicht. Steht er schon in der omp.env, uebernehmen wir ihn: ein Reload wechselt
# das Modell, er darf die laufende Anmeldung nicht wegwerfen.
if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$OMP_ENV_FILE" ]; then
    _existing_key=$(sed -n 's/^OPENAI_API_KEY=//p' "$OMP_ENV_FILE" | head -n 1)
    if [ -n "$_existing_key" ] && [ "$_existing_key" != "sk-noauth" ]; then
        OPENAI_API_KEY="$_existing_key"
        export OPENAI_API_KEY
    fi
fi

if [ -z "${OPENAI_BASE_URL:-}" ] || [ -z "${OPENAI_MODEL:-}" ]; then
    echo "[render-omp-config] kein Modell bekannt (OPENAI_BASE_URL/OPENAI_MODEL leer)" >&2
    exit 1
fi

_BASE_URL="${OPENAI_BASE_URL}"
_MODEL="${OPENAI_MODEL}"
OMP_MODEL_SELECTOR="mc-openai/${_MODEL}"

# ── Vision-Fähigkeit (W3, 06.09.2026) ────────────────────────────────────────
# MC liefert OMP_MODEL_INPUT als Komma-Liste ("text" oder "text,image") aus
# runtimes.supports_vision (build_runtime_env). Live am omp-Binary geprüft
# (/usr/local/bin/omp, grep): fehlt "image" in der input-Liste des aktiven
# Modells, sucht omp quer über ALLE registrierten Modelle nach einem
# bildfähigen — inklusive eines eingebauten, nie konfigurierten Standard-
# Providers, der dann mit "401 Incorrect API key provided: sk-noauth" gegen
# api.openai.com läuft statt gegen unsere Box. Default "text" (kein Flag
# gesetzt = alte Runtimes ohne das Feld) hält den sicheren Zustand.
_SUPPORTS_VISION=0
case ",${OMP_MODEL_INPUT:-text}," in
    *,image,*) _SUPPORTS_VISION=1 ;;
esac
if [ "$_SUPPORTS_VISION" = "1" ]; then
    _MODEL_INPUT_YAML="[text, image]"
else
    _MODEL_INPUT_YAML="[text]"
fi

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
        # Vision-Fähigkeit (W3, 06.09.2026): siehe Kommentar oben bei
        # OMP_MODEL_INPUT. [text] ist der sichere Standard.
        input: ${_MODEL_INPUT_YAML}
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

# ── omp-Konfiguration (config.yml über `omp config set`) ────────────────────
# Handgeschriebene config.yml wird von omp NICHT respektiert — bestätigt
# in-container (omp v16.2.13, siehe entrypoint.sh) —, `omp config set` ist der
# einzige Weg, der im Profil-Speicher ankommt.
#
# Nicht-Vision-Fall: statt nur `modelRoles.vision` unbelegt zu lassen (omps
# eigener Auflösungspfad sucht dann quer über ALLE registrierten Modelle nach
# einem bildfähigen und würde trotzdem den eingebauten Standard-Provider
# treffen — siehe Kommentar oben), wird `images.blockImages: true` gesetzt.
# Das ist im omp-Binary die ERSTE Prüfung vor jeder Modell-Auflösung für
# Bildfragen (`if (settings.get("images.blockImages")) throw …`) — omp lehnt
# das Bild dann mit einer eigenen, klaren Fehlermeldung ab, statt den
# `sk-noauth`-Schlüssel gegen api.openai.com zu verschicken.
if command -v omp >/dev/null 2>&1; then
    if [ "$_SUPPORTS_VISION" = "1" ]; then
        omp config set images.blockImages false >/dev/null 2>&1 \
            && omp config set modelRoles.vision "${OMP_MODEL_SELECTOR}" >/dev/null 2>&1 \
            && omp config set images.questionTimeoutMs 120000 >/dev/null 2>&1 \
            && echo "[render-omp-config] Vision aktiv: modelRoles.vision=${OMP_MODEL_SELECTOR}, images.blockImages=false" \
            || echo "[render-omp-config] WARN: omp config set (Vision) fehlgeschlagen"
    else
        omp config set images.blockImages true >/dev/null 2>&1 \
            && echo "[render-omp-config] kein Vision-Modell: images.blockImages=true (omp lehnt Bilder ab statt extern nachzufragen)" \
            || echo "[render-omp-config] WARN: omp config set (images.blockImages) fehlgeschlagen"
    fi
fi

echo "[render-omp-config] models.yml + omp.env geschrieben (${_BASE_URL}, Modell ${_MODEL})"
