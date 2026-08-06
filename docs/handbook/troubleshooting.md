# Troubleshooting

Failure modes that actually happen, in the order you are likely to hit them.
Every entry: symptom → cause → check → fix.

First stop for anything stack-wide:

```bash
docker compose ps                       # what is up, what is restarting
docker compose logs backend --tail=50   # or: make logs
curl -sf http://localhost:8000/health   # backend liveness
```

---

## Install & boot

### Backend refuses to start: "Refusing to start with insecure configuration"

**Cause** — the stack was started without `./setup.sh`, so `JWT_SECRET_KEY` is
still a placeholder or `SECRETS_ENCRYPTION_KEY` is empty. The backend runs
with `ENVIRONMENT=production` and fails fast rather than booting with forgeable
admin tokens.

```bash
docker compose logs backend --tail=30
grep -E '^(JWT_SECRET_KEY|SECRETS_ENCRYPTION_KEY)=' .env
```

**Fix**

```bash
./setup.sh                # idempotent; generates every secret into .env
docker compose up -d
```

### `install.sh` aborts on prerequisites

**Cause** — Docker daemon not running, Compose v1 instead of v2, or a missing
`git`/`openssl`. The installer checks all four before touching anything.

```bash
docker info >/dev/null && echo daemon-ok
docker compose version     # must work — "docker-compose" (v1) is not enough
```

**Fix** — start Docker Desktop / `systemctl start docker`, install Compose v2.
On Windows, run the installer inside WSL2; `install.sh` refuses any OS other
than Linux and macOS.

### Caddy won't start / `http://localhost` doesn't answer

**Cause** — something else already owns port 80 (or 443). Caddy publishes both.

```bash
docker compose ps caddy
docker compose logs caddy --tail=20
sudo lsof -i :80          # who has the port
```

**Fix** — stop the other service, or publish MC on different host ports via
`docker-compose.override.yml`.

---

## Access

### Can't reach MC from your phone / another machine

**Cause** — every published port binds to `127.0.0.1` by default. This changed
in the current release; installs that used to be reachable from the LAN stop
being reachable after the update.

```bash
grep MC_BIND_ADDRESS .env
docker compose ps caddy   # PORTS column shows 127.0.0.1:80->80/tcp
```

**Fix**

```bash
# .env
MC_BIND_ADDRESS=0.0.0.0
```
```bash
docker compose up -d caddy
```

Do this only after the first admin is registered, and preferably on a tailnet
— see [Reverse proxy & remote access](operations/reverse-proxy.md).

### UI loads but every request fails (CORS errors in the console)

**Cause** — you are reaching MC under a hostname that is not in the backend's
CORS allowlist. Only `localhost` variants are allowed out of the box.

```bash
grep -E '^(PUBLIC_HOST|EXTRA_CORS_ORIGINS)=' .env
docker compose logs backend --tail=30
```

**Fix** — set `PUBLIC_HOST=<the host you type in the browser>` (adds
`http://…`, `http://…:80`, `https://…`) or list extra origins in
`EXTRA_CORS_ORIGINS`, then `docker compose up -d backend`.

### UI is dead when opened on port 3000

**Cause** — the prebuilt frontend makes same-origin `/api/*` calls. Reaching
`:3000` directly bypasses Caddy, so the page renders and every API call 404s.

**Fix** — always enter through Caddy (`http://localhost`, port 80). Same rule
applies behind your own reverse proxy: forward to Caddy, not to the frontend.

### Registration returns 403 "Registration geschlossen"

**Cause** — `POST /api/v1/auth/register` only works while no user with a
password exists. Someone (or an earlier install against the same database)
already registered.

```bash
curl -s http://localhost:8000/api/v1/auth/setup-required
```

**Fix** — log in with the existing account. If you lost it, have an admin
create a user, or restore/reset the database
([Backup & restore](operations/backup-restore.md)).

---

## Agents

### "Provision" button errors or times out

**Cause** — `scripts/cli-bridge.py` is not running. Provisioning (workspace
creation, rendering `SOUL.md`/`TOOLS.md`/`settings.json`) happens on the host,
not in a container.

```bash
curl http://localhost:18792/health
docker compose exec backend curl -s http://host.docker.internal:18792/health
```

**Fix**

```bash
python3 -m pip install --user jinja2 websockets
python3 scripts/cli-bridge.py &     # keep it under tmux/screen/launchd
```

### Agent card stuck on "Provisioning"; container restarts

**Cause** — the LLM credential is missing or stored under the wrong key. The
container fetches credentials from the backend at startup.

```bash
docker logs mc-agent-<slug> --tail=40      # look for:
#   [entrypoint] FEHLER: CLAUDE_CODE_OAUTH_TOKEN fehlt
docker exec mc-agent-<slug> env | grep -E 'CLAUDE_CODE_OAUTH_TOKEN|OPENAI_'
```

**Fix** — store the token under **exactly** `claude_code_oauth_token`
(Settings → API Keys → "Claude Code OAuth Token"). Generate it with
`claude setup-token` — the Docker image needs a long-lived OAuth token, not an
API key. For OpenAI-compatible runtimes, add that runtime's key tile instead.
Full walkthrough: [Your first agent](howto/first-agent.md) ·
[docs/setup/first-agent.md](../setup/first-agent.md).

### Agent container never appears in `docker ps`

**Cause** — the agent's compose service block was never picked up; bringing an
agent up the first time needs an explicit fleet start.

```bash
docker ps | grep mc-agent-
```

**Fix**

```bash
./scripts/start-all.sh
```

### Container won't start: `image not found`

**Cause** — the agent image for that runtime was never built.

```bash
docker images | grep mc-
```

**Fix** — build the one your runtime needs:

```bash
./scripts/build-agent-images.sh claude       # mc-claude-agent
./scripts/build-agent-images.sh openclaude   # mc-agent-base
./scripts/build-agent-images.sh omp          # mc-omp-agent
```

### Agent never ACKs a dispatched task

**Cause** — the poll loop inside the container is dead or crash-looping
(window 1 of the agent's tmux session).

```bash
docker exec -itu agent mc-agent-<slug> tmux capture-pane -p -t <slug>:1
```

**Fix** — restart the container; if it crash-loops, work backwards from
`docker logs mc-agent-<slug>` (usually credentials or a bad runtime binding).
A task that is never ACKed triggers the watchdog after a runtime-dependent
window (15 minutes for Docker cli-bridge agents, 5 for host agents, by
default — `AGENT_RUNTIME_ACK_TIMEOUTS` in
`backend/app/services/task_runner.py`). At half that time the dispatch is
silently retried by rotating the attempt ID; at the full timeout you get an
escalation approval in the inbox. Override per agent with
`dispatch_config["ack_timeout_minutes"]`.

### Can't create an agent from a template — no board to pick

**Cause** — template agents must be assigned to a board, and a fresh install
has none.

**Fix** — create one via **New board** in the board switcher, or seed the
demo board.

---

## Demo seed

### `LOCAL_AUTH_TOKEN not found — run ./setup.sh first`

**Cause** — `scripts/demo-seed.py` authenticates with `LOCAL_AUTH_TOKEN` from
`./.env`, which only `setup.sh` writes. Catalog installs (Umbrel, Runtipi,
CasaOS) have no such `.env`.

```bash
grep '^LOCAL_AUTH_TOKEN=' .env
```

**Fix** — run from a checkout that has a generated `.env`, or export the value
(`LOCAL_AUTH_TOKEN=… python3 scripts/demo-seed.py`). On catalog installs, use
the demo-board step in the first-run wizard instead — it runs with your login.

### `Demo board already exists (slug 'demo-product-launch')`

**Fix**

```bash
python3 scripts/demo-seed.py --cleanup && python3 scripts/demo-seed.py
```

---

## Updating & data

### `./install.sh --update` stops at `git pull`

**Cause** — the update runs `git pull --ff-only`; local commits or a dirty
working tree make it refuse, and `set -e` aborts the script.

```bash
git status
git log --oneline origin/main..HEAD
```

**Fix** — commit or reset your local changes, then re-run. Host-specific
config belongs in `.env` and `docker-compose.override.yml` (both gitignored),
not in tracked files.

### `./backup.sh restore` says "No backup found"

**Cause** — restore looks in `./backups/` relative to the current directory
and matches `mc_backup_*.sql.gz`.

```bash
ls -1t backups/mc_backup_*.sql.gz
```

**Fix** — run from the repo root, or pass the path explicitly:
`./backup.sh restore /path/to/mc_backup_<ts>.sql.gz`. If the matching
`mc_data_<ts>.tar.gz` is missing, the script restores the database only and
says so — `~/.mc` (vault, agent configs) then stays as-is.

### Disk filling up with container logs

**Cause** — on installs from before the current release, container logs grew
unbounded. They are now capped at 10 MB × 3 files per service.

```bash
docker system df
du -sh /var/lib/docker/containers/* 2>/dev/null | sort -h | tail
```

**Fix** — update, then recreate the containers (`docker compose up -d`) so the
new logging options apply. `docker system prune` reclaims the rest.
