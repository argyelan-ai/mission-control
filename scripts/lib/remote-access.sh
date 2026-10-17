# shellcheck shell=bash
# Mission Control — "will you open MC from another device?" (install.sh, setup.sh)
#
# Why this exists (fresh install on a headless board, 17.10.2026): MC binds to
# 127.0.0.1 and PUBLIC_HOST is empty out of the box. On a server without a
# browser, "open http://localhost" cannot work, and the remote path was only
# explained later, in the README's phone section. This library asks the
# question BEFORE the first start (or detects the answer: an SSH session or a
# Linux box without a desktop), writes MC_BIND_ADDRESS + PUBLIC_HOST, prints
# what that means for security, and tells install.sh which URL to print.
#
# Sourced, never executed. bash 3.2 compatible (macOS /bin/bash): no
# associative arrays, no ${var,,}. Every function takes the .env path as its
# last argument (default ./.env) so the tests can point it at a scratch file.
#
# Inputs (all optional):
#   MC_INTERACTIVE=1       may prompt via /dev/tty (install.sh sets it)
#   MC_REMOTE=yes|no       answer the question without a prompt (--remote/--local)
#   MC_PUBLIC_HOST=<addr>  address other devices use (implies MC_REMOTE=yes)

# mc_env_get KEY [file] — value of KEY in the .env file ("" when absent).
mc_env_get() {
  local key="$1" file="${2:-.env}" line
  [ -f "$file" ] || return 0
  line=$(grep "^${key}=" "$file" 2>/dev/null | tail -n 1) || true
  printf '%s' "${line#*=}"
}

# mc_env_set KEY VALUE [file] — replace-or-append without sed (a value with
# sed metacharacters must not corrupt the file). Keeps the line's position.
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
  # cat > keeps the inode (and so any bind mount of .env stays valid).
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

# mc_enable_remote_access HOST [file]
mc_enable_remote_access() {
  local host="$1" file="${2:-.env}"
  mc_env_set MC_BIND_ADDRESS 0.0.0.0 "$file"
  mc_env_set PUBLIC_HOST "$host" "$file"
}

mc_print_remote_note() {
  local host="$1"
  cat <<EOF

  ⚠  Mission Control will be reachable from every device on this network:
     http://$host
     - Plain HTTP, no encryption: passwords cross the network readable.
     - Until the first admin is registered, ANYONE on this network can
       claim that account. Register right after the start.
     Better way: Tailscale (private and encrypted, also from outside home).
       Install it on this machine and your devices, then:
         sudo tailscale serve --bg 80     → https://<machine>.<tailnet>.ts.net
       With Tailscale MC can stay local-only (MC_BIND_ADDRESS=127.0.0.1);
       put that name into PUBLIC_HOST.
     Back to local-only: MC_BIND_ADDRESS=127.0.0.1 in .env, then
       docker compose up -d caddy
EOF
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
     To open it to your network instead: re-run with --remote
     (or set MC_BIND_ADDRESS=0.0.0.0 and PUBLIC_HOST=<address> in .env).
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

# mc_configure_access [file] — the whole step. Never fails the install: a
# missing or unusable address keeps the safe default (local-only).
mc_configure_access() {
  local file="${1:-.env}" choice host detected default
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
    *)
      mc_is_headless && mc_print_tunnel_hint
      return 0 ;;
  esac

  host="${MC_PUBLIC_HOST:-}"
  if [ -z "$host" ]; then
    detected=$(mc_detect_address)
    mc_ask "Address other devices use to reach this machine (IP or name.local)" "$detected"
    host="$REPLY"
  fi
  if ! mc_valid_host "$host"; then
    echo "✗ '${host}' is not a usable address (expected a bare IP address or name.local, no http:// or port)."
    echo "  Staying local-only."
    mc_print_tunnel_hint
    return 0
  fi
  mc_enable_remote_access "$host" "$file"
  echo "✓ remote access: MC_BIND_ADDRESS=0.0.0.0, PUBLIC_HOST=$host"
  mc_print_remote_note "$host"
}
