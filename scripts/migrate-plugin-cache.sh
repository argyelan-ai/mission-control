#!/bin/bash
# Migriert Plugin-Cache von pro-Agent-Kopien zu Shared Cache mit Symlinks.
# Einmalig ausfuehren, idempotent.

set -euo pipefail
PLUGINS_DIR="$HOME/.openclaw/plugins"
AGENTS_DIR="$HOME/.openclaw/agents"
TEMPLATE_DIR="$AGENTS_DIR/_template/claude-config/plugins"

echo "=== Shared Plugin Cache Migration ==="

# 1. Zentralen Cache erstellen (aus _template kopieren)
if [ ! -d "$PLUGINS_DIR/cache" ]; then
    mkdir -p "$PLUGINS_DIR"
    if [ -d "$TEMPLATE_DIR/cache" ]; then
        cp -r "$TEMPLATE_DIR/cache/" "$PLUGINS_DIR/cache/"
        echo "Cache kopiert von _template"
    else
        echo "FEHLER: _template/claude-config/plugins/cache nicht gefunden"
        exit 1
    fi
else
    echo "Shared cache existiert bereits, ueberspringe"
fi

# Master installed_plugins.json
if [ ! -f "$PLUGINS_DIR/installed_plugins.json" ]; then
    cp "$TEMPLATE_DIR/installed_plugins.json" "$PLUGINS_DIR/"
    echo "installed_plugins.json kopiert"
fi

# marketplaces/ (lokaler Index fuer Plugin-Suche)
if [ ! -d "$PLUGINS_DIR/marketplaces" ]; then
    cp -r "$TEMPLATE_DIR/marketplaces" "$PLUGINS_DIR/marketplaces"
    echo "marketplaces/ kopiert"
fi

# plugin-store/ Wrapper (CLAUDE_CONFIG_DIR zeigt hierhin)
STORE_DIR="$HOME/.openclaw/plugin-store"
if [ ! -d "$STORE_DIR" ]; then
    mkdir -p "$STORE_DIR"
    ln -sfn ../plugins "$STORE_DIR/plugins"
    echo "plugin-store/ erstellt"
fi

# known_marketplaces.json
if [ ! -f "$PLUGINS_DIR/known_marketplaces.json" ]; then
    cp "$TEMPLATE_DIR/known_marketplaces.json" "$PLUGINS_DIR/"
    echo "known_marketplaces.json kopiert"
fi

# 2. Pro Agent: Cache durch Symlink ersetzen
for agent_dir in "$AGENTS_DIR"/*/; do
    agent_name=$(basename "$agent_dir")
    plugin_dir="$agent_dir/claude-config/plugins"

    [ ! -d "$plugin_dir" ] && continue

    # Cache-Verzeichnis durch Symlink ersetzen
    if [ -d "$plugin_dir/cache" ] && [ ! -L "$plugin_dir/cache" ]; then
        rm -rf "$plugin_dir/cache"
        ln -s "../../../plugins/cache" "$plugin_dir/cache"
        echo "$agent_name: cache -> symlink"
    elif [ -L "$plugin_dir/cache" ]; then
        echo "$agent_name: cache bereits symlink"
    fi

    # known_marketplaces.json symlinken
    if [ -f "$plugin_dir/known_marketplaces.json" ] && [ ! -L "$plugin_dir/known_marketplaces.json" ]; then
        rm "$plugin_dir/known_marketplaces.json"
        ln -s "../../../plugins/known_marketplaces.json" "$plugin_dir/known_marketplaces.json"
        echo "$agent_name: known_marketplaces -> symlink"
    elif [ -L "$plugin_dir/known_marketplaces.json" ]; then
        echo "$agent_name: known_marketplaces bereits symlink"
    fi
done

echo ""
echo "=== Migration abgeschlossen ==="
du -sh "$PLUGINS_DIR/cache/" 2>/dev/null || true
