#!/bin/bash
# start-claude.sh — Host-Variante des Boss-Launchers
#
# Startet das offizielle claude binary mit OAuth-Login (aus macOS Keychain
# unter ~/.claude/) + SOUL.md als --append-system-prompt.
#
# Modell: claude-opus-4-7 (1M context). Direkter Anthropic-API-Call,
# KEIN openclaude/LM-Studio-Detour wie im Container.
#
# Wird von entrypoint.sh via tmux aufgerufen (Task B4).

set -eu

CONFIG_DIR="$HOME/.mc/agents/boss-host/claude-config"
SOUL_FILE="$CONFIG_DIR/SOUL.md"
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

# Modell explizit auf Opus 4.8
export ANTHROPIC_MODEL="claude-opus-4-8"

# --dangerously-skip-permissions matcht aktuelles Container-Verhalten.
# Whitelist wurde bewusst NICHT eingebaut (Operator-Vorgabe: "perfekt + sauber" =
# kein Funktionsverlust gegenueber Container-Boss). Bei Bedarf spaeter
# via --allowed-tools "Read,Grep,..." oder --permission-mode einschraenken.
if [ -s "$SOUL_FILE" ]; then
    exec "$CLAUDE_BIN" \
        --dangerously-skip-permissions \
        --strict-mcp-config \
        --mcp-config "$MCP_CONFIG" \
        --append-system-prompt "$(cat "$SOUL_FILE")"
else
    echo "WARN: $SOUL_FILE leer oder fehlt — starte ohne system-prompt" >&2
    exec "$CLAUDE_BIN" \
        --dangerously-skip-permissions \
        --strict-mcp-config \
        --mcp-config "$MCP_CONFIG"
fi
