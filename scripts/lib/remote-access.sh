# shellcheck shell=bash
# Mission Control — "will you open MC from another device?" (install.sh,
# setup.sh, scripts/open-network.sh)
#
# Why this exists (fresh install on a headless board, 17.10.2026): MC binds to
# 127.0.0.1 and PUBLIC_HOST is empty out of the box. On a server without a
# browser, "open http://localhost" cannot work, and the remote path was only
# explained later, in the README's phone section.
#
# The rule this file enforces: the network is opened ONLY AFTER the first
# admin exists. Before that, anyone on the LAN could call /auth/register and
# become admin — and an admin drives agents with shell and Docker access. So:
#   1. before the first start: decide (ask, or detect a headless box) and
#      remember the address — nothing is written yet (mc_remote_decide);
#   2. the stack starts on 127.0.0.1;
#   3. the first admin is registered from the terminal, over localhost
#      (mc_register_admin_tty — password hidden, sent on stdin, never in argv);
#   4. only then MC_BIND_ADDRESS/PUBLIC_HOST are written and the stack is
#      re-upped (mc_open_network, which refuses while no admin exists).
# Without a terminal nothing is opened; the switch command is printed instead.
#
# Sourced, never executed. bash 3.2 compatible (macOS /bin/bash). Every
# function that touches the .env takes its path as the last argument
# (default ./.env) so the tests can point it at a scratch file.
#
# Inputs (all optional):
#   MC_INTERACTIVE=1       may prompt via /dev/tty
#   MC_REMOTE=yes|no       answer the question without a prompt (--remote/--local)
#   MC_PUBLIC_HOST=<addr>  address other devices use (implies MC_REMOTE=yes)
#   MC_API_URL             backend for the register/admin check
#                          (default http://localhost:8000 — never the LAN)

# mc_env_get KEY [file] — value of KEY in the .env file ("" when absent).
mc_env_get() {
  local key="$1" file="${2:-.env}" line
  [ -f "$file" ] || return 0
  line=$(grep "^${key}=" "$file" 2>/dev/null | tail -n 1) || true
  printf '%s' "${line#*=}"
}

# mc_env_set KEY VALUE [file] — replace-or-append without sed (a value with
# sed metacharacters must not corrupt the file). Keeps the line's position
# and the file's inode (`cat >`, not `mv`): the backend bind-mounts .env.
mc_env_set() {
  local key="$1" value="$2" file="${3:-.env}" tmp found=0 line
  tmp="${file}.tmp.$$"
  : >"$tmp"
  if [ -f "$file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "${key}="*)
          if [ "$found" = 0 ]; then printf '%s=%s\n' "$key" "$value" >>"$tmp"; found=1; fi ;;
        *) printf '%s\n' "$line" >>"$tmp" ;;
      esac
    done <"$file"
  fi
  [ "$found" = 1 ] || printf '%s=%s\n' "$key" "$value" >>"$tmp"
  cat "$tmp" >"$file" && rm -f "$tmp"
}

# mc_env_unset KEY [file] — drop every KEY= line, same in-place write.
mc_env_unset() {
  local key="$1" file="${2:-.env}" tmp line
  [ -f "$file" ] || return 0
  tmp="${file}.tmp.$$"
  : >"$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in "${key}="*) ;; *) printf '%s\n' "$line" >>"$tmp" ;; esac
  done <"$file"
  cat "$tmp" >"$file" && rm -f "$tmp"
}

# mc_is_headless — 0 when nobody can open a browser ON this machine: an SSH
# session, or Linux without a graphical session.
mc_is_headless() {
  [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ] && return 0
  if [ "$(uname -s)" = Linux ] && [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    return 0
  fi
  return 1
}

_mc_is_loopback() {
  case "$1" in ""|127.*|localhost|::1) return 0 ;; esac
  return 1
}

# mc_detect_address — the best guess for the address other devices use.
# First choice: the address the current SSH session came in on (the user
# just proved it is reachable). Then the source address of the default route
# (Linux), then the first Wi-Fi/Ethernet address (macOS). Prints "" if none.
mc_detect_address() {
  local addr=""
  if [ -n "${SSH_CONNECTION:-}" ]; then
    # "client_ip client_port server_ip server_port"
    # shellcheck disable=SC2086
    set -- $SSH_CONNECTION
    addr="${3:-}"
    _mc_is_loopback "$addr" && addr=""
    case "$addr" in *:*) addr="" ;; esac   # IPv6: not usable as a bare URL host
  fi
  if [ -z "$addr" ] && command -v ip >/dev/null 2>&1; then
    addr=$(ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
  fi
  if [ -z "$addr" ] && command -v hostname >/dev/null 2>&1 && [ "$(uname -s)" = Linux ]; then
    addr=$(hostname -I 2>/dev/null | awk '{print $1}')
  fi
  if [ -z "$addr" ] && command -v ipconfig >/dev/null 2>&1; then
    addr=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  fi
  _mc_is_loopback "$addr" && addr=""
  printf '%s' "$addr"
}

# mc_valid_host HOST — a bare IPv4 address or host name, nothing else (no
# scheme, port, path or whitespace: it ends up in URLs and CORS origins).
mc_valid_host() {
  printf '%s' "$1" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
}

# mc_access_url [file] — the URL to open MC with, from the .env as written.
mc_access_url() {
  local file="${1:-.env}" bind host
  bind=$(mc_env_get MC_BIND_ADDRESS "$file")
  host=$(mc_env_get PUBLIC_HOST "$file")
  if ! _mc_is_loopback "$bind" && [ -n "$host" ]; then
    printf 'http://%s' "$host"
  else
    printf 'http://localhost'
  fi
}

mc_print_tunnel_hint() {
  local addr user
  addr=$(mc_detect_address)
  user="${USER:-$(id -un 2>/dev/null || echo user)}"
  cat <<EOF

  ℹ  This machine has no browser of its own (SSH session / no desktop), and
     Mission Control only listens on localhost. To open it from your laptop
     without exposing it to the network, use an SSH tunnel:
       ssh -L 8080:localhost:80 ${user}@${addr:-<this-machine>}
     then open http://localhost:8080 on the laptop.
EOF
}

mc_print_open_command() {
  local host="${1:-<address>}"
  cat <<EOF

  ℹ  To open Mission Control to your network (laptop, phone): first register
     the admin (via the SSH tunnel above, or with a terminal), then run in
     this directory:
       bash scripts/open-network.sh ${host}
     It refuses while no admin exists — before that, anyone on the network
     could claim the admin account.
EOF
}

mc_print_remote_note() {
  local host="$1"
  cat <<EOF

  ⚠  Mission Control is now reachable from every device on this network:
     http://$host
     - Plain HTTP, no encryption: passwords cross the network readable.
     Better way: Tailscale (private and encrypted, also from outside home).
       Install it on this machine and your devices, then:
         sudo tailscale serve --bg 80     → https://<machine>.<tailnet>.ts.net
       With Tailscale MC can stay local-only (MC_BIND_ADDRESS=127.0.0.1);
       put that name into PUBLIC_HOST.
     Back to local-only: MC_BIND_ADDRESS=127.0.0.1 in .env, then
       docker compose up -d
EOF
}

# mc_ask QUESTION DEFAULT — sets REPLY; prompts via /dev/tty only when
# MC_INTERACTIVE=1 (curl | bash has no usable stdin).
mc_ask() {
  local q="$1" d="$2"
  if [ "${MC_INTERACTIVE:-0}" = 1 ]; then
    printf '%s [%s]: ' "$q" "$d" >/dev/tty
    IFS= read -r REPLY </dev/tty || REPLY=""
    [ -n "$REPLY" ] || REPLY="$d"
  else
    REPLY="$d"
  fi
}

# mc_ask_secret QUESTION — sets REPLY without echo (never in scrollback).
mc_ask_secret() {
  printf '%s: ' "$1" >/dev/tty
  stty -echo </dev/tty 2>/dev/null || true
  IFS= read -r REPLY </dev/tty || REPLY=""
  stty echo </dev/tty 2>/dev/null || true
  printf '\n' >/dev/tty
}

# mc_remote_decide [file] — step 1, BEFORE the first start. Sets
# MC_REMOTE_HOST to the address to open later, or "" (stay local). Writes
# nothing. Never fails: an unusable address means "stay local".
mc_remote_decide() {
  local file="${1:-.env}" choice detected default
  MC_REMOTE_HOST=""
  choice="${MC_REMOTE:-}"
  [ -n "${MC_PUBLIC_HOST:-}" ] && [ -z "$choice" ] && choice=yes

  # Already opened on an earlier run and nothing new asked: leave it alone.
  if [ -z "$choice" ] && ! _mc_is_loopback "$(mc_env_get MC_BIND_ADDRESS "$file")" \
    && [ -n "$(mc_env_get PUBLIC_HOST "$file")" ]; then
    echo "✓ remote access already configured: $(mc_access_url "$file")"
    return 0
  fi

  if [ -z "$choice" ]; then
    if [ "${MC_INTERACTIVE:-0}" = 1 ]; then
      default=n
      if mc_is_headless; then
        default=y
        echo ""
        echo "This machine looks headless (SSH session or no desktop) — you will"
        echo "probably open Mission Control from another device."
      fi
      mc_ask "Open Mission Control from another device (laptop, phone)? y/n" "$default"
      choice="$REPLY"
    else
      choice=no
    fi
  fi

  case "$choice" in
    y|Y|yes|Yes|YES) ;;
    *) return 0 ;;
  esac

  MC_REMOTE_HOST="${MC_PUBLIC_HOST:-}"
  if [ -z "$MC_REMOTE_HOST" ]; then
    detected=$(mc_detect_address)
    mc_ask "Address other devices use to reach this machine (IP or name.local)" "$detected"
    MC_REMOTE_HOST="$REPLY"
  fi
  if ! mc_valid_host "$MC_REMOTE_HOST"; then
    echo "✗ '${MC_REMOTE_HOST}' is not a usable address (expected a bare IP address or name.local, no http:// or port)."
    echo "  Staying local-only."
    MC_REMOTE_HOST=""
    return 0
  fi
  echo "✓ will open MC on http://$MC_REMOTE_HOST — after the first admin is registered (next steps)"
}

_mc_api() { printf '%s' "${MC_API_URL:-http://localhost:8000}"; }

# mc_admin_exists — 0 when the first admin is registered (the backend says
# setup is no longer required). Unknown (backend down) counts as "no".
mc_admin_exists() {
  local body
  body=$(curl -sf --max-time 10 "$(_mc_api)/api/v1/auth/setup-required" 2>/dev/null) || return 1
  case "$body" in *'"setup_required":false'*|*'"setup_required": false'*) return 0 ;; esac
  return 1
}

# JSON string escape for the register body: backslash and quote escaped,
# control characters refused (they have no place in a name or password).
_mc_json_str() {
  local s="$1"
  case "$s" in *[[:cntrl:]]*) return 1 ;; esac
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

# mc_register_admin_tty [file] — step 3: create the first admin from the terminal,
# over localhost only. The body goes to curl on stdin (printf is a builtin:
# the password is never in a process argument, the environment, a log or the
# shell history). Returns 0 once an admin exists.
mc_register_admin_tty() {
  local name email pw pw2 jn je jp code tries=0
  [ "${MC_INTERACTIVE:-0}" = 1 ] || return 1
  echo ""
  echo "Create the first admin now (it stays on this machine — MC is not on the"
  echo "network yet):"
  name=$(mc_env_get OPERATOR_NAME "${1:-.env}"); mc_ask "Admin name" "${name:-Operator}"; name="$REPLY"
  while :; do
    mc_ask "Admin email" ""; email="$REPLY"
    case "$email" in *@*.*) break ;; esac
    echo "  please enter an email address"
  done
  while :; do
    tries=$((tries + 1))
    [ "$tries" -gt 3 ] && { echo "✗ no matching password after 3 tries."; return 1; }
    mc_ask_secret "Admin password (min. 6 characters, hidden)"; pw="$REPLY"
    [ "${#pw}" -ge 6 ] || { echo "  too short"; continue; }
    mc_ask_secret "Repeat the password"; pw2="$REPLY"
    [ "$pw" = "$pw2" ] && break
    echo "  the passwords differ"
  done
  if ! { jn=$(_mc_json_str "$name") && je=$(_mc_json_str "$email") && jp=$(_mc_json_str "$pw"); }; then
    echo "✗ control characters are not allowed."; return 1
  fi
  code=$(printf '{"name":%s,"email":%s,"password":%s}' "$jn" "$je" "$jp" \
    | curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        -H 'Content-Type: application/json' --data-binary @- \
        "$(_mc_api)/api/v1/auth/register" 2>/dev/null) || code=000
  pw=""; pw2=""; jp=""
  if [ "$code" = 200 ]; then
    echo "✓ admin '$email' created — log in with it."
    return 0
  fi
  echo "✗ registration failed (HTTP $code) — register in the browser via the SSH tunnel instead."
  return 1
}

# mc_open_network HOST [file] — step 4: write the binding and re-up the
# stack. Refuses while no admin exists. Uses the plain project of this
# directory (same as the install's own `docker compose up`).
mc_open_network() {
  local host="$1" file="${2:-.env}"
  if ! mc_valid_host "$host"; then
    echo "✗ '${host}' is not a usable address."; return 1
  fi
  if ! mc_admin_exists; then
    echo "✗ no admin is registered yet — refusing to open MC to the network."
    echo "  Register first (http://localhost, or the SSH tunnel), then run this again."
    return 1
  fi
  mc_env_set MC_BIND_ADDRESS 0.0.0.0 "$file"
  mc_env_set PUBLIC_HOST "$host" "$file"
  echo "→ applying: Caddy listens on the network, PUBLIC_HOST=$host ..."
  docker compose up -d >/dev/null || { echo "✗ docker compose up -d failed — run it by hand."; return 1; }
  echo "✓ remote access: http://$host"
  mc_print_remote_note "$host"
}

# mc_remote_finish HOST [file] — after the stack is healthy: register the
# admin if needed (terminal only), then open. Without a terminal: print the
# command. Never fails the install.
mc_remote_finish() {
  local host="$1" file="${2:-.env}"
  [ -n "$host" ] || { mc_is_headless && { mc_print_tunnel_hint; mc_print_open_command; }; return 0; }
  if ! mc_admin_exists; then
    if ! mc_register_admin_tty "$file"; then
      mc_print_tunnel_hint
      mc_print_open_command "$host"
      return 0
    fi
  fi
  mc_open_network "$host" "$file" || { mc_print_open_command "$host"; return 0; }
}
