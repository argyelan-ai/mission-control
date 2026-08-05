# How to: get your first agent running

You have the core stack up (`./install.sh` or `./setup.sh` + `docker compose up
-d`) and you have registered the first admin user at `http://localhost`. That
is a complete Mission Control — boards, vault, API — but no agents yet.

The Docker agent fleet is a **separate, host-coupled layer** on top of the core
stack. Getting one agent to pick up a task takes five things: an agent image,
LLM credentials, a host helper process, a board, and the agent itself.

Run every command below from the repo root. The full reference for this path,
including a troubleshooting table, is
[docs/setup/first-agent.md](../../setup/first-agent.md) — this page is the
short version.

## 1. Build an agent image

Agent containers do not ship as prebuilt images yet. Build them locally:

```bash
./scripts/build-agent-images.sh claude       # mc-claude-agent — Anthropic `claude` CLI
./scripts/build-agent-images.sh openclaude   # mc-agent-base — OpenAI-compatible CLI
./scripts/build-agent-images.sh both         # both (default when no argument is given)
./scripts/build-agent-images.sh omp          # mc-omp-agent — headless NDJSON bridge
```

Pick `claude` if you will authenticate with an Anthropic Pro/Max subscription,
`openclaude` if your agents will talk to an OpenAI-compatible endpoint (vLLM,
LM Studio, Ollama). Building both takes a few minutes and saves you a decision.

The CLI version each image installs comes from `docker/cli-versions.json`, not
from the Dockerfile — edit and rebuild to pin a version yourself.

## 2. Put LLM credentials in the vault

Agent containers fetch credentials from the backend at startup
(`GET /api/v1/internal/bootstrap`, reachable only inside the Docker network).
They never read plaintext files, so the token has to be in the encrypted
`secrets` table first.

**Anthropic (Claude Code, Pro/Max subscription).** The container needs a
long-lived OAuth token, not a raw API key. Generate one with Anthropic's CLI on
any machine with a browser:

```bash
npm install -g @anthropic-ai/claude-code   # if you don't have it
claude setup-token                         # browser login, prints sk-ant-oat01-…
```

Paste it into **Settings → API Keys → "Claude Code OAuth Token"**. The secret
key must be exactly `claude_code_oauth_token` — every agent bound to a
pre-seeded `anthropic-claude-*` runtime reads that one key.

**OpenAI-compatible endpoint.** Add the matching key tile in **Settings → API
Keys**, or attach a key to the runtime itself when you create it. See
[How to: run agents on a local LLM](local-llm.md).

## 3. Start the CLI-Bridge host helper

Provisioning an agent — creating its on-disk workspace and rendering
`SOUL.md`, `TOOLS.md` and `settings.json` — is done by a small HTTP server that
runs **outside Docker, directly on the host**: `scripts/cli-bridge.py` on port
`18792`. The backend container reaches it at
`http://host.docker.internal:18792`. Without it, the Provision button fails.

```bash
python3 -m pip install --user jinja2 websockets   # one-time
brew install tmux                                 # or your platform's package manager
python3 scripts/cli-bridge.py &                   # keep it running
```

There is no bundled service unit for this. For anything beyond a local test,
keep it alive under `tmux`, `screen`, or your own `launchd`/`systemd` job.

## 4. Create a board, then the agent

Template agents must belong to a board, and a fresh install has none. Create
one via **New board** in the board switcher, or seed a demo board:

```bash
python3 scripts/demo-seed.py            # demo board + tasks across the pipeline
python3 scripts/demo-seed.py --cleanup  # remove it again
```

Then open **Agents → Templates** and pick a builtin role — Researcher, Writer,
Reviewer, Tester, Developer, Deployer or Lead. Templates arrive with a
role-appropriate scope set and a pre-written `SOUL.md`, which is the fastest
route to a working agent. "New Agent" (custom) also works but starts blank.

Fill in the board, optionally a name and model, and create it. The agent is now
`provision_status: local` and shows a token — you can discard it, the next step
issues a fresh one.

## 5. Bind a runtime, then provision

On the agent's detail page:

1. **Runtime** → pick the runtime you want (e.g. a Claude runtime, or your own
   OpenAI-compatible one) and save. The runtime also selects the Docker image,
   so this is what brings the container up for the first time. Give it 30–90
   seconds.
2. Click **Provision**. This calls the host helper from step 3 to create
   `~/.mc/agents/<slug>/`, renders `SOUL.md` / `TOOLS.md` / `settings.json`
   from the database, and shows a one-time agent token.
3. If no container appeared (`docker ps | grep mc-agent-<slug>` is empty),
   bring the fleet up explicitly:

   ```bash
   ./scripts/start-all.sh
   ```

## 6. Watch it work

- The provision badge on the agent card flips to **Live** once the container
  runs and has sent a heartbeat.
- **Agents → [agent] → Sessions** streams the agent's live tmux pane into the
  browser (window 0 is the CLI itself).
- `docker logs mc-agent-<slug> -f` shows the same from outside the UI — useful
  while the Sessions tab still refuses to connect.

Now create a task and assign it to the agent, or let board-lead dispatch pick
it up. The status flow is `inbox` → `in_progress` (the agent must ACK within
a runtime-dependent window — 15 minutes for Docker cli-bridge agents by
default — or the watchdog steps in) → `review` → `done`.

## Rough edges you should know about

- **No prebuilt agent images.** The core stack pulls prebuilt images from
  GHCR; agent images you build yourself (step 1). Publishing them is on the
  roadmap.
- **No managed cli-bridge service.** `scripts/cli-bridge.py` is a foreground
  process you supervise yourself, and it needs `tmux` and a POSIX host. A
  managed service unit is on the roadmap.
- **Host agents are macOS-only.** Agents that run as native `launchd` jobs
  instead of containers (`agent_runtime: host`) are a separate, macOS-specific
  path. The Docker fleet is not macOS-bound.
- **Provisioning is not idempotent from the UI alone.** If a step fails
  halfway, the troubleshooting table in
  [docs/setup/first-agent.md](../../setup/first-agent.md) maps symptoms to the
  step that actually broke.

## Where to go next

- [Run agents on a local LLM](local-llm.md) — runtimes, model binding,
  switching between Claude and a local model.
- [Connect GitHub](github-workflow.md) — so agents work on branches and open
  PRs instead of only commenting.
- [Chat and voice integrations](integrations.md) — Slack, Discord, Telegram,
  voice.
