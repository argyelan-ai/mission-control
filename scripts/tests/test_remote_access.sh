#!/usr/bin/env bash
# Behaviour tests for scripts/lib/remote-access.sh (install.sh / setup.sh).
#
# Finding 17.10.2026: a fresh install on a headless board listened on
# 127.0.0.1 with an empty PUBLIC_HOST, and the installer said "open
# http://localhost" — impossible on a machine without a browser. These tests
# drive the REAL library against a scratch .env; only the environment
# (SSH_CONNECTION, DISPLAY, uname, ip) and the prompt are stubbed.
#
# Sabotage probes (undo ONE line in remote-access.sh, exactly that test goes red):
#   ci-safe   : `choice=no` in the non-interactive branch → `choice=yes`
#               (an unattended install would open MC to the network)
#   remote    : the `mc_env_set MC_BIND_ADDRESS 0.0.0.0` line removed
#   url       : mc_access_url always printing http://localhost
#   sshaddr   : `addr="${3:-}"` → `addr="${1:-}"` (the CLIENT address)
#   badhost   : the `mc_valid_host` check in mc_configure_access removed
#   headless  : the SSH_CONNECTION test in mc_is_headless removed
#
# Usage: bash scripts/tests/test_remote_access.sh   (exit 0 = all green)
# The shell snippets below are single-quoted ON PURPOSE (expanded inside run).
# shellcheck disable=SC2016,SC2329
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/../lib/remote-access.sh"
EXAMPLE="$HERE/../../.env.example"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check() { # check NAME ACTUAL EXPECTED
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$3', got '$2'"; fi
}

# Fresh shell state per test: subshell + clean env + library sourced anew.
fresh_env() {
  cp "$EXAMPLE" "$SCRATCH/.env"
}
run() { # run "<shell code>" — executes in a subshell with the lib loaded
  (
    unset SSH_CONNECTION SSH_CLIENT SSH_TTY DISPLAY WAYLAND_DISPLAY \
      MC_INTERACTIVE MC_REMOTE MC_PUBLIC_HOST
    # shellcheck source=scripts/lib/remote-access.sh
    . "$LIB"
    eval "$1"
  )
}

echo "remote-access.sh"

# ── .env helpers ─────────────────────────────────────────────────────────────
fresh_env
check "example ships local-only" "$(run 'mc_env_get MC_BIND_ADDRESS "$SCRATCH/.env"')" "127.0.0.1"
check "example ships an empty PUBLIC_HOST" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" ""

printf 'A=1\nPUBLIC_HOST=\nB=2\n' >"$SCRATCH/kv"
run 'mc_env_set PUBLIC_HOST "a&b\\c|d" "$SCRATCH/kv"'
check "env_set keeps metacharacters literal" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/kv"')" 'a&b\c|d'
check "env_set replaces in place" "$(sed -n 2p "$SCRATCH/kv")" 'PUBLIC_HOST=a&b\c|d'
check "env_set keeps the other lines" "$(sed -n 1p "$SCRATCH/kv")$(sed -n 3p "$SCRATCH/kv")" "A=1B=2"
run 'mc_env_set NEW_KEY x "$SCRATCH/kv"'
check "env_set appends a missing key" "$(tail -n 1 "$SCRATCH/kv")" "NEW_KEY=x"

# ── ci-safe: unattended install never opens the network ─────────────────────
fresh_env
out=$(run 'uname() { echo Linux; }; mc_configure_access "$SCRATCH/.env"' 2>&1)
check "ci-safe: non-interactive keeps 127.0.0.1" "$(run 'mc_env_get MC_BIND_ADDRESS "$SCRATCH/.env"')" "127.0.0.1"
check "ci-safe: non-interactive leaves PUBLIC_HOST empty" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" ""
case "$out" in *"ssh -L 8080:localhost:80"*) ok "ci-safe: headless box gets the SSH tunnel hint" ;;
  *) bad "ci-safe: headless box gets the SSH tunnel hint" "$out" ;; esac
check "ci-safe: URL stays localhost" "$(run 'mc_access_url "$SCRATCH/.env"')" "http://localhost"

# ── remote: explicit address ─────────────────────────────────────────────────
fresh_env
out=$(run 'MC_REMOTE=yes MC_PUBLIC_HOST=192.0.2.20 mc_configure_access "$SCRATCH/.env"' 2>&1)
check "remote: binds to all interfaces" "$(run 'mc_env_get MC_BIND_ADDRESS "$SCRATCH/.env"')" "0.0.0.0"
check "remote: PUBLIC_HOST is the given address" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" "192.0.2.20"
check "url: install.sh prints the remote URL" "$(run 'mc_access_url "$SCRATCH/.env"')" "http://192.0.2.20"
case "$out" in *ANYONE*"tailscale serve"*) ok "remote: security note (first-admin race + Tailscale)" ;;
  *) bad "remote: security note (first-admin race + Tailscale)" "$out" ;; esac

fresh_env
run 'MC_PUBLIC_HOST=pi.local mc_configure_access "$SCRATCH/.env"' >/dev/null 2>&1
check "remote: a host name alone implies yes" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" "pi.local"

# re-run without new input: untouched
out=$(run 'mc_configure_access "$SCRATCH/.env"' 2>&1)
check "remote: a re-run keeps the earlier choice" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" "pi.local"
case "$out" in *"already configured: http://pi.local"*) ok "remote: re-run says so" ;;
  *) bad "remote: re-run says so" "$out" ;; esac

# ── badhost: garbage never reaches the .env ─────────────────────────────────
for h in "http://192.0.2.20" "192.0.2.20:80" "a b" "x/y" "-bad"; do
  fresh_env
  run "MC_REMOTE=yes MC_PUBLIC_HOST='$h' mc_configure_access \"\$SCRATCH/.env\"" >/dev/null 2>&1
  check "badhost: '$h' keeps local-only" \
    "$(run 'mc_env_get MC_BIND_ADDRESS "$SCRATCH/.env"')|$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" "127.0.0.1|"
done

# ── sshaddr: detection prefers the address the SSH session came in on ───────
check "sshaddr: server side of SSH_CONNECTION" \
  "$(run 'SSH_CONNECTION="198.51.100.7 51515 192.0.2.30 22"; ip() { echo "x src 192.0.2.99 uid 0"; }; mc_detect_address')" \
  "192.0.2.30"
check "sshaddr: loopback session falls back to the route source" \
  "$(run 'SSH_CONNECTION="127.0.0.1 1 127.0.0.1 22"; ip() { echo "1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.99 uid 0"; }; mc_detect_address')" \
  "192.0.2.99"
fresh_env
run 'SSH_CONNECTION="198.51.100.7 51515 192.0.2.30 22"; MC_REMOTE=yes mc_configure_access "$SCRATCH/.env"' >/dev/null 2>&1
check "sshaddr: --remote without an address uses the detected one" \
  "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" "192.0.2.30"

# ── headless detection + the interactive default ────────────────────────────
check "headless: SSH session" "$(run 'uname() { echo Darwin; }; SSH_CONNECTION="198.51.100.7 1 192.0.2.30 22"; mc_is_headless && echo y || echo n')" "y"
check "headless: Linux without a display" "$(run 'uname() { echo Linux; }; mc_is_headless && echo y || echo n')" "y"
check "headless: Linux desktop is not" "$(run 'uname() { echo Linux; }; DISPLAY=:0; mc_is_headless && echo y || echo n')" "n"
check "headless: local Mac is not" "$(run 'uname() { echo Darwin; }; mc_is_headless && echo y || echo n')" "n"

# Interactive: the prompt is stubbed to accept its default, so the defaults
# the user would see are what gets tested.
fresh_env
out=$(run 'uname() { echo Linux; }; SSH_CONNECTION="198.51.100.7 1 192.0.2.30 22"; MC_INTERACTIVE=1
  mc_ask() { echo "ASK[$1][$2]"; REPLY="$2"; }; mc_configure_access "$SCRATCH/.env"' 2>&1)
case "$out" in *"ASK[Open Mission Control from another device"*"][y]"*) ok "interactive: headless defaults to yes" ;;
  *) bad "interactive: headless defaults to yes" "$out" ;; esac
case "$out" in *"][192.0.2.30]"*) ok "interactive: address prompt pre-filled with the detected one" ;;
  *) bad "interactive: address prompt pre-filled with the detected one" "$out" ;; esac
check "interactive: accepted defaults open MC on that address" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/.env"')" "192.0.2.30"

fresh_env
out=$(run 'uname() { echo Darwin; }; MC_INTERACTIVE=1
  mc_ask() { echo "ASK[$1][$2]"; REPLY="$2"; }; mc_configure_access "$SCRATCH/.env"' 2>&1)
case "$out" in *"][n]"*) ok "interactive: a desktop Mac defaults to no" ;;
  *) bad "interactive: a desktop Mac defaults to no" "$out" ;; esac
check "interactive: desktop default stays local-only" "$(run 'mc_env_get MC_BIND_ADDRESS "$SCRATCH/.env"')" "127.0.0.1"

# ── strict mode: install.sh runs `set -euo pipefail` — no path may abort it ─
for mode in "" "MC_REMOTE=yes MC_PUBLIC_HOST=192.0.2.20" "MC_REMOTE=yes MC_PUBLIC_HOST=bad/host"; do
  fresh_env
  out=$(
    set -euo pipefail
    unset SSH_CONNECTION SSH_CLIENT SSH_TTY DISPLAY WAYLAND_DISPLAY MC_INTERACTIVE MC_REMOTE MC_PUBLIC_HOST
    # shellcheck source=scripts/lib/remote-access.sh
    . "$LIB"
    uname() { echo Linux; }
    eval "$mode mc_configure_access \"\$SCRATCH/.env\""
    mc_access_url "$SCRATCH/.env" >/dev/null
    echo SURVIVED
  ) 2>&1
  case "$out" in *SURVIVED*) ok "strict mode survives: '${mode:-no choice}'" ;;
    *) bad "strict mode survives: '${mode:-no choice}'" "$out" ;; esac
done

# ── install.sh wiring: asked BEFORE the first start, URL from the .env ──────
INSTALL="$HERE/../../install.sh"
ask_line=$(grep -n 'mc_configure_access .env' "$INSTALL" | head -n 1 | cut -d: -f1)
up_line=$(grep -n 'docker compose up -d$' "$INSTALL" | tail -n 1 | cut -d: -f1)
if [ -n "$ask_line" ] && [ -n "$up_line" ] && [ "$ask_line" -lt "$up_line" ]; then
  ok "install.sh: remote question comes before the first start"
else
  bad "install.sh: remote question comes before the first start" "ask=$ask_line up=$up_line"
fi
if grep -q 'Open \$MC_URL and register' "$INSTALL" && ! grep -q 'Open http://localhost and register' "$INSTALL"; then
  ok "install.sh: final message prints the computed URL"
else
  bad "install.sh: final message prints the computed URL"
fi

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
