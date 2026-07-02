#!/usr/bin/env bash
# auto-rebuild-agents-on-drift.sh
#
# Detects when agent container scripts (poll.sh / recycler.sh / entrypoint.sh /
# start-claude.sh / lib/) have drifted from what's running in each agent
# container, and rebuilds + recreates the affected containers — skipping any
# container that's actively working on a task.
#
# Idempotent no-op when nothing drifted (typical case). Logs to
# /tmp/mc-auto-rebuild.log. Emits a JSON systemMessage to stdout if it did work,
# so the Claude Code Stop hook surfaces it to the operator.
#
# Intended to run as a Stop hook from .claude/settings.local.json.
# Safe to invoke manually too.
#
# Root cause this guards against: 2026-05-12 Sparky session-recycler bug —
# container ran an outdated recycler.sh that lacked the .task-active.lock
# guard; the new code was in the repo but the container was never rebuilt.

set -euo pipefail

REPO="${HOME}/Workspace/Projects/mission-control"
LOG="/tmp/mc-auto-rebuild.log"

# Agent containers and their image variant (mc-agent-base vs mc-claude-agent).
# macOS ships bash 3.2 — no associative arrays — so encode as
# "container:variant" lines and look up via a function.
ALL_AGENTS="mc-agent-sparky:base
mc-agent-shakespeare:base
mc-agent-freecode:base
mc-agent-researcher:base
mc-agent-rex:claude
mc-agent-davinci:claude
mc-agent-deployer:claude
mc-agent-tester:claude"

variant_for() {
    # Echo "base" or "claude" for the given container name, empty if unknown.
    echo "$ALL_AGENTS" | awk -F: -v c="$1" '$1==c{print $2; exit}'
}

# Files watched per image variant (repo path → container path)
# poll.sh is the SHARED source — synced into both contexts by build-agent-images.sh
files_for_variant() {
    local variant=$1
    case "$variant" in
        base)
            cat <<EOF
docker/shared/poll.sh|/home/agent/poll.sh
docker/mc-agent-base/recycler.sh|/home/agent/recycler.sh
docker/mc-agent-base/entrypoint.sh|/home/agent/entrypoint.sh
docker/mc-agent-base/start-claude.sh|/home/agent/start-claude.sh
docker/mc-agent-base/lib/turn-state.sh|/home/agent/lib/turn-state.sh
EOF
            ;;
        claude)
            cat <<EOF
docker/shared/poll.sh|/home/agent/poll.sh
docker/mc-claude-agent/recycler.sh|/home/agent/recycler.sh
docker/mc-claude-agent/entrypoint.sh|/home/agent/entrypoint.sh
EOF
            ;;
    esac
}

# Compute md5 of a local file (macOS `md5 -q` or GNU `md5sum`)
local_md5() {
    if command -v md5 >/dev/null 2>&1; then
        md5 -q "$1" 2>/dev/null
    else
        md5sum "$1" 2>/dev/null | awk '{print $1}'
    fi
}

# Compute md5 of a file inside a container (busybox `md5sum` exists in alpine)
container_md5() {
    docker exec "$1" md5sum "$2" 2>/dev/null | awk '{print $1}'
}

# Pre-flight
cd "$REPO" 2>/dev/null || exit 0
docker info >/dev/null 2>&1 || exit 0   # Docker daemon down → silent exit

# Pass 1: detect drift per container
ALL_DRIFTED=()
DRIFTED_VARIANTS=""   # space-separated unique list ("base" / "claude")

while IFS=: read -r c variant; do
    [ -n "$c" ] || continue
    docker ps -q --filter "name=^/${c}\$" | grep -q . || continue   # not running

    drifted=0
    while IFS='|' read -r src dst; do
        [ -n "$src" ] || continue
        [ -f "$REPO/$src" ] || continue
        repo_hash=$(local_md5 "$REPO/$src")
        cont_hash=$(container_md5 "$c" "$dst")
        [ -n "$repo_hash" ] && [ -n "$cont_hash" ] && [ "$repo_hash" != "$cont_hash" ] && {
            drifted=1
            break
        }
    done < <(files_for_variant "$variant")

    if [ "$drifted" = "1" ]; then
        ALL_DRIFTED+=("$c")
        case " $DRIFTED_VARIANTS " in
            *" $variant "*) ;;
            *) DRIFTED_VARIANTS="$DRIFTED_VARIANTS $variant" ;;
        esac
    fi
done <<< "$ALL_AGENTS"

# Nothing drifted → silent exit (typical case for non-script edits)
[ ${#ALL_DRIFTED[@]} -eq 0 ] && exit 0

# Pass 2: filter out containers that are mid-task (Task-Active-Guard)
SAFE=()
BUSY=()
for c in "${ALL_DRIFTED[@]}"; do
    has_lock=$(docker exec "$c" sh -c 'test -f /home/agent/.task-active.lock && echo Y' 2>/dev/null || true)
    has_poll=$(docker exec "$c" pgrep -f '/home/agent/poll.sh' 2>/dev/null || true)
    if [ "$has_lock" = "Y" ] && [ -n "$has_poll" ]; then
        BUSY+=("$c")
    else
        SAFE+=("$c")
    fi
done

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
    echo "==================== [$ts] mc-auto-rebuild ===================="
    echo "Drifted: ${ALL_DRIFTED[*]}"
    [ ${#BUSY[@]} -gt 0 ] && echo "Skipped (mid-task): ${BUSY[*]}"
    echo "Will recreate: ${SAFE[*]:-<none>}"
} >> "$LOG"

if [ ${#SAFE[@]} -eq 0 ]; then
    # All drifted containers were busy. Notify but don't touch them.
    busy_csv=$(IFS=,; echo "${BUSY[*]}")
    printf '{"systemMessage": "⚠️  Container-Scripts gedriftet aber mid-task — skipped: %s. Rebuild manuell via scripts/auto-rebuild-agents-on-drift.sh wenn idle."}\n' "$busy_csv"
    exit 0
fi

# Pass 3: rebuild needed image(s) via the canonical build script
NEEDS_BASE=0
NEEDS_CLAUDE=0
for c in "${SAFE[@]}"; do
    v=$(variant_for "$c")
    case "$v" in
        base)   NEEDS_BASE=1 ;;
        claude) NEEDS_CLAUDE=1 ;;
    esac
done

if [ "$NEEDS_BASE" = "1" ] && [ "$NEEDS_CLAUDE" = "1" ]; then
    BUILD_TARGET="both"
elif [ "$NEEDS_BASE" = "1" ]; then
    BUILD_TARGET="openclaude"
else
    BUILD_TARGET="claude"
fi

echo "Building images: $BUILD_TARGET" >> "$LOG"
if ! "$REPO/scripts/build-agent-images.sh" "$BUILD_TARGET" >> "$LOG" 2>&1; then
    echo "ERROR: build-agent-images.sh failed" >> "$LOG"
    printf '{"systemMessage": "❌ Auto-Rebuild fehlgeschlagen beim Image-Build. Siehe %s"}\n' "$LOG"
    exit 0
fi

# Pass 4: recreate safe containers with both env-flags (per mc-container-lifecycle skill)
echo "Recreating: ${SAFE[*]}" >> "$LOG"
if ! docker compose \
    -f docker-compose.yml \
    -f docker/docker-compose.agents.yml \
    --env-file .env \
    --env-file docker/.env.agents \
    up -d --force-recreate --no-deps "${SAFE[@]}" >> "$LOG" 2>&1; then
    echo "ERROR: docker compose up failed" >> "$LOG"
    printf '{"systemMessage": "❌ Auto-Rebuild: docker compose up fehlgeschlagen. Siehe %s"}\n' "$LOG"
    exit 0
fi

# Verify env-tokens survived (per mc-container-lifecycle skill)
for c in "${SAFE[@]}"; do
    tok=$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
          | grep "^CLAUDE_CODE_OAUTH_TOKEN=" | cut -d= -f2-)
    mc=$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
         | grep "^MC_TOKEN=" | cut -d= -f2-)
    echo "  $c: CLAUDE=${#tok}c MC=${#mc}c" >> "$LOG"
done

echo "Done [$ts]" >> "$LOG"

# User-facing summary
safe_csv=$(IFS=,; echo "${SAFE[*]}")
suffix=""
[ ${#BUSY[@]} -gt 0 ] && suffix=" (mid-task skipped: $(IFS=,; echo "${BUSY[*]}"))"
printf '{"systemMessage": "🔄 Container-Scripts gedriftet → rebuilt + recreated: %s%s"}\n' "$safe_csv" "$suffix"
