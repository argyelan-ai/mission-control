# Scopes and Security

Mission Control hands autonomous processes real credentials and lets them run shell commands. That only works if every agent's reach is deliberately bounded, so the permission model is the part of the system you should understand before you expose anything. There are two entirely separate authentication worlds — humans authenticate with JWTs and hold roles, agents authenticate with tokens and hold scopes — and they meet at no point in the routing. On top of that sit two encrypted stores with different audiences, a filtering Docker proxy, and a network posture that assumes you are running on a machine you own. This page covers all of it, including where the boundaries are weaker than they look.

## Two API surfaces

There are two routers, on purpose ([ADR-009](../../decisions/009-agent-scoped-router-separat.md)). User-facing endpoints (`/api/v1/tasks`, `/agents`, `/boards`, …) require a user JWT and check roles. Agent-facing endpoints all live under `/api/v1/agent/*` and require an agent token plus the right scope.

They are genuinely different endpoints with different logic, even where they touch the same object: `PATCH /tasks/{id}` and `PATCH /agent/boards/{board_id}/tasks/{task_id}` are not the same code path. The alternative — one router with dual auth — was rejected because every route would have to know which caller it was serving, and a misconfigured dependency would silently hand agent traffic a user-only endpoint. The cost is some duplication; the benefit is that an agent endpoint cannot accidentally inherit user privileges.

## Agent tokens

An agent's token is generated once at creation, returned **exactly once** in the response, and stored only as a **PBKDF2-SHA256 hash with 200 000 iterations** (random salt). There is no recovery — if it is lost, reset it with `POST /api/v1/agents/{id}/reset-token`.

Verifying that hash costs roughly 200 ms, which is fine once and ruinous at fleet polling rates. So verification is cached in Redis ([ADR-010](../../decisions/010-redis-cache-pbkdf2.md)): the key is `SHA256(token)`, the value is only the agent id, TTL 5 minutes. The raw token is never stored anywhere, in any form. Redis being down degrades performance, not correctness — it falls back to the database path.

One consequence to plan around: because the cache has a 5-minute TTL and no explicit invalidation, a reset token can leave the old one working for up to five minutes.

## The scopes

Scopes are defined in `backend/app/scopes.py`. There are **21** of them today (some documentation still says 16 — that count is stale):

| Scope | Grants |
|---|---|
| `tasks:read` | Read board snapshot and tasks |
| `tasks:write` | Update task status and fields |
| `tasks:create` | Create tasks |
| `tasks:manage` | Broader task management |
| `tasks:help` | Ask for help on a task |
| `knowledge:read` | Read the knowledge base |
| `knowledge:write` | Write knowledge entries |
| `memory:read` | Read memory / search own lessons |
| `memory:write` | Write memory and lessons |
| `vault:read` | Read the Markdown vault |
| `vault:write` | Write vault notes |
| `project:read` | Read project data |
| `project:write` | Write project data |
| `approvals:create` | Raise an approval for the operator |
| `chat:write` | Post to chat / team channels |
| `content:submit` | Submit content pipeline stages |
| `agents:manage` | Create and manage agents (board leads) |
| `credentials:read` | Read task-time credentials from the vault |
| `deploy:execute` | Execute deployments |
| `heartbeat` | Send heartbeats |
| `telegram:send` | Push a vault file to the operator's Telegram |

A missing scope returns 403 with the exact scope named. An agent with **no** scopes set is treated as having all of them — a backward-compatibility default worth knowing when you audit an old agent row.

`telegram:send` is deliberately narrow: it can push any file from the vault onto your phone, so it is granted sparingly.

### Defaults per role

Each role gets a default scope set. `lead` and `orchestrator` receive everything; the rest are trimmed to their job:

| Role | Notable grants | Notably missing |
|---|---|---|
| `developer` | tasks read/write, knowledge, memory r/w, vault r/w, credentials, project r/w | `tasks:create`, `content:submit`, `deploy:execute`, `agents:manage` |
| `reviewer` | tasks read/write, knowledge r/w, memory read, vault read | memory write, vault write, credentials |
| `tester` | tasks read/write, knowledge r/w, memory write, credentials | vault access entirely |
| `deployer` | as developer, plus `deploy:execute` | `tasks:create` |
| `researcher` | knowledge r/w, memory **read and write**, content submit, vault r/w | credentials, deploy |
| `writer` | tasks read/write, knowledge read, memory read, content submit, vault r/w | knowledge write, credentials |
| `planner` | knowledge r/w, memory write, project r/w, vault r/w | `tasks:write` |

The researcher's `memory:read` is a scar worth reading as a lesson: it originally had write only, so `mc memory search` returned 403 and the agent could never retrieve its own earlier lessons. The learning loop was silently broken by a missing read scope.

Scopes are not only enforced — they are **documented per agent**. `TOOLS.md` is generated with sections gated on the agent's scopes, so an agent never sees curl examples for endpoints it cannot call. Permissions and documentation are generated from the same input and cannot drift.

## Users, roles and login

Human authentication is JWT-based with three roles in a strict hierarchy: `admin` (3) > `operator` (2) > `viewer` (1).

Registration is open **only while no user exists** — the first account you create is the admin, and the endpoint closes behind you. Register that first admin before you widen network access, not after.

## Secrets vs. credentials

There are two encrypted stores, both using Fernet, and confusing them has cost real debugging time — which is why [ADR-033](../../decisions/033-secrets-vs-credentials-boundary.md) codified the boundary instead of merging them.

| | **Secrets** (Settings → API Keys) | **Credentials** (Credentials Vault) |
|---|---|---|
| Purpose | How MC itself talks to the world | What MC hands an agent to finish a task |
| Identifier | Unique `key` (`openai_api_key`, `github_token`) | UUID + free-form `name` |
| Shape | A single encrypted string | Typed JSON: `login` / `token` / `custom` |
| Write access | **Admin only** | Any logged-in user |
| Agent access | **None** — no agent endpoint exists | Read via `credentials:read`, per board |
| Cardinality | One per provider/service | Many per use case |

The rule of thumb: a system token for a provider (LLM key, GitHub, Discord) is a **secret**, and the backend fetches it on the agent's behalf. A login or token needed to do the actual task (a website, an external API) is a **credential**, referenced by `credential_id` and never pasted into a task description or a chat message.

Agents cannot currently *create* credentials — you add them in the UI. That is a known gap, not an oversight.

## Docker access

The runtime-switch feature needs the Docker API from inside the backend container. Mounting `/var/run/docker.sock` there would make any backend RCE equivalent to host root, so [ADR-047](../../decisions/047-docker-socket-proxy.md) put a filtering proxy in between. Only `docker-socket-proxy` mounts the socket (read-only); the backend talks to it over `DOCKER_HOST=tcp://docker-socket-proxy:2375`, and it is not published on any port — it exists only on the internal compose network. The whitelist covers containers, images, networks, volumes, exec and info; **build, swarm and system are blocked**.

The ADR is honest about what this does *not* achieve, and so is this page: `POST` plus `CONTAINERS` still allows creating a container with arbitrary bind mounts. The proxy meaningfully reduces the attack surface — no raw socket, no build, no swarm — but it is **not a complete privilege boundary**. A full one would need a broker validating request bodies, which was deliberately deferred under a "trusted network" threat model.

## Network posture

Every service port, including Caddy's `:80`/`:443`, binds to `127.0.0.1` by default. Nothing is reachable from another machine until you opt in by setting `MC_BIND_ADDRESS=0.0.0.0` — and you should do that only after the first admin account exists, since registration is open until then.

The recommended way to reach MC from a phone or laptop is a private VPN such as Tailscale: set `MC_BIND_ADDRESS`, add your tailnet hostname to `PUBLIC_HOST` for CORS, and the stack stays completely unreachable from the public internet. Do not port-forward it.

## The honest threat model

Agents execute code and shell commands **by design**. Scopes and per-agent tokens bound what they can do through MC's API, but an agent with `deploy:execute` or a broad workspace mount can affect the host, and container lifecycle control remains a powerful capability even behind the socket proxy. MC is built to run on hardware you own, on a network you trust. It is not hardened for direct internet exposure, and [`SECURITY.md`](../../../SECURITY.md) says so plainly — including the hardening checklist and how to report a vulnerability privately.

## Related

- [Agents and souls](agents-and-souls.md) — roles, templates, and the install allowlist
- [Runtimes](runtimes.md) — how provider credentials are resolved per agent and runtime
- [`SECURITY.md`](../../../SECURITY.md) — threat model and operator checklist
