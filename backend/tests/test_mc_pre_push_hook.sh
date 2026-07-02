#!/usr/bin/env bash
# test_mc_pre_push_hook.sh — smoke-test for the pre-push guard.
#
# Runs locally; asserts the hook aborts on wrong remote, passes on
# matching remote, and is silent when no expected-remote file is
# present. Invoked via tests/test_mc_pre_push_hook.py (pytest wrapper)
# so the whole thing shows up in the normal suite.
set -euo pipefail

HOOK="${1:-$(dirname "$0")/../../docker/mc-agent-base/lib/mc-pre-push.sh}"

if [ ! -f "$HOOK" ]; then
    echo "FAIL: hook not found at $HOOK" >&2
    exit 2
fi

fail() { echo "FAIL: $1" >&2; exit 1; }

run_case() {
    local label="$1"
    local expected_file_content="$2"   # empty string = no file
    local remote_url="$3"
    local should_pass="$4"             # "yes" | "no"

    local dir
    dir=$(mktemp -d)
    (cd "$dir" && git init -q && git remote add origin "$remote_url")
    if [ -n "$expected_file_content" ]; then
        printf '%s\n' "$expected_file_content" > "$dir/.mc-expected-remote"
    fi

    set +e
    (cd "$dir" && "$HOOK" origin "$remote_url" </dev/null >/tmp/hook-stdout 2>/tmp/hook-stderr)
    local rc=$?
    set -e

    if [ "$should_pass" = "yes" ] && [ "$rc" -ne 0 ]; then
        cat /tmp/hook-stderr >&2
        fail "$label: expected pass, got rc=$rc"
    fi
    if [ "$should_pass" = "no" ] && [ "$rc" -eq 0 ]; then
        fail "$label: expected block, hook passed"
    fi

    rm -rf "$dir"
    echo "  ok: $label"
}

echo "==== mc-pre-push hook smoke-tests ===="
run_case "no expected-remote file → silent pass" \
    "" \
    "https://github.com/test-owner/anything.git" \
    "yes"

run_case "matching remote → pass" \
    "https://github.com/test-owner/argyelan.ai.git" \
    "https://github.com/test-owner/argyelan.ai.git" \
    "yes"

run_case "matching remote without .git suffix → pass" \
    "https://github.com/test-owner/argyelan.ai" \
    "https://github.com/test-owner/argyelan.ai.git" \
    "yes"

run_case "ssh vs https same repo → pass" \
    "https://github.com/test-owner/argyelan.ai.git" \
    "git@github.com:test-owner/argyelan.ai.git" \
    "yes"

run_case "wrong remote → block" \
    "https://github.com/test-owner/argyelan.ai.git" \
    "https://github.com/test-owner/argyelan-website.git" \
    "no"

run_case "different owner → block" \
    "https://github.com/test-owner/argyelan.ai.git" \
    "https://github.com/someoneelse/argyelan.ai.git" \
    "no"

echo "==== all hook cases passed ===="
