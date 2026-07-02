# Your first agent

You've run the [Quickstart](../../README.md#quickstart): the core stack is up and
you've registered the admin user. This guide takes you from there to a running
Docker agent that can pick up a task.

The Docker agent fleet is a separate, host-coupled layer on top of the core
stack (see README → "The agent fleet (advanced)"). It needs three things you
haven't set up yet: an agent Docker image, LLM credentials, and a small
host-side helper process. All commands below run from the repo root.

## 1. Build an agent image

`scripts/build-agent-images.sh` builds the Docker image(s) agent containers
run from:

```bash
./scripts/build-agent-images.sh claude       # mc-claude-agent — Anthropic `claude` CLI
./scripts/build-agent-images.sh openclaude    # mc-agent-base — OpenAI-compatible CLI (vLLM/LM Studio/Ollama/any /v1 endpoint)
./scripts/build-agent-images.sh both          # both of the above (default when no arg given)
./scripts/build-agent-images.sh omp           # mc-omp-agent — headless bridge for local high-throughput OpenAI-compatible runtimes
```

Pick `claude` if you'll authenticate with an Anthropic Pro/Max subscription,
`openclaude` if you'll point agents at an OpenAI-compatible endpoint. Build
both if you're not sure yet — it costs a few minutes, not a decision.

## 2. Get LLM credentials into the vault

Agent containers fetch their credentials from the backend at startup
(`GET /api/v1/internal/bootstrap`, Docker-network-only) instead of reading
plaintext files. You need to get a token into the encrypted `secrets` table
first, via **Settings → API Keys** or the API directly.

**Path A — Anthropic (Claude Code, Pro/Max subscription)**

The Docker image needs a long-lived OAuth token, not a raw API key. Generate
one with Anthropic's official CLI (`npm install -g @anthropic-ai/claude-code`
if you don't have it) on any machine with a browser:

```bash
claude setup-token   # opens a browser login, prints a token like sk-ant-oat01-...
```

Paste the token into **Settings → API Keys → "Claude Code OAuth Token"**
(preset tile). If you prefer the API:

```bash
TOKEN=$(curl -s -X POST http://localhost/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"yourpassword"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST http://localhost/api/v1/secrets \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"key":"claude_code_oauth_token","value":"sk-ant-oat01-...","provider":"anthropic-claude-code","label":"Claude Code OAuth Token"}'
```

The key name must be exactly `claude_code_oauth_token` — it's shared by every
agent bound to one of the pre-seeded `anthropic-claude-opus` /
`anthropic-claude-sonnet` runtimes (see step 4).

**Path B — OpenAI-compatible endpoint (vLLM, LM Studio, Ollama Cloud, ...)**

Add the matching tile in **Settings → API Keys** (e.g. "Ollama Cloud API
Key") — this is the fallback credential the bootstrap endpoint uses for any
agent whose runtime isn't `anthropic-claude-*`. If your endpoint needs its
own runtime entry (custom vLLM/LM Studio host), create one under
**Runtimes → Add Runtime** (admin-only) with `runtime_type` = `vllm_docker` /
`lmstudio` / `openai_compatible` and your endpoint URL — the `ollama-cloud`
runtime is pre-seeded and enabled if you just want a quick cloud fallback.

## 3. Start the CLI-Bridge host helper

Agent provisioning (creating an agent's on-disk workspace, rendering its
`settings.json`/`SOUL.md`/`TOOLS.md`) is done by a small HTTP server that runs
**outside Docker, directly on the host** — `scripts/cli-bridge.py`, listening
on port `18792`. The backend container reaches it at
`http://host.docker.internal:18792` (wired via `extra_hosts` in
`docker-compose.yml`). Without it running, the "Provision" button in the UI
will fail.

```bash
python3 -m pip install --user jinja2 websockets   # one-time
brew install tmux                                 # if you don't have it
python3 scripts/cli-bridge.py &                   # keep this running (its own terminal/tmux/screen, or a launchd job)
```

There's no bundled launchd job for this yet — for anything beyond a local
test, keep it running under your process supervisor of choice (`tmux`,
`screen`, or your own `launchd`/`systemd` unit).

## 4. Create the agent

**Prerequisite: a board.** Template agents must be assigned to a board, and a
fresh install has none — create one first via the workspace switcher in the
sidebar (**+ Neues Board**), or run `python3 scripts/demo-seed.py` for a
ready-made demo board.

Open **Agents → Templates** and pick a builtin template — Researcher,
Writer, Reviewer, Tester, Developer, Deployer, or Lead. Templates come with a
role-appropriate scope set and a pre-written `SOUL.md`, which is the fastest
way to get a working agent; "Agents → New Agent" (custom) works too but
starts blank (keep its Runtime dropdown on the default, **"CLI Bridge
(lokal)"**).

Click a template card → fill in a Board and optionally a name/model → **Agent
erstellen**. This creates the agent (`provision_status: local`) and shows a
token — you can ignore/discard it, the next step issues a fresh one.

## 5. Bind a runtime and provision

On the agent's detail page:

1. **Runtime** section → pick the LLM runtime you set up in step 2 (e.g.
   "Claude Sonnet 4.6 (Anthropic Pro/Max)" or your OpenAI-compatible
   runtime) → save. This is also what selects the Docker image
   (`mc-claude-agent` vs `mc-agent-base`) and, since the agent has no
   container yet, brings one up for the first time
   (`docker compose ... up -d --force-recreate mc-agent-<slug>`) — give it
   30–90s.
2. Click **Provision**. This talks to the host helper from step 3 to create
   `~/.mc/agents/<slug>/` (queue dirs, `claude-config/`), then renders
   `SOUL.md`/`TOOLS.md`/`settings.json` from the DB and shows a one-time
   agent token.
3. If the container isn't up yet after that (check with `docker ps | grep
   mc-agent-<slug>`), bring the whole fleet up explicitly:
   ```bash
   ./scripts/start-all.sh
   # or: docker compose -f docker-compose.yml -f docker/docker-compose.agents.yml \
   #       --env-file .env --env-file docker/.env.agents up -d
   ```

## 6. Watch it come alive

- **ProvisionBadge** on the agent card turns "Live" once the container is
  running and has reported a heartbeat.
- **Agents → [agent] → Sessions** tab shows the agent's live tmux pane
  (window 0 = the CLI itself) streamed into the browser.
- `docker logs mc-agent-<slug> -f` shows the same thing from outside the UI —
  useful when the Sessions tab won't connect yet.

## 7. Give it a task

Create a task (Tasks/Pipeline board) and assign it to the agent, or let
Board-Lead dispatch pick it. Watch the status flow: `inbox` →
`in_progress` (the agent ACKs within ~10 min or gets re-assigned) → `review`
→ `done`. Task comments show the agent's progress updates; the Pipeline
board on the homepage shows all agents' lanes at a glance.

## Troubleshooting

| Symptom | Check |
|---|---|
| Agent card stuck on "Provisioning" | `docker logs mc-agent-<slug>` — look for `[entrypoint] FEHLER: CLAUDE_CODE_OAUTH_TOKEN fehlt` (secret missing/misnamed, step 2) or a bootstrap-retry loop (backend unreachable) |
| Provision button errors / times out | Is `scripts/cli-bridge.py` (step 3) actually running? `curl http://localhost:18792/health` from the host. Also check the backend container can reach it: `docker compose exec backend curl -s http://host.docker.internal:18792/health` |
| Container never appears (`docker ps` shows nothing) | Run `./scripts/start-all.sh` explicitly — bringing an agent up for the first time needs its service block picked up by `docker compose up -d` |
| Agent never ACKs a dispatched task | Is the tmux session actually inside the container? `docker exec -itu agent mc-agent-<slug> tmux capture-pane -p -t <slug>:1` (window 1 = `poll.sh` — should show poll activity, not a crash loop) |
| `image not found` / container won't start | Did you run `scripts/build-agent-images.sh` for the image your runtime needs (`claude` vs `openclaude` vs `omp`)? `docker images | grep mc-`. |
| Wrong/no LLM credentials reach the container | `docker exec mc-agent-<slug> env | grep -E 'CLAUDE_CODE_OAUTH_TOKEN|OPENAI_'` — empty means the bootstrap call failed or the secret key name doesn't match (`claude_code_oauth_token`, or your runtime's endpoint/`ollama_api_key`) |

## Host agents (advanced)

Boss/Hermes/Jarvis-style agents run as native macOS `launchd` jobs instead of
Docker containers (`agent_runtime: host`). That's a separate, macOS-specific
setup path — not covered here.
