#!/usr/bin/env bash
# install-boss-host.sh — Host-Agent (Boss) Runtime-Scripts an Repo anbinden
#
# Problem: Boss laeuft Host-native (claude-opus-4-7 via launchd), nicht im
# Docker-Container. Seine Runtime-Scripts (poll.sh, entrypoint.sh, start-
# claude.sh) liegen unter ~/.mc/agents/boss-host/. Ohne diesen Installer
# muesste man nach jedem Git-Pull die Scripts manuell cp'en — Drift-Risiko.
# Passiert ist das am 2026-04-23: PR #85 fixte Bug C in docker/boss-host/poll.sh,
# aber die Host-Kopie wurde nicht synchronisiert. Boss lief weiter mit dem
# kaputten Shell-Escape, Close-Reminder kamen nie an, der Operator bekam
# Auto-Close-Eskalations-Telegram.
#
# Loesung: Runtime-Scripts als Symlinks von ~/.mc/agents/boss-host/
# auf die kanonischen Versionen in <repo>/docker/boss-host/. Git-pull am Repo
# aktualisiert automatisch die Host-Runtime.
#
# WICHTIG: Nicht-Runtime-Files (agent.env, settings.json, claude-config/, logs/)
# bleiben unberuehrt — die sind agent-/DB-spezifisch und gehoeren in den
# Host-Pfad, nicht ins Git.
#
# Usage:
#   scripts/install-boss-host.sh              # Symlinks + launchd reload
#   scripts/install-boss-host.sh --no-reload  # Symlinks ohne launchd reload
#   scripts/install-boss-host.sh --dry-run    # Zeigen was passieren wuerde
#
# Exit-Codes:
#   0  Erfolgreich
#   1  Fehler (Host-Pfad fehlt, permission denied, etc.)
#   2  Config-Problem (launchd plist fehlt)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$ROOT/docker/boss-host"
HOST_DIR="$HOME/.mc/agents/boss-host"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/com.openclaw.boss.plist"
LAUNCHD_LABEL="com.openclaw.boss"

# Runtime-Files die via Symlink auf Repo gebunden werden (Drift-frei).
# NICHT in der Liste: agent.env, settings.json, claude-config/, logs/,
# .tmux.conf, .tmux.sock, com.openclaw.boss*.plist — die sind entweder
# agent-spezifisch (env/config) oder werden separat installiert (plist).
SYMLINK_FILES=(
    "poll.sh"
    "entrypoint.sh"
    "start-claude.sh"
)

DRY_RUN=false
DO_RELOAD=true
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --no-reload) DO_RELOAD=false ;;
        -h|--help)
            sed -n '1,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown arg: $arg" >&2; exit 1 ;;
    esac
done

log() { echo "[install-boss-host] $*"; }

run() {
    if [ "$DRY_RUN" = true ]; then
        echo "  DRY-RUN: $*"
    else
        "$@"
    fi
}

# Preflight
if [ ! -d "$REPO_DIR" ]; then
    log "ERROR: Repo-Pfad fehlt: $REPO_DIR"
    exit 1
fi
if [ ! -d "$HOST_DIR" ]; then
    log "Host-Pfad existiert nicht — lege ihn an: $HOST_DIR"
    run mkdir -p "$HOST_DIR/logs"
fi

# Source-Files existieren?
for f in "${SYMLINK_FILES[@]}"; do
    if [ ! -f "$REPO_DIR/$f" ]; then
        log "ERROR: Source-File fehlt im Repo: $REPO_DIR/$f"
        exit 1
    fi
done

# Symlinks setzen. Wenn ein File schon ein Symlink auf das richtige Target ist,
# idempotent skippen. Wenn ein echtes File existiert: entfernen (das war die
# manuelle Kopie) und durch Symlink ersetzen.
log "Setze Symlinks: $HOST_DIR/{${SYMLINK_FILES[*]}} → $REPO_DIR/"
for f in "${SYMLINK_FILES[@]}"; do
    src="$REPO_DIR/$f"
    dst="$HOST_DIR/$f"

    if [ -L "$dst" ]; then
        current_target="$(readlink "$dst")"
        if [ "$current_target" = "$src" ]; then
            log "  $f — Symlink korrekt (idempotent skip)"
            continue
        fi
        log "  $f — Symlink zeigt auf $current_target, aktualisiere..."
        run rm "$dst"
    elif [ -f "$dst" ]; then
        log "  $f — echte Kopie gefunden, ersetze durch Symlink..."
        run rm "$dst"
    fi

    run ln -s "$src" "$dst"
    log "  $f → $src"
done

# `mc` CLI auf Host installieren. Boss laeuft Host-native, nicht im Docker-
# Container — er hat keinen /home/agent/.local/bin/mc Symlink. Ohne diesen
# Schritt muss Boss `mc delegate`/`mc done`/etc. via rohem `curl POST` ersetzen
# → Anti-Pattern (z.B. mc task-create + mc blocked statt atomic mc delegate).
# Das war bis 2026-04-23 ein unbemerktes Problem: Boss's SOUL sagte
# "Nutze IMMER `mc delegate`" — aber `mc` existierte nicht im Host-PATH.
HOST_MC_BIN="$HOME/.local/bin/mc"
REPO_MC_BIN="$ROOT/scripts/mc-cli/mc"
log "Setze Symlink: $HOST_MC_BIN → $REPO_MC_BIN"
if [ ! -f "$REPO_MC_BIN" ]; then
    log "ERROR: mc CLI fehlt im Repo: $REPO_MC_BIN"
    exit 1
fi
if [ -L "$HOST_MC_BIN" ] && [ "$(readlink "$HOST_MC_BIN")" = "$REPO_MC_BIN" ]; then
    log "  mc — Symlink korrekt (idempotent skip)"
else
    run mkdir -p "$(dirname "$HOST_MC_BIN")"
    if [ -e "$HOST_MC_BIN" ] || [ -L "$HOST_MC_BIN" ]; then
        run rm -f "$HOST_MC_BIN"
    fi
    run ln -s "$REPO_MC_BIN" "$HOST_MC_BIN"
    log "  mc → $REPO_MC_BIN"
    log "  (Pruefe: 'command -v mc && mc --help | head -5')"
fi

# launchd plist: auch im Repo gepflegt, aber wird in ~/Library/LaunchAgents/
# erwartet. Wenn Repo-Version abweicht → warnen (nicht auto-kopieren, das
# ist eine separate Install-Aktion die der User bewusst macht).
if [ -f "$LAUNCHD_PLIST" ] && [ -f "$REPO_DIR/com.openclaw.boss.plist" ]; then
    if ! diff -q "$LAUNCHD_PLIST" "$REPO_DIR/com.openclaw.boss.plist" > /dev/null 2>&1; then
        log "WARNING: launchd plist weicht vom Repo ab:"
        log "  $LAUNCHD_PLIST"
        log "  vs $REPO_DIR/com.openclaw.boss.plist"
        log "  (nicht automatisch ueberschrieben — manuell pruefen + cp wenn gewuenscht)"
    fi
fi

# launchd reload — lädt die neuen poll.sh/entrypoint.sh-Inhalte durch Kill+Restart
# des Boss-Prozesses. Unterbricht aktive Arbeit; mit --no-reload skippable.
if [ "$DO_RELOAD" = true ]; then
    if [ ! -f "$LAUNCHD_PLIST" ]; then
        log "WARNING: launchd plist nicht gefunden: $LAUNCHD_PLIST — skip reload"
        log "  (install es mit: cp $REPO_DIR/com.openclaw.boss.plist $LAUNCHD_PLIST && launchctl load $LAUNCHD_PLIST)"
        exit 2
    fi

    log "Reload launchd Agent '$LAUNCHD_LABEL'..."
    if launchctl list | grep -q "$LAUNCHD_LABEL"; then
        run launchctl unload "$LAUNCHD_PLIST"
        run sleep 2
    fi
    run launchctl load "$LAUNCHD_PLIST"

    # Verify — launchctl list hat manchmal bis zu 5s Lag nach load(),
    # deswegen retry-Loop statt single-check.
    if [ "$DRY_RUN" = false ]; then
        for i in 1 2 3 4 5 6; do
            sleep 2
            if launchctl list | grep -q "$LAUNCHD_LABEL"; then
                log "  ✓ $LAUNCHD_LABEL geladen (nach ${i}x2s)"
                break
            fi
            if [ "$i" = "6" ]; then
                log "  ✗ $LAUNCHD_LABEL NICHT geladen nach 12s — pruefe launchd logs"
                exit 1
            fi
        done
    fi
else
    log "Reload uebersprungen (--no-reload). Boss laeuft mit altem poll.sh bis zum manuellen Reload:"
    log "  launchctl unload $LAUNCHD_PLIST && launchctl load $LAUNCHD_PLIST"
fi

log "Fertig."
