# Quickstart: install to your first remote-controlled agent

This is the short path to the thing the project is actually about today: a
lead agent you can steer from the MC chat, on your phone or desktop. It is a
tighter version of [Your first agent](first-agent.md) — read that one for
every detail and the troubleshooting table; read
[Heads and remote control](../concepts/heads-and-remote-control.md) first if
you want the concept before the steps.

## 1. Install the core stack

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

It installs the latest release, starts Postgres, Redis, Qdrant, the backend
and the frontend, and prints the address to open (on a server without a
screen it creates the first admin in the terminal — see the README's
Install section). After the first login the setup wizard opens by itself.
No agent exists yet — this step alone is boards, vault and API, nothing that
talks to a model.

## 2. Pick your model and get a key in place

- **Claude Code (Anthropic Pro/Max subscription):**
  ```bash
  npm install -g @anthropic-ai/claude-code   # if you don't have it
  claude setup-token                         # browser login, prints sk-ant-oat01-…
  ```
  Paste the printed token into **Settings → API Keys → "Claude Code OAuth
  Token"** (the secret must be stored under exactly `claude_code_oauth_token`).
- **A local model (vLLM, LM Studio, Ollama, any OpenAI-compatible `/v1`):**
  add the key (if the endpoint needs one) under **Settings → API Keys**, or
  attach it to the runtime when you create it. See
  [Run agents on a local LLM](local-llm.md).

You can do both — the lead agent and the local omp agent below use different
credentials and can run side by side.

## 3. Get the agent image(s) you need

The Claude Code agent image is published with every release (amd64 and
arm64) and is pulled automatically. The local omp agent image is built on
your machine (arm64 only for now):

```bash
./scripts/build-agent-images.sh omp      # for the local omp agent (one-time, ~6 min on a Pi 4)
```

Build the Claude image yourself only on `--channel edge`
(`./scripts/build-agent-images.sh claude`).

## 4. Start the host helper

The host helper (`scripts/cli-bridge.py`) is a small process the backend
calls to provision an agent's workspace and render its config files. Without
it, the **Provision** button fails. Install its dependencies once, then run it
as a managed service (launchd on macOS, a systemd user unit on Linux):

```bash
# macOS
python3 -m pip install --user jinja2 websockets && brew install tmux
# Debian / Ubuntu / Raspberry Pi OS
sudo apt install -y tmux python3-jinja2 python3-websockets nodejs npm
curl -fsSL https://claude.ai/install.sh | bash      # Claude Code CLI, if the lead agent runs on Claude

make bridge-install        # run inside your Mission Control folder
```

The helper listens on `127.0.0.1` only and accepts requests only with a
shared token that MC creates on first start — see
[Host helper authentication](../../setup/cli-bridge-auth.md).

## 5. Create your lead agent

Boards come first (**New board**, or `python3 scripts/demo-seed.py` for a
seeded demo board — a fresh install's setup wizard does this for you and
shows the classic multi-agent board, not the two-agent view described above;
that is the current first-run experience, not a bug).

Then **Agents → New agent**:

- **Start:** pick a template for an agent that does the work itself — for
  example **Developer**. Do *not* pick the built-in **Lead** template: it is a
  pure orchestrator from the old fleet model that only delegates and writes
  no code. There is no template yet for the hands-on lead agent that
  [ADR-089](../../decisions/089-lead-agent-with-hands.md) describes.
- **Identity:** tick **Board lead**. This sets `is_board_lead`; no template
  sets it for you.
- Bind it to your Claude Code (or OpenAI-compatible) runtime and click
  **Provision**.

This is the agent you will talk to in the chat: it plans, does the work,
reads results and reports back to you.

## 6. Create your local omp agent (optional, but it is the other half of "remote control")

**Agents → New agent**, harness `omp`, bound to your local runtime. Provision
it the same way. This is the agent that picks up small jobs on your own GPU
box; it talks to MC over the ACP protocol.

## 7. Open the chat

**Chats** in the navigation (or the chat icon in the mobile nav) → pick the lead agent or
the local omp agent → type. You should see live tool output as it works,
diffs landing per edit, and (once it has touched files) a **Changes** panel
with Revert / Commit / Open PR. This is the surface
[ADR-090](../../decisions/090-chat-as-remote-control.md) calls your remote
control, and it is the same surface on a phone over Tailscale/your reverse
proxy (see [Reverse proxy & remote access](../operations/reverse-proxy.md)).

## What this quickstart does not cover (on purpose)

- **Heads** (a short-lived session per job, its own git worktree) are an
  opt-in preview behind `HEADS_ENABLED` (default off, macOS host only) — see
  [Heads and remote control](../concepts/heads-and-remote-control.md).
  Nothing above turns them on.
- **Voice** (calling the lead agent, OpenAI Realtime, your own key, daily
  budget cap) is a separate, optional preview, off by default
  (`VOICE_LIVE_ENABLED`). See
  [ADR-091](../../decisions/091-live-voice-in-the-chat.md) and the voice
  block in `.env.example`. (The voice described under
  [Integrations](integrations.md) is the older, separate LiveKit voice.)
- **GitHub** — without it, agents can still work but cannot open a PR. See
  [Connect GitHub](github-workflow.md).

## Known gap in this path

There is no single setup step that says "this agent is the lead agent, go."
You combine a working template (step 5) with the **Board lead** tick box in
the Identity step — the mechanism is documented in
[Agents and souls](../concepts/agents-and-souls.md). The built-in **Lead**
template does not do this for you, and it only delegates. If you are coming
from the old multi-agent fleet mental model, "which agent is in charge" is
easy to get wrong on a board with several agents: the demo board already has
its own board lead from the demo crew. Pick the board lead explicitly if you
seed the demo board and then clean it up.
