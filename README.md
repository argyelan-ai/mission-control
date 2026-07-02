# Mission Control

[![CI](https://github.com/argyelan-ai/mission-control/actions/workflows/ci.yml/badge.svg)](https://github.com/argyelan-ai/mission-control/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-0fa3a3.svg)](LICENSE)

**Self-hosted command center for AI agent fleets.** Create agents, give them
souls, dispatch tasks, watch them ship — from a single dark-mode control room,
running entirely on your own hardware.

![Mission Control — control room](docs/assets/mc-home.png)

## How it works

![How Mission Control works](docs/assets/how-it-works.svg)

You describe the work on a Kanban board. Mission Control dispatches it to an
agent — a Claude Code instance or any OpenAI-compatible LLM (vLLM, LM Studio,
Ollama) in a Docker container. The agent codes on its own branch, opens a PR,
a reviewer agent gates the merge, and watchdogs catch anything that stalls.

## Install in one line

![Installing Mission Control](docs/assets/install-demo.svg)

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

It checks prerequisites, pulls the prebuilt images (or builds locally),
configures secrets, boots the stack and opens the browser — where a first-run
wizard walks you through admin account, LLM provider key and a demo board.
Updating later is `./install.sh --update`. Details in
[Quickstart](#quickstart) · [Windows](docs/setup/windows.md) ·
[Updating](docs/setup/updating.md).

## Highlights

- **Multi-runtime agent fleet** — Docker cli-bridge agents (Claude Code binary
  or OpenAI-compatible runtimes like vLLM / LM Studio / Ollama) and host-side
  agents, switchable per agent at runtime with automatic rollback.
- **Task orchestration** — boards, projects, phase-based planning,
  dispatch-ACK handshake, watchdogs, automatic re-assignment, review gates.
- **Agent git workflow** — one repo per project, one branch per task,
  automatic PRs and squash-merges via the GitHub CLI.
- **Knowledge & memory** — a Markdown vault as source of truth, hybrid FTS5 +
  vector search (Qdrant), per-agent lessons, daily LLM-distilled insights.
- **Live terminals** — attach to any agent's tmux session from the browser.
- **Integrations (all optional)** — Discord per-agent channels, Telegram
  approvals + report delivery, voice agent (LiveKit + realtime speech API).
- **Scope-based permissions** — 16 API scopes per agent, PBKDF2 agent tokens,
  JWT user auth with roles.

<details>
<summary><b>More screenshots</b> — first-run wizard, agent registry, runtime manager</summary>

*The first-run wizard — from empty install to configured in three steps:*
![First-run wizard](docs/assets/mc-setup-wizard.png)

*The agent registry — one fleet, mixed runtimes (Claude + local Qwen via vLLM):*
![Agent registry](docs/assets/mc-agents.png)

*The runtime manager — GPU hosts, models, live binding of agents to runtimes:*
![Runtime manager](docs/assets/mc-runtimes.png)
</details>

## Architecture

```
Browser → Caddy (:80) → Frontend (:3000) / Backend (:8000)
                          ↓                    ↓
                     Next.js 15           FastAPI + SSE
                                              ↓
                              PostgreSQL 16 + Redis 7 + Qdrant
                                              ↓
                                   ┌──────────────────────────────┐
                                   │ Multi-Runtime Agent Dispatch │
                                   │  • cli-bridge (Docker)       │
                                   │  • host agents (optional)    │
                                   │     ↑ poll loop              │
                                   └──────────────────────────────┘
```

- **Backend**: Python 3.12, FastAPI, SQLModel, asyncpg, PostgreSQL 16,
  Redis 7, Alembic, sse-starlette
- **Frontend**: Next.js 15 (App Router), TypeScript strict, Tailwind CSS v4,
  TanStack Query v5, Zustand, Recharts
- **Infrastructure**: Docker Compose, Caddy reverse proxy

Living architecture doc: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
Decision records: [`docs/decisions/`](docs/decisions/)

## Quickstart

Prerequisites: Docker (with Compose v2), `git`, `openssl`, and optionally
`python3` (nicer secret generation).

**Platforms:** developed on macOS, CI-tested on Linux (the fresh-boot E2E job
runs the full quickstart on Ubuntu). On Windows, use **WSL2**
(recommended) or native PowerShell with `setup.ps1` — both experimental, see
[docs/setup/windows.md](docs/setup/windows.md). Host-side agents (launchd)
are macOS-only; the Docker fleet is not.

One line (checks prerequisites, clones, configures, pulls the prebuilt
images from GHCR — or builds locally as fallback — boots and migrates):

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/argyelan-ai/mission-control.git
cd mission-control

./setup.sh                                            # generates .env with secure secrets
docker compose up --build -d                          # build + start (migrations run automatically)
```

Then open **http://localhost** and register the first admin user (the
register endpoint only works while no user exists).

That's a full working core: UI, task boards, knowledge base, API. Everything
below is optional and off by default.

### Optional integrations

| Feature | What you set in `.env` |
|---|---|
| Agent git workflow (repos, PRs, merges) | `GH_TOKEN`, `GITHUB_OWNER` |
| Discord notifications + per-agent channels | `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID` |
| Telegram approvals / reports | `TELEGRAM_*` tokens + chat IDs |
| Voice agent (LiveKit + realtime speech) | `LIVEKIT_*`, `XAI_API_KEY`, `JARVIS_AGENT_TOKEN` |
| Remote LLM runtime host via SSH | `DGX_SSH_HOST`, `DGX_SSH_USER` + SSH-key mount |
| Reachability from other devices | `PUBLIC_HOST`, `LIVEKIT_NODE_IP`, TLS via `caddy/Caddyfile.tls.example` |

Voice (LiveKit) and the Playwright visual-verify sidecars are behind
compose profiles — enable with `COMPOSE_PROFILES=voice,browser` in `.env`
(the default boot is the lean core stack).

Want something to look at before provisioning your first agent?

```bash
python3 scripts/demo-seed.py            # demo board + tasks across the pipeline
python3 scripts/demo-seed.py --cleanup  # remove it again
```

Host-specific mounts (SSH keys, sandbox dirs, custom Caddyfile) go into
`docker-compose.override.yml` — see
[`docker-compose.override.example.yml`](docker-compose.override.example.yml).

### The agent fleet (advanced)

The Docker agent fleet (`docker/docker-compose.agents.yml`) is a separate,
host-coupled layer on top of the core stack: it provisions per-agent
containers with tmux sessions, a poll loop, and rendered SOUL/TOOLS files.
Start with the core stack first; provision agents via the UI (Agents → New →
Provision) once it runs. Agent souls and settings are rendered from
`backend/templates/*.j2` — customize `USER.md.j2` (who you are) and set
`OPERATOR_NAME` in `.env` (how agents address you).

Step-by-step: [docs/setup/first-agent.md](docs/setup/first-agent.md).
Updating an install: [docs/setup/updating.md](docs/setup/updating.md).

## Development

```bash
# Backend tests (pytest — SQLite in-memory + fakeredis, no Docker needed)
cd backend && python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]" && pytest -v

# Frontend tests (vitest — jsdom, no browser needed)
cd frontend-v2 && npm install && npm run test:run

# Rebuild after code changes
docker compose up --build -d backend
docker compose up --build -d frontend
```

~2000 tests total. Design system spec lives in [`DESIGN.md`](DESIGN.md)
(dark-mode only, single teal accent) and [`PRODUCT.md`](PRODUCT.md).

## Language note

The codebase grew in a German-speaking home lab: many ADRs
(`docs/decisions/`), inline comments and some UI strings are German.
The README, setup flow and API are English; full i18n is on the roadmap
and contributions are welcome.

## Access from your phone, anywhere (Tailscale)

MC binds to localhost by design — the recommended way to reach it from your
phone, laptop or office is [Tailscale](https://tailscale.com) (free for
personal use, zero config):

1. Install Tailscale on the machine running MC and on your phone (same account).
2. Put the machine's Tailscale name into `.env`:
   `PUBLIC_HOST=your-machine.tailnet-name.ts.net` (adds it to the CORS allowlist).
3. Open `http://your-machine.tailnet-name.ts.net` on the phone. Done — the
   full control room, task approvals and live agent terminals, from anywhere.

For HTTPS on the tailnet, see `caddy/Caddyfile.tls.example`. This setup keeps
MC completely unreachable from the public internet — exactly how it's meant
to run.

## Security notes

- The backend reaches Docker only through a filtering socket-proxy
  (whitelisted API paths, no build/swarm/system — see
  [ADR-047](docs/decisions/047-docker-socket-proxy.md)). Container lifecycle
  control is still powerful: run MC only on hosts you trust end-to-end, and
  never expose the stack directly to the internet.
- All service ports except Caddy (:80/:443) bind to `127.0.0.1`.
- Secrets live in the encrypted `secrets` table (Fernet) or in `.env` —
  see [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

AGPL-3.0 — see [LICENSE](LICENSE). Use it, self-host it, modify it freely;
if you distribute a modified version or offer it as a network service, share
your changes under the same license. For commercial licensing beyond AGPL,
contact the maintainer.
