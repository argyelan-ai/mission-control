#!/bin/bash
# init-mc-deliverables-dirs.sh — legt ~/.mc/deliverables/{agent}/ fuer alle
# Docker- und Host-Agents an. Idempotent, keine Side-Effects bei bereits
# existierenden Dirs.
#
# Wird von docker/docker-compose.agents.yml als Host-Mount-Target referenziert:
#   ${HOME}/.mc/deliverables/{slug}:/deliverables
#
# Wichtig: MUSS vor `docker compose up` laufen, sonst legt Docker die Dirs
# als root an (via bind-mount auto-creation) und der Host-User kann nicht
# mehr reinschreiben. (ADR-023 ultrareview Finding).
#
# ADR-022 (2026-04-21): BASE von ~/.mc-deliverables/ auf ~/.mc/deliverables/
# migriert — mirror the new MC-Home layout.
set -euo pipefail

BASE="${HOME}/.mc/deliverables"
# Agent-Slugs, die Container-seitig mounten. Planner + Neo (Migration 0086)
# + Cody bleiben hier als Legacy-Eintraege damit alte Deliverables nicht
# waisen (readonly, werden nicht mehr befuellt).
AGENTS=(
    boss
    davinci
    cody
    rex
    planner
    researcher
    deployer
    neo
    sparky
    shakespeare
    freecode
    tester
    henry
)

mkdir -p "$BASE"
for a in "${AGENTS[@]}"; do
    d="$BASE/$a"
    if [ ! -d "$d" ]; then
        mkdir -p "$d"
        echo "created: $d"
    else
        echo "exists:  $d"
    fi
done

# Preflight: scan /workspace-ref source for accidentally-committed secrets
# (ADR-023 ultrareview — reviewer concern). Warn the operator if any real .env files
# live inside ~/Workspace/Projects/, since that tree is ro-mounted into 6
# agents as /workspace-ref.
REF_DIR="${HOME}/Workspace/Projects"
if [ -d "$REF_DIR" ]; then
    # Only match real secret files, not .env.example / .env.template
    leaks=$(find "$REF_DIR" -maxdepth 4 -type f \
        \( -name ".env" -o -name ".env.local" -o -name ".env.production" \
           -o -name "*.pem" -o -name "*.key" -o -name "credentials.json" \) 2>/dev/null || true)
    if [ -n "$leaks" ]; then
        echo
        echo "⚠️  WARNUNG: Sensitive Dateien unter $REF_DIR gefunden:"
        echo "$leaks" | sed 's/^/   /'
        echo "   Diese werden 6 Agents via /workspace-ref:ro sichtbar."
        echo "   Bitte verschieben nach ~/.mc/secrets/ oder via .gitignore schuetzen."
    fi
fi

echo
echo "done — $BASE"
