# Reverse proxy & remote access

Mission Control ships its own Caddy. Everything — UI, API, SSE streams,
terminal WebSockets — enters through it on one port. This page covers how that
front door works, how to open it to other devices, and how to put your own
proxy in front of it.

## The built-in Caddy

`Caddyfile` in the repo root is mounted read-only into the `caddy` service.
It listens on `:80` and routes by path:

| Path | Target | Notes |
|---|---|---|
| `/api/*` | `backend:8000` | REST, SSE streams, terminal WebSockets |
| `/health` | `backend:8000` | liveness check |
| `/livekit-signal`, `/livekit-signal/*` | `livekit:7880` | WebSocket, prefix stripped; voice profile only |
| `/rtc`, `/rtc/*` | `livekit:7880` | LiveKit signaling |
| everything else | `frontend:3000` | Next.js |

It also sets `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, removes the `Server`
header and enables gzip/zstd.

**The browser must enter through Caddy.** The prebuilt frontend image makes
relative, same-origin API calls — exposing `frontend:3000` directly gives you
a UI that loads and then fails every request.

## Ports and binding

Every published port binds to `127.0.0.1` by default:

| Service | Published port | Bind |
|---|---|---|
| caddy | 80, 443 | `${MC_BIND_ADDRESS:-127.0.0.1}` |
| backend | 8000 | `127.0.0.1` |
| frontend | 3000 | `127.0.0.1` |
| db / redis / qdrant | 5432 / 6379 / 6333 | `127.0.0.1` |
| livekit (voice profile) | 7880 | `127.0.0.1` |
| livekit RTC media | 7881/tcp, 7882/udp | **all interfaces** — needed for WebRTC media |

Only Caddy's binding is configurable, via `MC_BIND_ADDRESS`:

```bash
# .env
MC_BIND_ADDRESS=0.0.0.0
```

```bash
docker compose up -d caddy
```

This was a breaking change in the current release — MC used to bind to all
interfaces. Do the opt-in **after** you registered the first admin account:
`POST /api/v1/auth/register` succeeds while no user with a password exists, so
on an open network anyone could otherwise claim the admin account.

## CORS: `PUBLIC_HOST`

The backend's CORS allowlist is built at startup. It always contains
`http://localhost`, `http://localhost:80`, `http://localhost:3000` (plus
preview ports 3001/3002), `http://frontend:3000` and `https://mc.local`.
Reaching MC under any other name needs:

```bash
# .env
PUBLIC_HOST=your-machine.tailnet-name.ts.net   # adds http://…, http://…:80, https://…
EXTRA_CORS_ORIGINS=https://mc.example.com,http://192.0.2.50   # comma-separated extras
```

`PUBLIC_HOST` is also used as the base for notification links. Both are read
at boot — restart the backend after changing them:

```bash
docker compose up -d caddy backend
```

## Recommended remote path: Tailscale

This keeps MC off the public internet entirely, which is how it is meant to
run. From the README's "Access from your phone, anywhere":

1. Install [Tailscale](https://tailscale.com) on the MC host and on your phone
   (same account).
2. In `.env`:
   ```bash
   MC_BIND_ADDRESS=0.0.0.0
   PUBLIC_HOST=your-machine.tailnet-name.ts.net
   ```
3. `docker compose up -d caddy backend`
4. Open `http://your-machine.tailnet-name.ts.net` on the phone.

For voice over the tailnet, also set `LIVEKIT_NODE_IP` to the host's tailnet
IP — LiveKit advertises that address as its RTC candidate and defaults to
`127.0.0.1` (local-only).

## HTTPS with your own certificate

The shipped Caddyfile is HTTP-only; `localhost` is a secure context for
browser WebRTC, so plain HTTP is fine locally. For TLS elsewhere:

```bash
cp caddy/Caddyfile.tls.example caddy/Caddyfile.local
tailscale cert your-machine.tailnet-name.ts.net    # or bring your own cert
# put <hostname>.crt / <hostname>.key into caddy/certs/   (gitignored)
```

Replace `<your-hostname>` in `Caddyfile.local`, then mount it over the default
via `docker-compose.override.yml` (see
[`docker-compose.override.example.yml`](../../../docker-compose.override.example.yml)):

```yaml
services:
  caddy:
    volumes:
      - ./caddy/Caddyfile.local:/etc/caddy/Caddyfile:ro
      - ./caddy/certs:/etc/caddy/certs:ro
      - caddy_data:/data
      - caddy_config:/config
```

The TLS example keeps the `:80` block for local access and adds an HTTPS site
with `Strict-Transport-Security` and `X-Frame-Options: DENY`. Never commit
certificates.

## Putting MC behind your own reverse proxy

Forward to **Caddy's published port** (80 by default), not to the frontend or
backend directly — path routing and same-origin behaviour depend on Caddy.
Set `MC_BIND_ADDRESS` so your proxy can reach it (`0.0.0.0`, or the specific
interface your proxy uses), and add the external name to `PUBLIC_HOST` /
`EXTRA_CORS_ORIGINS`.

Two things break under a naively configured proxy:

**Server-Sent Events.** The UI holds long-lived `text/event-stream`
connections open — `/api/v1/activity/stream`, `/api/v1/agents/stream`,
`/api/v1/approvals/stream`, `/api/v1/boards/{id}/tasks/stream`,
`/api/v1/boards/{id}/memory/stream`, `/api/v1/schedule/stream`,
`/api/v1/workflows/stream`, `/api/v1/meetings/stream`. Response buffering must
be off and read timeouts long, or the live board simply stops updating.

nginx:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:80;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 24h;
}
```

**WebSockets.** The live agent terminals and the browser-live view upgrade to
WebSocket: `/api/v1/agents/{id}/terminal`, `/api/v1/agents/{id}/terminal/ws`,
`/api/v1/agents/{id}/terminal/{task_id}/ws`,
`/api/v1/host-agents/{id}/terminal`, `/api/v1/browser-live/ws`. With the voice
profile enabled, `/livekit-signal*` and `/rtc*` too. Pass the upgrade headers:

```nginx
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
```

Since both SSE and WebSocket endpoints live under `/api/`, one `location /`
block with buffering off and upgrade headers set covers everything.

Voice is the exception that cannot be proxied over HTTP alone: LiveKit's RTC
media uses ports 7881/tcp and 7882/udp directly on the host.

## Security reminder

Do not expose Mission Control to the public internet. The backend can control
Docker containers (through a filtering socket proxy, ADR-047) and agents run
real shells — run it on hosts you trust, behind a VPN or tailnet. See the
README's security notes and [SECURITY.md](../../../SECURITY.md).
