#!/bin/bash
# start-claude.sh — claude-code Launcher mit SOUL.md als System-Prompt.
#
# Wird von entrypoint.sh via tmux aufgerufen. Drei Aufgaben:
#
#   1. /home/agent/.claude/.env sourcen wenn vorhanden (CLAUDE_CODE_OAUTH_TOKEN
#      kommt aus Bootstrap-Response, siehe entrypoint.sh).
#
#   2. CARD.md (falls vorhanden, Context-Economy Stufe 2 Opt-in) oder sonst
#      SOUL.md lesen und als --append-system-prompt an claude weiterreichen
#      (Brücke zwischen Template-Renderer und claude-code CLI).
#
#   3. claude starten. env-Vars werden vom Prozess geerbt.
#
# Chat mode (`start-claude.sh --chat <chat-id> <claude args...>`, chats
# slice S3, backend/app/services/claude_chat_runner.py): a further chat of
# this agent runs in its own tmux window with its own CLAUDE_CONFIG_DIR
# (<claude-home>/chats/<chat-id>/), so its session file never lands next to
# the agent's own ones. The backend computes every claude argument (session
# id, resume, system-prompt file) — this script only sets the environment,
# prepares the chat's config dir and execs `claude <args>`. Without
# `--chat` nothing below changes.
#
# Unterschiede zu docker/mc-agent-base/start-claude.sh (openclaude-Variant):
#   - Binary: `claude` statt `openclaude`
#   - Kein OpenAI-Shim — claude-code nutzt CLAUDE_CODE_OAUTH_TOKEN direkt
#   - Modell kommt aus settings.json (CLAUDE_CONFIG_DIR/settings.json)
#
# Weitere MC-Config-Files (TOOLS.md, HEARTBEAT.md, USER.md, MEMORY.md) liegen
# im selben Verzeichnis (/home/agent/.claude/) und können vom Agent per
# Tool-Call gelesen werden.

set -eu

# MC_CLAUDE_HOME only exists for the tests (backend/tests/test_claude_chat_runner.py);
# in the container it is never set.
CLAUDE_HOME="${MC_CLAUDE_HOME:-/home/agent/.claude}"
ENV_FILE="$CLAUDE_HOME/.env"
CARD_FILE="$CLAUDE_HOME/CARD.md"
SOUL_FILE="$CLAUDE_HOME/SOUL.md"
# Context-Economy Stufe 2: CARD.md (<=5.5KB) ersetzt SOUL.md (~29KB) als
# --append-system-prompt, aber nur fuer Agenten mit gesetztem Opt-in-Flag
# (docker_agent_sync.write_operating_card schreibt/loescht die Datei je nach
# agent.use_operating_card). -s statt -f: eine LEERE CARD.md (0 Byte) muss
# wie "fehlt" behandelt werden, sonst startet der Agent ganz ohne
# System-Prompt statt auf SOUL.md zurueckzufallen (matcht den -s-Check unten).
[ -s "$CARD_FILE" ] || CARD_FILE="$SOUL_FILE"
CLAUDE_ARGS="--dangerously-skip-permissions"

# Schritt 1: .env sourcen (wenn vorhanden) — überschreibt Container-env
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

if [ "${1:-}" = "--chat" ]; then
    CHAT_ID="${2:-}"
    case "$CHAT_ID" in
        ""|*[!0-9a-fA-F-]*)
            echo "[start-claude] --chat needs a chat id (uuid), got: '$CHAT_ID'" >&2
            exit 2
            ;;
    esac
    shift 2
    CHAT_DIR="$CLAUDE_HOME/chats/$CHAT_ID"
    mkdir -p "$CHAT_DIR"
    # Shared with the agent's own config (model, hooks, status line, skills,
    # plugins): links, so a change by the agent's sync reaches every chat.
    # The chat's own: projects/ (its session files), .claude.json (trust,
    # onboarding), history.
    for _name in settings.json settings.local.json skills plugins agents commands CLAUDE.md statusline-mc.sh; do
        if [ -e "$CLAUDE_HOME/$_name" ] && [ ! -e "$CHAT_DIR/$_name" ] && [ ! -L "$CHAT_DIR/$_name" ]; then
            ln -s "../../$_name" "$CHAT_DIR/$_name"
        fi
    done
    # First start of a new config dir: claude would ask for theme, folder
    # trust and the skip-permissions warning — a chat cannot answer those.
    # Start from the agent's own .claude.json (it has answered them) and set
    # the same trust entries entrypoint.sh sets for the agent.
    MC_CHAT_PARENT_JSON="$CLAUDE_HOME/.claude.json" MC_CHAT_JSON="$CHAT_DIR/.claude.json" python3 - <<'PY' || echo "[start-claude] warn: could not prepare $CHAT_DIR/.claude.json" >&2
import json, os

parent, path = os.environ["MC_CHAT_PARENT_JSON"], os.environ["MC_CHAT_JSON"]
d = {}
for src in (path, parent):
    if os.path.exists(src):
        try:
            with open(src) as f:
                d = json.load(f)
            break
        except Exception:
            d = {}
d["hasCompletedOnboarding"] = True
d["bypassPermissionsModeAccepted"] = True
projects = d.setdefault("projects", {})
for p in ("/home/agent", "/workspace"):
    e = projects.setdefault(p, {})
    e["hasTrustDialogAccepted"] = True
    e.setdefault("allowedTools", [])
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp, path)
PY
    export CLAUDE_CONFIG_DIR="$CHAT_DIR"
    # macOS keeps the login in the keychain under a name derived from
    # CLAUDE_CONFIG_DIR; empty = the normal entry, so one login serves every
    # chat dir (measured 19.11.2026, Claude Code 2.1.323). Linux reads the
    # login from CLAUDE_CODE_OAUTH_TOKEN (set by entrypoint.sh / .env).
    if [ "$(uname -s)" = "Darwin" ]; then
        export CLAUDE_SECURESTORAGE_CONFIG_DIR=
    fi
    exec claude "$@"
fi

# Schritt 2+3: CARD.md/SOUL.md als system-prompt + claude starten
if [ -s "$CARD_FILE" ]; then
    exec claude $CLAUDE_ARGS --append-system-prompt "$(cat "$CARD_FILE")"
else
    exec claude $CLAUDE_ARGS
fi
