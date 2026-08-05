# Mission Control Handbook

Mission Control is a self-hosted command center for AI agent fleets: you
describe work on boards, agents pick it up in real terminal sessions, code
on their own branches, open PRs, and watchdogs plus review gates keep the
whole thing honest. Everything runs on your own hardware — with your
Claude subscription, your own GPU, or both in one fleet.

This handbook is the map. Installation lives in the
[README](../../README.md); this is everything after `docker compose up`.

## Five-minute tour

1. **Home** is the pipeline: every task on the current board, in swim
   lanes from inbox to done, plus system health and recent events.
2. **Tasks** is the full board view; **Inbox** collects approvals and
   things that need a human.
3. **Agents** is the fleet registry: create agents from role templates,
   give them souls, bind them to a runtime, provision them.
4. **Sessions** attaches your browser to any agent's real terminal —
   watch it work, type into it.
5. **Runtimes** is where model endpoints live: Claude Code, vLLM,
   LM Studio, Ollama, any OpenAI-compatible `/v1` — switchable per agent
   with automatic rollback.
6. **Memory / Files / Repos / Skills** hold what the fleet knows: the
   Markdown vault, workspace files, the shared repos registry with
   per-repo work rules, and per-agent capability allowlists.

## Concepts — how MC thinks

| Page | What it explains |
|---|---|
| [Boards & tasks](concepts/boards-and-tasks.md) | Boards, the pipeline, projects & phases, task lifecycle |
| [Agents & souls](concepts/agents-and-souls.md) | Registry vs. provisioned agents, SOUL/TOOLS templates, roles |
| [Runtimes](concepts/runtimes.md) | Claude vs. OpenAI-compatible, harness decoupling, switching |
| [Dispatch lifecycle](concepts/dispatch-lifecycle.md) | ACK handshake, watchdogs, re-assignment, review gates, recovery |
| [Scopes & security](concepts/scopes-and-security.md) | The 21 per-agent scopes, tokens, vault, localhost-first networking |
| [Knowledge](concepts/knowledge.md) | Vault, board memory, lessons, hybrid search, insights |

## How-tos — get something done

| Page | What you get |
|---|---|
| [Your first agent](howto/first-agent.md) | From fresh install to a working agent |
| [Run a local LLM](howto/local-llm.md) | LM Studio / Ollama / vLLM as a runtime |
| [GitHub workflow](howto/github-workflow.md) | Repos, work rules, branch-per-task PR flow |
| [Integrations](howto/integrations.md) | Slack, Discord, Telegram, voice |
| [Verticals](howto/verticals.md) | Package a domain module on top of MC |

## Operations — run it for real

| Page | What it covers |
|---|---|
| [Hardware requirements](operations/hardware-requirements.md) | Measured RAM/CPU/disk numbers |
| [Backup & restore](operations/backup-restore.md) | `backup.sh`, schedules, what's covered |
| [Updating](operations/updating.md) | `install.sh --update`, image pinning, migrations |
| [Reverse proxy & remote access](operations/reverse-proxy.md) | Caddy, Tailscale, your own proxy |
| [Platforms](operations/platforms.md) | Linux, NAS catalogs, macOS, Windows, hypervisors |

When something breaks: [Troubleshooting](troubleshooting.md) ·
Quick answers: [FAQ](faq.md)

## Where deeper truth lives

- [Architecture](../ARCHITECTURE.md) — the living system doc
- [Decision records](../decisions/) — 70+ ADRs explaining *why* things
  are built the way they are
- [Flows](../flows/) — dispatch, task lifecycle, watchdogs, provisioning
  in detail
