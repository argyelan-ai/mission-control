# CLI-Bridge auth: shared token + loopback bind

`scripts/cli-bridge.py` runs on the host, outside Docker. It starts agents,
installs plugins, rebuilds images and attaches terminals (WebSocket PTY).
Only Mission Control may talk to it. Two layers protect it:

| Layer | What | Default |
|---|---|---|
| **Shared token** (the real protection) | Every request needs the header `X-MC-Bridge-Token`. Missing or wrong → HTTP `401`; for the WebSocket PTY the handshake itself is answered with `401`, so no socket opens. Compared in constant time. | always on |
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
| Backend + worker | `CLI_BRIDGE_TOKEN` → file `CLI_BRIDGE_TOKEN_FILE` → `$HOME_HOST/.mc/secrets/cli-bridge.token` |
| `scripts/mc-mcp.py` (operator mode only) | same as the helper |

- The helper creates the file on first start (`secrets.token_urlsafe(32)`,
  mode `0600`, directory created if missing). It never prints or logs the
  value. If the file exists with looser permissions, it is tightened to `0600`.
- A token shorter than 32 characters (env or file) makes the helper refuse to
  start (exit code 2, the log says why — without the value).
- The backend and worker containers bind-mount `~/.mc` 1:1, so on a standard
  install they read the **same file** — nothing to copy. The backend reads the
  file on every call, so a rotated token is picked up without a restart.
- **Prefer the file.** If the token lives elsewhere, point both sides at it
  with `CLI_BRIDGE_TOKEN_FILE` (a 0600 file under `~/.mc/`, which the
  containers can see).
- **`CLI_BRIDGE_TOKEN` in `.env` is the last resort** — only when the
  container cannot read any host file. ⚠ `.env` sits in the repo checkout, and
  that checkout is mounted read-only into agent containers, so every agent
  container could read the token. `docker-compose.yml` passes it to `backend`
  and `mc-worker`.

### Limits

- **Host agents run as the same OS user** as the helper and can read the token
  file like any other file of that user. The token keeps the network and
  other machines out; it does not separate processes of one user. Agent
  containers do not mount `~/.mc/secrets` and cannot read it.
- `scripts/mc-mcp.py` in agent mode never reads the token; it only calls
  `/ping`.

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

1. Update the checkout the helper runs from (`git pull` there) and create the
   token file: `python3 scripts/cli-bridge.py --init-token`.
2. Deploy the new backend + worker. They read the file and send the header.
   The old helper ignores the header, so nothing breaks in between.
3. Check that the backend sees the file (prints `True`, not the token):
   `docker compose exec backend python -c "from app.services.cli_bridge_auth import bridge_token; print(bool(bridge_token()))"`
   If it prints `False`, set `CLI_BRIDGE_TOKEN_FILE` to a 0600 file under
   `~/.mc/` that both sides can read. Only if that is impossible, use
   `CLI_BRIDGE_TOKEN` in `.env` (see the warning above). Then recreate
   `backend` + `mc-worker`.
4. **Replace the running helper** — from the checkout it should run from:
   `make bridge-install`. On macOS this also **retires older launchd jobs that
   run `scripts/cli-bridge.py` under another label** (hand-made installs used
   their own label): it stops them, moves their plist to
   `~/.mc/backups/launchd/<label>.plist.bak-<date>`, waits until the port is
   free, then starts `com.mc.cli-bridge`. A label it cannot detect can be named
   in `CLI_BRIDGE_LEGACY_LABELS`. If something else still holds the port, it
   stops with the process name and changes nothing. Re-running it is safe.
5. Verify:
   - `lsof -nP -iTCP:18792 -sTCP:LISTEN` and `lsof -nP -iTCP:18793 -sTCP:LISTEN`
     → exactly one listener each, on **`127.0.0.1`** (not `*`).
   - `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18792/health` → `401`.
   - `make bridge-status` → RUNNING; the agent page in MC shows the helper as
     reachable.

If you restart the helper first, MC gets `401` until step 2 is done — the
backend log says `Bridge GET /health rejected (401)`. Nothing is lost; finish
step 2.

**Rollback:** `make bridge-uninstall`, move the backed-up plist back to
`~/Library/LaunchAgents/` and `launchctl bootstrap gui/$(id -u) <plist>`. The
old helper ignores the header, so the new backend keeps working with it.

## Rotating the token

Delete or overwrite `~/.mc/secrets/cli-bridge.token`, then restart the helper
(it creates a new one). The backend picks it up on its next call. If you use
`CLI_BRIDGE_TOKEN`, update it and recreate `backend` + `mc-worker`.
