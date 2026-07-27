#!/bin/bash
# start-claude.sh — Host-Variante des Boss-Launchers
#
# Startet das offizielle claude binary mit OAuth-Login (aus macOS Keychain
# unter ~/.claude/) + SOUL.md als --append-system-prompt.
#
# Modell: kommt aus agent.env (ANTHROPIC_MODEL, gesourced unten aus
# runtime.model_identifier — siehe backend/app/routers/internal.py
# build_runtime_env). Direkter Anthropic-API-Call, KEIN openclaude/
# LM-Studio-Detour wie im Container.
#
# Wird von entrypoint.sh via tmux aufgerufen (Task B4).

set -eu

CONFIG_DIR="$HOME/.mc/agents/boss-host/claude-config"
CARD_FILE="$CONFIG_DIR/CARD.md"
SOUL_FILE="$CONFIG_DIR/SOUL.md"
# Context-Economy Stufe 2: CARD.md (<=5KB) ersetzt SOUL.md (~29KB) als
# --append-system-prompt, aber nur fuer Agenten mit gesetztem Opt-in-Flag
# (docker_agent_sync.write_operating_card schreibt/loescht die Datei je nach
# agent.use_operating_card). -s statt -f: eine LEERE CARD.md (0 Byte) muss
# wie "fehlt" behandelt werden, sonst startet der Agent ganz ohne
# System-Prompt statt auf SOUL.md zurueckzufallen (matcht den -s-Check unten).
[ -s "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"
CLAUDE_BIN="$HOME/.local/bin/claude"
# Boss-eigene MCP-Config — leer fuer jetzt, kann spaeter erweitert werden.
# Wird mit --strict-mcp-config genutzt damit claude die persoenliche
# ~/.claude.json des Operators (mit youtube-transcript etc.) NICHT als MCP-Quelle laedt.
# Loest "1 MCP server failed" warning + Privacy/Security-Issue. Siehe
# docs/plans/2026-04-25-boss-host-claude-config-isolation.md (Phase 1).
MCP_CONFIG="$CONFIG_DIR/.mcp.json"
[ -f "$MCP_CONFIG" ] || echo '{"mcpServers": {}}' > "$MCP_CONFIG"

# agent.env defensiv sourcen — falls claude unabhängig vom entrypoint
# (z.B. via tmux respawn-window) neugestartet wird, brauchen wir die
# MC_API_URL + MC_AGENT_TOKEN env-Vars. Die unsets unten betreffen nur
# ANTHROPIC/OPENAI Vars, nicht MC_*.
ENV_FILE="$HOME/.mc/agents/boss-host/agent.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Sicherstellen: Wir nutzen das ECHTE claude (nicht openclaude)
if ! "$CLAUDE_BIN" --version 2>&1 | grep -q "Claude Code"; then
    echo "FEHLER: $CLAUDE_BIN ist nicht das offizielle Claude Code Binary." >&2
    exit 1
fi

# Container-Boss-Env-Contamination entfernen — diese Vars routen claude
# zu LM Studio/Ollama statt api.anthropic.com:
unset CLAUDE_CONFIG_DIR    # damit claude OAuth-Keychain unter ~/.claude/ findet
unset ANTHROPIC_BASE_URL
unset OPENAI_BASE_URL
unset OPENAI_API_KEY
unset OPENAI_MODEL
unset CLAUDE_CODE_USE_OPENAI
# ANTHROPIC_MODEL absichtlich NICHT unset — das ist der einzige Modellkanal
# fuer Host-Claude (kommt aus agent.env, oben gesourced) und darf hier nicht
# verloren gehen.

if [ -n "${ANTHROPIC_MODEL:-}" ]; then
    # runtime.model_identifier ist die einzige Wahrheit: agent.env wurde oben
    # gesourced (Z. 37-43) und hat ANTHROPIC_MODEL bereits gesetzt. Nichts tun.
    echo "[start-claude] ANTHROPIC_MODEL=$ANTHROPIC_MODEL (aus agent.env)"
else
    # Seit 2026-07-25 ist harness "claude" in HOST_ADAPTERS registriert
    # (backend/app/services/host_harness_adapter.py -> ClaudeHostAdapter), d.h.
    # sync_host_agent_model schreibt ANTHROPIC_MODEL aus runtime.model_identifier
    # regulaer in diese agent.env. Fehlt der Wert trotzdem, ist der Agent an
    # keine Runtime (oder an eine ohne model_identifier) gebunden.
    #
    # Dann wird KEIN Modell gepinnt: das claude-CLI nimmt seinen Account-Default,
    # der neuen Releases automatisch folgt. Ein hier hart eingetragener Pin
    # veraltet dagegen still (genau der Vorfall: Fleet blieb auf einem alten
    # opus, waehrend Anthropic laengst weiter war). Warnen statt raten.
    echo "WARN: ANTHROPIC_MODEL fehlt in $ENV_FILE — Boss ist an keine Runtime mit model_identifier gebunden. Kein Pin gesetzt, claude nutzt seinen Account-Default." >&2
fi

# --dangerously-skip-permissions matcht aktuelles Container-Verhalten.
# Whitelist wurde bewusst NICHT eingebaut (Operator-Vorgabe: "perfekt + sauber" =
# kein Funktionsverlust gegenueber Container-Boss). Bei Bedarf spaeter
# via --allowed-tools "Read,Grep,..." oder --permission-mode einschraenken.
if [ -s "$CARD_FILE" ]; then
    exec "$CLAUDE_BIN" \
        --dangerously-skip-permissions \
        --strict-mcp-config \
        --mcp-config "$MCP_CONFIG" \
        --append-system-prompt "$(cat "$CARD_FILE")"
else
    echo "WARN: $CARD_FILE leer oder fehlt — starte ohne system-prompt" >&2
    exec "$CLAUDE_BIN" \
        --dangerously-skip-permissions \
        --strict-mcp-config \
        --mcp-config "$MCP_CONFIG"
fi
