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

# ── Atomar schreiben (Fix 19.10.2026) ────────────────────────────────────────
# `cat > datei` kürzt die Datei zuerst und füllt sie danach. Ein Leser in
# dieser Lücke (acp_model.read_env_file bei JEDER ACP-Sitzung, launch-omp.sh
# beim Sourcen) sah eine halbe Datei — fehlte darin OMP_MODEL_SELECTOR, griff
# still der alte Wert aus der Prozess-Umgebung (Vorfall 19.10.2026). Und
# `chmod 600` kam erst NACH dem Schreiben: eine neue Datei mit dem API-Schlüssel
# lag kurz mit 644 da.
#
# Darum: Temp-Datei im SELBEN Ordner (mv bleibt ein rename auf demselben
# Dateisystem), Rechte setzen, dann EIN `mv`. Leser sehen entweder die alte
# oder die neue Datei, nie eine halbe.
#
# ACHTUNG Mounts: wird eine dieser Dateien je EINZELN in den Container
# gemountet (bind mount einer Datei), bräche `mv` den Mount — der Container
# sähe die neue Datei, der Host behielte die alte. Heute liegen omp.env und
# models.yml im Container-Dateisystem (docker inspect, 19.10.2026). Der
# Wächter unten erkennt einen Einzel-Mount (mountinfo) und schreibt dann in
# place, mit Warnung.
_MOUNTINFO="${OMP_RENDER_MOUNTINFO:-/proc/self/mountinfo}"
_is_mount_target() {
    [ -r "$_MOUNTINFO" ] || return 1
    awk -v p="$1" '$5 == p { found = 1 } END { exit found ? 0 : 1 }' "$_MOUNTINFO"
}

# _atomic_write <ziel> <modus>   — Inhalt kommt auf stdin.
_atomic_write() {
    _aw_target="$1"
    _aw_mode="$2"
    if _is_mount_target "$_aw_target"; then
        echo "[render-omp-config] WARN: ${_aw_target} ist einzeln gemountet — schreibe in place (nicht atomar), mv würde den Mount brechen" >&2
        ( umask 077; cat > "$_aw_target" )
        chmod "$_aw_mode" "$_aw_target"
        return 0
    fi
    _aw_tmp=$(umask 077; mktemp "${_aw_target}.XXXXXX")
    if ! cat > "$_aw_tmp"; then
        rm -f "$_aw_tmp"
        return 1
    fi
    chmod "$_aw_mode" "$_aw_tmp"
    mv -f "$_aw_tmp" "$_aw_target"
}

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
    # umask 077: die Antwort enthält den API-Schlüssel.
    _code=$(umask 077; curl -s -o "$_body" -w '%{http_code}' --max-time 5 \
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
              "OMP_CONTEXT_WINDOW", "OMP_MAX_TOKENS", "OMP_THINKING_BUDGET",
              "OMP_MODEL_INPUT"):
        v = d.get(k)
        if v not in (None, ""):
            print(f"{k}={v}")
except Exception:
    sys.exit(1)
' 2>/dev/null) || return 1
    [ -n "$_exports" ] || return 1
    # MC sends the thinking cap empty for a runtime without one (cloud,
    # non-reasoning) and the loop above skips empty values. Bootstrap is the
    # truth for a reload: drop any cap still in this shell, so a switch onto
    # such a runtime does not keep the previous runtime's cap.
    unset OMP_THINKING_BUDGET
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
# name = the model id: omp reports it in ACP configOptions and the composer
# chip shows it (a fixed placeholder until 19.10.2026). Same plain form as the
# id line, so any id that parses there parses as the name. (No comment inside
# the heredoc below: it is unquoted, backticks there would run as commands.)
# contextWindow + maxTokens: MC decides both from the runtime row
# (backend/app/routers/internal.py, omp_max_output_tokens). maxTokens follows
# the window: local models an eighth of it, at most 131072; cloud endpoints at
# most 32768. Do not cap or recompute it here, or the two places drift. The
# 262144/32768 fallback applies only when MC knows no window for the runtime:
# unknown model, so the cautious old pair, never a guessed larger budget.
# Thinking cap (backend/app/routers/internal.py, omp_thinking_budget): omp
# 18.10.1 sends a model's compat.extraBody verbatim on every request, at every
# thinking level (checked in a throwaway container). Engines that know
# `thinking_budget` close the think block and still answer; self-hosted
# engines that do not know it ignore it. MC sends it only for local reasoning
# runtimes. Anything but a positive integer means: no block, no field.
_COMPAT_YAML=""
case "${OMP_THINKING_BUDGET:-}" in
    ""|*[!0-9]*|??????????*) ;;  # empty, not a number, or absurdly long
    *)
        if [ "$((10#${OMP_THINKING_BUDGET}))" -gt 0 ]; then
            _COMPAT_YAML=$(printf '\n        compat:\n          extraBody:\n            thinking_budget: %d' "$((10#${OMP_THINKING_BUDGET}))")
        fi
        ;;
esac
# 600: die Datei trägt den API-Schlüssel (apiKey).
_atomic_write "${MODELS_DIR}/models.yml" 600 <<YAML
providers:
  mc-openai:
    name: MC OpenAI-compatible endpoint
    baseUrl: ${_BASE_URL}
    api: openai-completions
${_AUTH_LINE}
    models:
      - id: ${_MODEL}
        name: ${_MODEL}
        # Ohne diese Fahne rendert omp die getrennten Reasoning-Deltas von vLLM
        # als gewöhnlichen Text statt als einklappbaren Denk-Block. Für Modelle
        # ohne Reasoning schadet sie nicht (das Feld kommt dann nie an).
        reasoning: true
        contextWindow: ${OMP_CONTEXT_WINDOW:-262144}
        maxTokens: ${OMP_MAX_TOKENS:-32768}
        # Vision-Fähigkeit (W3, 06.09.2026): siehe Kommentar oben bei
        # OMP_MODEL_INPUT. [text] ist der sichere Standard.
        input: ${_MODEL_INPUT_YAML}${_COMPAT_YAML}
YAML

# ── omp.env ──────────────────────────────────────────────────────────────────
# Wird von launch-omp.sh bei JEDEM Respawn neu eingelesen — das ist der Hebel,
# der den Container-Neustart überflüssig macht.
mkdir -p "$(dirname "$OMP_ENV_FILE")"
_atomic_write "$OMP_ENV_FILE" 600 <<ENVFILE
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
# ── Model roles + built-in providers (fix 20.10.2026, ADR-086) ─────────────
# omp's subagents pick their model by ROLE: `task` → @task, `scout` → @smol,
# `reviewer` → @slow (`omp agents unpack`, omp 18.10.1); the effort judge uses
# `judge`, a fresh ACP session `default`. omp resolves an empty role across
# every AVAILABLE model, and OPENAI_API_KEY in omp's environment (`sk-noauth`,
# or the real key of a keyed cloud runtime) makes omp's built-in `openai`
# provider available. Live 20.10.2026: subagents went to api.openai.com ("401
# invalid_api_key (sk-noauth)"); on a keyed runtime MC's key would go to a
# third party. Three writes, each on its own with its own WARN, the lock first
# (one failing write, e.g. a setting renamed by an omp bump, must not skip the
# others):
#   * disabledProviders: omp's built-in model providers (list below). omp has
#     no allowlist (`enabledProviders` only governs skill/rule sources); it
#     checks disabledProviders for availability, roles and fallback chains.
#     Kept enabled: `web` (web_search backends, no model route), `local`
#     (in-container tiny models), `cursor` (the same id is omp's .cursor
#     rules source, the setting is shared; the model provider needs a cursor
#     login MC never has). Source: the provider catalog of the omp 18.10.1
#     binary plus `llama.cpp`, a keyless discovery provider outside it (omp
#     probes 127.0.0.1:8080, LLAMA_CPP_BASE_URL, and lists what answers).
#     ONE keyless discovery provider stays enabled on purpose: `lm-studio`
#     (loopback :1234/v1/models, no key). omp refreshes its model registry —
#     re-reads models.yml — for a model it does not know only while some
#     discovery provider is enabled (`getDiscoverableProviders().length > 0`
#     in omp 18.10.1). A running omp (the chat daemon lives for days) needs
#     that refresh to find the new model after a reload/runtime switch; with
#     all three locked its subagents fail (container test c3). lm-studio, not
#     llama.cpp: 8080 is the common dev-server port, and llama.cpp re-probes
#     on every refresh; lm-studio caches its probe for 24 h.
#   * modelRoles: every chat role + judge → the bound model; vision only on a
#     vision runtime (else images.blockImages, below). A record setting is
#     written whole: omp 18.10.1 answers `modelRoles.vision` with "Unknown
#     setting" (the old vision branch never set the role). Writing it whole
#     also drops a stale vision role after a switch.
#   * task.agentModelOverrides: empty (it outranks the roles per agent).
# LIMIT: all of this is omp's PROFILE layer. omp merges project settings from
# the working directory over it, and a project `disabledProviders` list
# REPLACES this one: `<cwd>/.omp/config.yml` or `<cwd>/.claude/settings.json`
# with `disabledProviders: []` re-enables `openai`, and OPENAI_API_KEY in omp's
# environment goes to api.openai.com again (live, omp 18.10.1). omp has no
# switch that turns project settings off. The structural fix is to keep
# OPENAI_API_KEY out of every omp child environment (models.yml already
# carries the key for mc-openai) — outside this script, still open.
# omp bump: both lists come from the omp 18.10.1 binary. _OMP_LOCK_CHECKED
# names that version: a CI test ties it to the pin in docker/cli-versions.json,
# and a different installed omp (the in-product CLI update skips CI) gets a
# WARN below. Re-check with tests/test_render_omp_model_roles.py and
# MC_OMP_IMAGE=<image>: part c shows every new, still open provider or role.
_OMP_LOCK_CHECKED=18.10.1
_MODEL_ROLES="default smol slow plan commit tiny memory task advisor judge"
if [ "$_SUPPORTS_VISION" = "1" ]; then
    _MODEL_ROLES="${_MODEL_ROLES} vision"
fi
_OMP_BUILTIN_PROVIDERS="abliteration aiand aimlapi alibaba-coding-plan
alibaba-token-plan amazon-bedrock anthropic apple azure azure-openai-responses
baseten bedrock-mantle cerebras charm-hyper cline-pass cloudflare-ai-gateway
cloudflare-workers-ai commandcode coralbricks coreweave deepinfra deepseek devin
factory-droid firepass fireworks github-copilot gitlab-duo gitlab-duo-agent
gmi-cloud google google-antigravity google-gemini-cli google-vertex groq helmcode
huggingface kilo kimi-code litellm llama.cpp meta minimax minimax-code
minimax-code-cn mistral moonshot muse-code nanogpt novita nvidia ollama
ollama-cloud openai openai-codex opencode-go opencode-zen openrouter qianfan
sakana siliconflow siliconflow-cn singularityapi-dev
singularityapi-tech snowflake stepfun synthetic together typesafe umans venice
vercel-ai-gateway vllm wafer-serverless xai xai-oauth xiaomi
xiaomi-token-plan-ams xiaomi-token-plan-cn xiaomi-token-plan-sgp yolo-auto zai
zenmux zhipu-coding-plan"
# Eine Provider-ID, die wie ein Modellname aussieht: sie SPERRT einen Provider,
# sie wählt kein Modell — darum die Freigabe-Marke für
# backend/tests/test_no_hardcoded_models.py auf genau dieser Zeile.
_OMP_BUILTIN_PROVIDERS="${_OMP_BUILTIN_PROVIDERS} qwen-portal"  # model-catalog: allow

if command -v omp >/dev/null 2>&1; then
    # WARN lines go to stdout like every other line here: the entrypoint's
    # first call drops stderr (`2>/dev/null`), stdout lands in the container log.
    _omp_version="$(omp --version 2>/dev/null | head -n 1)"
    _omp_version="${_omp_version##*/}"
    if [ "$_omp_version" != "$_OMP_LOCK_CHECKED" ]; then
        echo "[render-omp-config] WARN: omp ${_omp_version:-unbekannt} — Provider-Sperre und Modell-Rollen sind für omp ${_OMP_LOCK_CHECKED} geprüft; neue Provider/Rollen bleiben offen (tests/test_render_omp_model_roles.py mit MC_OMP_IMAGE)"
    fi

    # JSON via python3, not by hand: model ids may contain `/`, `:` etc. If
    # that fails the value stays empty and the write's own WARN fires.
    _DISABLED_JSON=$(python3 -c 'import json, sys
print(json.dumps(sys.argv[1].split()))' "$_OMP_BUILTIN_PROVIDERS" 2>/dev/null) || _DISABLED_JSON=""
    _ROLES_JSON=$(python3 -c 'import json, sys
print(json.dumps({r: sys.argv[1] for r in sys.argv[2].split()}))' \
        "$OMP_MODEL_SELECTOR" "$_MODEL_ROLES" 2>/dev/null) || _ROLES_JSON=""

    if [ -n "$_DISABLED_JSON" ] \
        && omp config set disabledProviders "$_DISABLED_JSON" >/dev/null 2>&1; then
        echo "[render-omp-config] eingebaute Modell-Provider gesperrt (disabledProviders, Profil-Ebene)"
    else
        echo "[render-omp-config] WARN: omp config set disabledProviders fehlgeschlagen — eingebaute Provider (openai u. a.) bleiben für Subagenten erreichbar"
    fi
    if [ -n "$_ROLES_JSON" ] \
        && omp config set modelRoles "$_ROLES_JSON" >/dev/null 2>&1; then
        echo "[render-omp-config] Modell-Rollen (${_MODEL_ROLES}) → ${OMP_MODEL_SELECTOR}"
    else
        echo "[render-omp-config] WARN: omp config set modelRoles fehlgeschlagen — Subagenten könnten an einem eingebauten Provider statt an ${OMP_MODEL_SELECTOR} landen"
    fi
    if omp config set task.agentModelOverrides '{}' >/dev/null 2>&1; then
        :
    else
        echo "[render-omp-config] WARN: omp config set task.agentModelOverrides fehlgeschlagen — ein alter Eintrag pro Subagent schlägt die Modell-Rollen"
    fi

    # Nicht-Vision-Fall: statt nur `modelRoles.vision` unbelegt zu lassen
    # (omps eigener Auflösungspfad sucht dann quer über ALLE registrierten
    # Modelle nach einem bildfähigen und würde trotzdem den eingebauten
    # Standard-Provider treffen — siehe Kommentar oben), wird
    # `images.blockImages: true` gesetzt. Das ist im omp-Binary die ERSTE
    # Prüfung vor jeder Modell-Auflösung für Bildfragen
    # (`if (settings.get("images.blockImages")) throw …`) — omp lehnt das Bild
    # dann mit einer eigenen, klaren Fehlermeldung ab, statt den
    # `sk-noauth`-Schlüssel gegen api.openai.com zu verschicken.
    # The vision role itself is part of the modelRoles write above (its own
    # line says whether it landed), so this line claims only what it set.
    if [ "$_SUPPORTS_VISION" = "1" ]; then
        omp config set images.blockImages false >/dev/null 2>&1 \
            && omp config set images.questionTimeoutMs 120000 >/dev/null 2>&1 \
            && echo "[render-omp-config] Vision aktiv: images.blockImages=false, images.questionTimeoutMs=120000" \
            || echo "[render-omp-config] WARN: omp config set (Vision) fehlgeschlagen"
    else
        omp config set images.blockImages true >/dev/null 2>&1 \
            && echo "[render-omp-config] kein Vision-Modell: images.blockImages=true (omp lehnt Bilder ab statt extern nachzufragen)" \
            || echo "[render-omp-config] WARN: omp config set (images.blockImages) fehlgeschlagen"
    fi

    # ── Browser: der gemeinsame Agenten-Browser (cdp-browser) ───────────────
    # omps eigener Chrome-Start scheitert auf arm64 immer. Stattdessen hängt
    # omp sich über das lokale Relay (cdp-relay.sh, 127.0.0.1) an den
    # gemeinsamen Chromium — mit WebGL, und sichtbar im Browser-Panel.
    # Gesetzt wird das nur, wenn der Dienstname auflöst: ohne laufenden
    # cdp-browser (Browser-Profil aus, fremde Installation) behält omp sein
    # eigenes Verhalten, statt gegen ein totes Relay zu laufen. Ein Reload
    # (`docker exec … render-omp-config.sh`) zieht beides neu nach.
    _cdp_target="${OMP_BROWSER_CDP_TARGET-cdp-browser:9300}"
    _cdp_port="${OMP_BROWSER_CDP_PORT:-9222}"
    _cdp_host="${_cdp_target%:*}"
    case "$_cdp_target" in
        ""|off|none|0) _cdp_on=0 ;;
        *)
            if python3 -c 'import socket, sys; socket.gethostbyname(sys.argv[1])' \
                    "$_cdp_host" >/dev/null 2>&1; then
                _cdp_on=1
            else
                _cdp_on=0
            fi
            ;;
    esac
    if [ "$_cdp_on" = "1" ]; then
        # Agent-Zuordnung (04.10.2026): steht hinter dem Ziel cdp-gateway,
        # trägt die cdpUrl den Pfad `/a/<slug>` — dieselbe Entscheidung, die
        # cdp-relay.sh trifft (beide fragen cdp_relay.py --agent-path, eine
        # Quelle). Allein reicht das NICHT: Puppeteer wirft den Pfad für
        # /json/version weg; das Relay setzt ihn darum auf jede Anfrage
        # selbst. Hier macht er die Zuordnung sichtbar (`omp config get
        # browser.cdpUrl`) und gilt auch für omps eigene Erreichbarkeits-Probe.
        _self="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"
        _relay_py="$(cd "$(dirname "$_self")" && pwd)/cdp_relay.py"
        _cdp_agent_path=""
        if [ -f "$_relay_py" ]; then
            _cdp_agent_path="$(OMP_BROWSER_CDP_TARGET="$_cdp_target" python3 "$_relay_py" --agent-path 2>/dev/null || true)"
        fi
        _cdp_url="http://127.0.0.1:${_cdp_port}${_cdp_agent_path}"
        omp config set browser.cdpUrl "$_cdp_url" >/dev/null 2>&1 \
            && echo "[render-omp-config] Browser: gemeinsamer Agenten-Browser (${_cdp_target} über ${_cdp_url})" \
            || echo "[render-omp-config] WARN: omp config set browser.cdpUrl fehlgeschlagen"
    else
        omp config reset browser.cdpUrl >/dev/null 2>&1 || true
        echo "[render-omp-config] Browser: kein gemeinsamer Agenten-Browser erreichbar (${_cdp_target:-aus}) — omp nutzt sein eigenes Verhalten"
    fi
fi

echo "[render-omp-config] models.yml + omp.env geschrieben (${_BASE_URL}, Modell ${_MODEL})"
