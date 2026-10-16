# CLI-Bridge auth: shared token + loopback bind

`scripts/cli-bridge.py` runs on the host, outside Docker. It starts agents,
installs plugins, rebuilds images and attaches terminals (WebSocket PTY).
Only Mission Control may talk to it. Two layers protect it:

| Layer | What | Default |
|---|---|---|
| **Shared token** (the real protection) | Every request needs the header `X-MC-Bridge-Token`. Missing or wrong → HTTP `401`, WebSocket closed with `1008`. Compared in constant time. | always on |
| **Bind address** (defence in depth) | `CLI_BRIDGE_BIND` — where HTTP `:18792` and WS `:18793` listen. | `127.0.0.1` |

The bind alone is not enough: any process on the host, and any container that
can resolve `host.docker.internal`, still reaches a loopback port. That is
why the token is required on every endpoint.

## Which endpoints are open

Only `GET /ping` answers without a token. It returns `{"status": "ok"}` and
nothing else — a port scan reveals the same. `GET /health` needs the token,
so MC's "helper reachable" checks also prove that the token matches.

## Where the token lives

| Side | Source (first match wins) |
|---|---|
| Helper | `CLI_BRIDGE_TOKEN` env → file `CLI_BRIDGE_TOKEN_FILE` → `~/.mc/secrets/cli-bridge.token` |
| Backend + worker | `CLI_BRIDGE_TOKEN` (`.env`) → `CLI_BRIDGE_TOKEN_FILE` → `$HOME_HOST/.mc/secrets/cli-bridge.token` |
| `scripts/mc-mcp.py` (operator mode only) | same as the helper |

- The helper creates the file on first start (`secrets.token_urlsafe(32)`,
  mode `0600`, directory created if missing). It never prints or logs the
  value. If the file exists with looser permissions, it is tightened to `0600`.
- The backend and worker containers bind-mount `~/.mc` 1:1, so on a standard
  install they read the **same file** — you do not need to copy the token
  into `.env`. The backend reads the file on every call, so a rotated token
  is picked up without restarting the backend.
- Agent containers do **not** mount `~/.mc/secrets`. Prefer the file over
  `.env` where you can: `.env` sits in the repo checkout, and that checkout
  may be mounted into agent containers.
- `CLI_BRIDGE_TOKEN` in `.env` is for setups where the container cannot read
  the host file (another user ID on Linux, the helper on a different machine).
  `docker-compose.yml` passes it to `backend` and `mc-worker`.

Create the file without starting the helper:

```bash
python3 scripts/cli-bridge.py --init-token     # prints the path, never the token
```

## Bind address per platform

**macOS (Docker Desktop):** keep the default `127.0.0.1`. Docker Desktop
delivers connections to `host.docker.internal` on the host's loopback (the
helper sees them coming from `127.0.0.1`), so the backend container still
reaches the helper, while other machines on the LAN do not.

**Linux:** `host.docker.internal` is mapped to `host-gateway` (see
`extra_hosts` in `docker-compose.yml`), which is the docker bridge address —
usually `172.17.0.1` (`docker network inspect bridge`). A helper on
`127.0.0.1` is not reachable from the container. Bind to the bridge address:

```bash
CLI_BRIDGE_BIND=172.17.0.1 make bridge-install
```

`0.0.0.0` also works but exposes the port to the network; if you need it, put
a host firewall in front. The token protects the helper in every case.

## Rollout on an existing install (no downtime)

Backend and helper are updated separately. Order matters: **backend first.**

1. Create the token file on the host:
   `python3 scripts/cli-bridge.py --init-token`
2. Deploy the new backend + worker. They read the file and send the header.
   The old helper ignores the header, so nothing breaks in between.
3. Check that the backend sees the file (prints `True`, not the token):
   `docker compose exec backend python -c "from app.services.cli_bridge_auth import bridge_token; print(bool(bridge_token()))"`
   If it prints `False`, set `CLI_BRIDGE_TOKEN` in `.env` (value from the
   file) and recreate `backend` + `mc-worker`.
4. Restart the helper with the new code (`make bridge-install`, or
   `launchctl kickstart -k gui/$(id -u)/com.mc.cli-bridge`). From now on it
   enforces the token and listens on `CLI_BRIDGE_BIND`.
5. Verify: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18792/health`
   → `401` (no token), and the agent page in MC shows the helper as reachable.

If you restart the helper first, MC gets `401` until step 2 is done — the
backend log says `Bridge GET /health rejected (401)`. Nothing is lost; finish
step 2.

**Rollback:** start the previous `scripts/cli-bridge.py`. It ignores the
header, so the new backend keeps working with it.

## Rotating the token

Delete or overwrite `~/.mc/secrets/cli-bridge.token`, then restart the helper
(it creates a new one). The backend picks it up on its next call. If you use
`CLI_BRIDGE_TOKEN` in `.env`, update it there and recreate `backend` +
`mc-worker`.
