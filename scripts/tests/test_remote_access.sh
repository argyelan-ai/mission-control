#!/usr/bin/env bash
# Behaviour tests for scripts/lib/remote-access.sh (install.sh, setup.sh,
# scripts/open-network.sh).
#
# Finding 17.10.2026: a fresh install on a headless board listened on
# 127.0.0.1 with an empty PUBLIC_HOST, and the installer said "open
# http://localhost" — impossible without a browser. Review finding on the
# first fix: opening the LAN BEFORE the first admin exists lets anyone on
# the network register as admin. These tests drive the REAL library against
# a scratch .env; only the environment (SSH_CONNECTION, DISPLAY, uname, ip),
# the prompts, curl (the backend) and docker are stubbed.
#
# Sabotage probes (undo ONE line in remote-access.sh, its tests go red):
#   admin-gate : `if ! mc_admin_exists; then` in mc_open_network → `if false; then`
#   decide     : mc_remote_decide writing MC_BIND_ADDRESS (nothing may be
#                written before the admin exists)
#   ci-safe    : `choice=no` in the non-interactive branch → `choice=yes`
#   argv       : the register body passed as `-d "$body"` instead of stdin
#   sshaddr    : `addr="${3:-}"` → `addr="${1:-}"` (the CLIENT address)
#   badhost    : the `mc_valid_host` check in mc_remote_decide removed
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
export SCRATCH

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check() { # check NAME ACTUAL EXPECTED
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$3', got '$2'"; fi
}
has() { # has NAME HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "missing '$3' in: $2" ;; esac
}
hasnt() {
  case "$2" in *"$3"*) bad "$1" "unexpected '$3' in: $2" ;; *) ok "$1" ;; esac
}

fresh_env() { cp "$EXAMPLE" "$SCRATCH/.env"; rm -f "$SCRATCH"/curl.* "$SCRATCH"/docker.* "$SCRATCH/admin.flag"; }

# Stubs, defined inside every run:
#   ADMIN=yes|no      what GET /auth/setup-required reports
#   REG_CODE=200|…    what POST /auth/register answers (and flips ADMIN state)
#   curl.args / curl.stdin record every call; docker.args records docker.
STUBS='
curl() {
  printf "%s\n" "$*" >>"$SCRATCH/curl.args"
  case "$*" in
    *auth/register*)
      cat >>"$SCRATCH/curl.stdin"
      [ "${REG_CODE:-200}" = 200 ] && : >"$SCRATCH/admin.flag"
      printf "%s" "${REG_CODE:-200}" ;;
    *setup-required*)
      if [ "${ADMIN:-no}" = yes ] || [ -f "$SCRATCH/admin.flag" ]; then
        printf "{\"setup_required\":false}"
      else
        printf "{\"setup_required\":true}"
      fi ;;
    *) return 7 ;;
  esac
}
docker() { printf "%s\n" "$*" >>"$SCRATCH/docker.args"; }
'

run() { # run "<shell code>" — subshell, clean env, lib + stubs loaded
  (
    unset SSH_CONNECTION SSH_CLIENT SSH_TTY DISPLAY WAYLAND_DISPLAY \
      MC_INTERACTIVE MC_REMOTE MC_PUBLIC_HOST MC_REMOTE_HOST
    # shellcheck source=scripts/lib/remote-access.sh
    . "$LIB"
    eval "$STUBS"
    eval "$1"
  )
}
envget() { run "mc_env_get $1 \"\$SCRATCH/.env\""; }

echo "remote-access.sh"

# ── .env helpers ─────────────────────────────────────────────────────────────
fresh_env
check "example ships local-only" "$(envget MC_BIND_ADDRESS)" "127.0.0.1"
check "example ships an empty PUBLIC_HOST" "$(envget PUBLIC_HOST)" ""
printf 'A=1\nPUBLIC_HOST=\nB=2\n' >"$SCRATCH/kv"
inode_before=$(ls -i "$SCRATCH/kv" | awk '{print $1}')
run 'mc_env_set PUBLIC_HOST "a&b\\c|d" "$SCRATCH/kv"'
check "env_set keeps metacharacters literal" "$(run 'mc_env_get PUBLIC_HOST "$SCRATCH/kv"')" 'a&b\c|d'
check "env_set replaces in place" "$(sed -n 2p "$SCRATCH/kv")" 'PUBLIC_HOST=a&b\c|d'
check "env_set keeps the other lines" "$(sed -n 1p "$SCRATCH/kv")$(sed -n 3p "$SCRATCH/kv")" "A=1B=2"
check "env_set keeps the inode (bind-mounted .env)" "$(ls -i "$SCRATCH/kv" | awk '{print $1}')" "$inode_before"
run 'mc_env_set NEW_KEY x "$SCRATCH/kv"'
check "env_set appends a missing key" "$(tail -n 1 "$SCRATCH/kv")" "NEW_KEY=x"

# ── decide: before the first start nothing is written ───────────────────────
fresh_env
out=$(run 'MC_REMOTE=yes MC_PUBLIC_HOST=192.0.2.20 mc_remote_decide "$SCRATCH/.env"; echo "HOST=$MC_REMOTE_HOST"' 2>&1)
has "decide: remembers the address" "$out" "HOST=192.0.2.20"
check "decide: binding untouched before the admin exists" "$(envget MC_BIND_ADDRESS)" "127.0.0.1"
check "decide: PUBLIC_HOST untouched before the admin exists" "$(envget PUBLIC_HOST)" ""

fresh_env
out=$(run 'uname() { echo Linux; }; mc_remote_decide "$SCRATCH/.env"; echo "HOST=[$MC_REMOTE_HOST]"' 2>&1)
has "ci-safe: unattended run decides local-only" "$out" "HOST=[]"

fresh_env
out=$(run 'MC_PUBLIC_HOST=pi.local mc_remote_decide "$SCRATCH/.env"; echo "HOST=$MC_REMOTE_HOST"' 2>&1)
has "decide: a host name alone implies yes" "$out" "HOST=pi.local"

for h in "http://192.0.2.20" "192.0.2.20:80" "a b" "x/y" "-bad"; do
  out=$(run "MC_REMOTE=yes MC_PUBLIC_HOST='$h' mc_remote_decide \"\$SCRATCH/.env\"; echo \"HOST=[\$MC_REMOTE_HOST]\"" 2>&1)
  has "badhost: '$h' is refused" "$out" "HOST=[]"
done

# Interactive defaults (prompt stubbed to accept the offered default).
out=$(run 'uname() { echo Linux; }; SSH_CONNECTION="198.51.100.7 1 192.0.2.30 22"; MC_INTERACTIVE=1
  mc_ask() { echo "ASK[$1][$2]"; REPLY="$2"; }; mc_remote_decide "$SCRATCH/.env"; echo "HOST=$MC_REMOTE_HOST"' 2>&1)
has "interactive: headless defaults to yes" "$out" "another device (laptop, phone)? y/n][y]"
has "interactive: address prompt pre-filled with the detected one" "$out" "][192.0.2.30]"
has "interactive: accepted defaults pick that address" "$out" "HOST=192.0.2.30"
out=$(run 'uname() { echo Darwin; }; MC_INTERACTIVE=1
  mc_ask() { echo "ASK[$1][$2]"; REPLY="$2"; }; mc_remote_decide "$SCRATCH/.env"; echo "HOST=[$MC_REMOTE_HOST]"' 2>&1)
has "interactive: a desktop Mac defaults to no" "$out" "][n]"
has "interactive: desktop default stays local" "$out" "HOST=[]"

# ── admin gate: the network never opens while no admin exists ──────────────
fresh_env
out=$(run 'ADMIN=no mc_open_network 192.0.2.20 "$SCRATCH/.env"; echo "rc=$?"' 2>&1)
has "admin-gate: open_network refuses without an admin" "$out" "refusing to open MC"
has "admin-gate: and says so with a non-zero status" "$out" "rc=1"
check "admin-gate: binding stays 127.0.0.1" "$(envget MC_BIND_ADDRESS)" "127.0.0.1"
check "admin-gate: no docker call" "$(cat "$SCRATCH/docker.args" 2>/dev/null)" ""

fresh_env
out=$(run 'ADMIN=yes mc_open_network 192.0.2.20 "$SCRATCH/.env"' 2>&1)
check "open: binds to all interfaces once an admin exists" "$(envget MC_BIND_ADDRESS)" "0.0.0.0"
check "open: PUBLIC_HOST is the address" "$(envget PUBLIC_HOST)" "192.0.2.20"
check "open: re-ups the stack (same project, this directory)" "$(cat "$SCRATCH/docker.args")" "compose up -d"
check "url: install.sh prints the remote URL" "$(run 'mc_access_url "$SCRATCH/.env"')" "http://192.0.2.20"
has "open: plain-HTTP note + Tailscale tip" "$out" "tailscale serve"
has "open: admin check went to localhost only" "$(cat "$SCRATCH/curl.args")" "http://localhost:8000/api/v1/auth/setup-required"

# ── finish without a terminal: never open, print the command ───────────────
fresh_env
out=$(run 'uname() { echo Linux; }; ADMIN=no mc_remote_finish 192.0.2.20 "$SCRATCH/.env"; echo "rc=$?"' 2>&1)
has "no-tty: finish never fails the install" "$out" "rc=0"
has "no-tty: prints the switch command" "$out" "bash scripts/open-network.sh 192.0.2.20"
has "no-tty: and the SSH tunnel" "$out" "ssh -L 8080:localhost:80"
check "no-tty: binding stays 127.0.0.1" "$(envget MC_BIND_ADDRESS)" "127.0.0.1"
check "no-tty: nothing registered" "$(grep -c register "$SCRATCH/curl.args" 2>/dev/null || true)" "0"

# ── register from the terminal, then open ──────────────────────────────────
PW='s3cr3t"pw\x'
fresh_env
out=$(run 'MC_INTERACTIVE=1
  mc_ask() { case "$1" in *name*) REPLY="Op \"One\"";; *email*) REPLY="op@example.com";; *) REPLY="$2";; esac; }
  mc_ask_secret() { REPLY='"'$PW'"'; }
  ADMIN=no mc_remote_finish 192.0.2.20 "$SCRATCH/.env"' 2>&1)
body=$(cat "$SCRATCH/curl.stdin" 2>/dev/null)
check "register: JSON body on stdin, quotes and backslashes escaped" "$body" \
  '{"name":"Op \"One\"","email":"op@example.com","password":"s3cr3t\"pw\\x"}'
hasnt "argv: the password is never a curl argument" "$(cat "$SCRATCH/curl.args")" "s3cr3t"
hasnt "argv: the password is never printed" "$out" "s3cr3t"
has "register: body goes in via --data-binary @-" "$(cat "$SCRATCH/curl.args")" "--data-binary @- http://localhost:8000/api/v1/auth/register"
check "register → open: binding written after the admin exists" "$(envget MC_BIND_ADDRESS)" "0.0.0.0"
check "register → open: stack re-upped" "$(cat "$SCRATCH/docker.args")" "compose up -d"

fresh_env
out=$(run 'MC_INTERACTIVE=1
  mc_ask() { case "$1" in *email*) REPLY="op@example.com";; *) REPLY="$2";; esac; }
  mc_ask_secret() { REPLY="password1"; }
  REG_CODE=403 ADMIN=no mc_remote_finish 192.0.2.20 "$SCRATCH/.env"; echo "rc=$?"' 2>&1)
has "register failed: install goes on" "$out" "rc=0"
has "register failed: says so" "$out" "registration failed (HTTP 403)"
check "register failed: stays local" "$(envget MC_BIND_ADDRESS)" "127.0.0.1"

fresh_env
out=$(run 'MC_INTERACTIVE=1; n=0
  mc_ask() { case "$1" in *email*) REPLY="op@example.com";; *) REPLY="$2";; esac; }
  mc_ask_secret() { n=$((n + 1)); if [ $((n % 2)) = 1 ]; then REPLY="password1"; else REPLY="password2"; fi; }
  ADMIN=no mc_remote_finish 192.0.2.20 "$SCRATCH/.env"' 2>&1)
has "register: mismatching passwords give up after 3 tries" "$out" "no matching password after 3 tries"
check "register: nothing sent when the passwords differ" "$(cat "$SCRATCH/curl.stdin" 2>/dev/null)" ""

fresh_env
run 'ADMIN=yes mc_remote_finish 192.0.2.20 "$SCRATCH/.env"' >/dev/null 2>&1
check "existing admin (re-run): opens without asking" "$(envget PUBLIC_HOST)" "192.0.2.20"
out=$(run 'mc_remote_decide "$SCRATCH/.env"' 2>&1)
has "re-run: earlier choice is kept" "$out" "already configured: http://192.0.2.20"

# ── address detection ──────────────────────────────────────────────────────
check "sshaddr: server side of SSH_CONNECTION" \
  "$(run 'SSH_CONNECTION="198.51.100.7 51515 192.0.2.30 22"; ip() { echo "x src 192.0.2.99 uid 0"; }; mc_detect_address')" \
  "192.0.2.30"
check "sshaddr: loopback session falls back to the route source" \
  "$(run 'SSH_CONNECTION="127.0.0.1 1 127.0.0.1 22"; ip() { echo "1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.99 uid 0"; }; mc_detect_address')" \
  "192.0.2.99"
check "headless: SSH session" "$(run 'uname() { echo Darwin; }; SSH_CONNECTION="198.51.100.7 1 192.0.2.30 22"; mc_is_headless && echo y || echo n')" "y"
check "headless: Linux without a display" "$(run 'uname() { echo Linux; }; mc_is_headless && echo y || echo n')" "y"
check "headless: Linux desktop is not" "$(run 'uname() { echo Linux; }; DISPLAY=:0; mc_is_headless && echo y || echo n')" "n"
check "headless: local Mac is not" "$(run 'uname() { echo Darwin; }; mc_is_headless && echo y || echo n')" "n"

# ── strict mode: install.sh runs `set -euo pipefail` — no path may abort it ─
for mode in "ADMIN=no" "ADMIN=yes" "MC_PUBLIC_HOST=bad/host"; do
  fresh_env
  out=$(
    set -euo pipefail
    unset SSH_CONNECTION SSH_CLIENT SSH_TTY DISPLAY WAYLAND_DISPLAY MC_INTERACTIVE MC_REMOTE MC_PUBLIC_HOST
    # shellcheck source=scripts/lib/remote-access.sh
    . "$LIB"
    eval "$STUBS"
    uname() { echo Linux; }
    eval "export $mode"
    MC_REMOTE=yes mc_remote_decide "$SCRATCH/.env"
    mc_remote_finish "${MC_REMOTE_HOST:-192.0.2.20}" "$SCRATCH/.env"
    mc_access_url "$SCRATCH/.env" >/dev/null
    mc_admin_exists || true
    echo SURVIVED
  ) 2>&1
  has "strict mode survives: '$mode'" "$out" "SURVIVED"
done

# ── install.sh / setup.sh wiring ───────────────────────────────────────────
INSTALL="$HERE/../../install.sh"
SETUP="$HERE/../../setup.sh"
line() { grep -n "$1" "$2" | head -n 1 | cut -d: -f1; }
decide=$(line 'mc_remote_decide .env' "$INSTALL")
health=$(line '^mc_wait_healthy ||' "$INSTALL")   # the fresh install's health wait
finish=$(line 'mc_remote_finish "$MC_REMOTE_HOST"' "$INSTALL")
url=$(line 'MC_URL=$(mc_access_url' "$INSTALL")
if [ -n "$decide" ] && [ -n "$health" ] && [ -n "$finish" ] && [ -n "$url" ] \
  && [ "$decide" -lt "$health" ] && [ "$health" -lt "$finish" ] && [ "$finish" -lt "$url" ]; then
  ok "install.sh: decide → boot+health → register/open → URL"
else
  bad "install.sh: decide → boot+health → register/open → URL" "decide=$decide health=$health finish=$finish url=$url"
fi
if grep -q 'mc_env_set MC_BIND_ADDRESS' "$INSTALL" "$SETUP"; then
  bad "install.sh/setup.sh never write the binding themselves"
else
  ok "install.sh/setup.sh never write the binding themselves"
fi
if grep -q 'scripts/lib/remote-access.sh" -o' "$INSTALL" && grep -q '\[ ! -f "\$lib" \]' "$INSTALL"; then
  ok "install.sh: library fetched from main when the checkout lacks it"
else
  bad "install.sh: library fetched from main when the checkout lacks it"
fi
grep -q '! mc_is_headless; then' "$INSTALL" && ok "install.sh: no browser opened on a headless box" \
  || bad "install.sh: no browser opened on a headless box"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
