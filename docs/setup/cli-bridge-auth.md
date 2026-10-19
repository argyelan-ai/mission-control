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

Only `GET /ping` answers without a token. It returns `{"status": "ok",
"api_version": 1}` and nothing else — liveness plus the version of the update
door below. `GET /health` needs the token,
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

Create the files (bridge + update door) without starting the helper:

```bash
python3 scripts/cli-bridge.py --init-token     # prints the paths, never a token
```

## Bind address per platform

**macOS (Docker Desktop):** keep the default `127.0.0.1`. Docker Desktop
delivers connections to `host.docker.internal` on the host's loopback (the
helper sees them coming from `127.0.0.1`), so the backend container still
reaches the helper, while other machines on the LAN do not.

**Linux:** a helper on `127.0.0.1` is not reachable from the container. Let
the installer set it up — from inside the install folder:

```bash
./install.sh --bridge
```

It binds the helper to the **gateway of MC's own Docker network**
(`docker network inspect mission-control_default`, e.g. `172.18.0.1`): the
backend container reaches that address, other machines on the LAN do not.
Because `host.docker.internal` (`host-gateway`) points at a *different*
address (the default `docker0` bridge, usually `172.17.0.1`), the installer
also writes `FREE_CODE_BRIDGE_URL=http://<gateway>:18792` into `.env` and
recreates `backend` + `mc-worker`, which read it (`docker-compose.yml`).

- The gateway belongs to the network: `docker compose down` removes the
  network, and the next `up` can give it another subnet. The helper then
  cannot bind and its service retries; `./install.sh --update` and
  `./install.sh --bridge` move it to the new address.
- **Linger** (`sudo loginctl enable-linger <user>`): without it the user's
  services stop at logout and do not start after a reboot. The installer
  offers it and runs it only after an explicit `y`; unattended installs set
  `MC_BRIDGE_LINGER=yes`. The update preflight shows a red check while it is
  off — an update interrupted by a power cut could not finish otherwise.

By hand, the same as the installer does: `CLI_BRIDGE_BIND=<gateway>
make bridge-install` plus `FREE_CODE_BRIDGE_URL` in `.env`. `0.0.0.0` also
works but exposes the port to the network; if you need it, put a host
firewall in front. The token protects the helper in every case.

## The update door (`/mc-update/*`, ADR-094)

The helper also carries the door through which **Settings → System** updates
MC (`scripts/mc_update_door.py`). It has its **own token scope**:

| | |
|---|---|
| Header | `X-MC-Update-Token` — the bridge token does not open the door, and the update token opens nothing else |
| Token file | `~/.mc/secrets/mc-update.token` (0600, created on first start or by `--init-token`; override `MC_UPDATE_TOKEN_FILE`). Deliberately **no** environment variable: `.env` is readable inside agent containers |
| Routes | exactly five: `POST /mc-update/preflight`, `POST /mc-update/start` (`{"expected_target": "vX.Y.Z"}`), `GET /mc-update/status[?run=<id>]`, `POST /mc-update/rollback` (`{"run_id"?}`), `POST /mc-update/resume` (`{"run_id"?}`) — no tag, ref, URL or command is taken from a request; the engine picks the target and only compares |
| Answers | `401` token · `404` unknown route · `405` wrong method · `409` `MC_UPDATE_MANAGED=deploy` or another target than the engine's · `422` a preflight check failed (nothing changed) · `423` a run is live or interrupted · `429` more than `MC_UPDATE_MAX_STARTS_PER_DAY` (default 3) starts in 24 h — preflight, status, resume, rollback, terminal runs and starts that failed on the network before the switch do not count |
| Detached | the engine runs outside the helper: Linux `systemd-run --user` (own unit `mc-update-<job>`), otherwise a double fork in a new session; the engine's output and exit code land in `~/.mc/update/bridge/`; the service units add `KillMode=process` / `AbandonProcessGroup`. Restarting the helper never stops an update |
| Start routine | when the helper starts, an interrupted run (journal open, lock holder dead) is resumed once `docker info` answers (max. 10 min); with no open run, a drain no run owns any more is lifted |

`GET /ping` answers `{"status": "ok", "api_version": 1}` — the version of
the door, so MC can tell an old helper apart.

## Rollout on an existing install (no downtime)

Backend and helper are updated separately. Order matters: **backend first.**

1. Update the checkout the helper runs from (`git pull` there) and create the
   token file: `python3 scripts/cli-bridge.py --init-token`.
2. Deploy the new backend + worker. They read the file and send the header.
   The old helper ignores the header, so nothing breaks in between.
3. Check that the backend sees the file (prints `True`, not the token):
   `docker compose exec -u mcuser backend python -c "from app.services.cli_bridge_auth import bridge_token; print(bool(bridge_token()))"`
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
