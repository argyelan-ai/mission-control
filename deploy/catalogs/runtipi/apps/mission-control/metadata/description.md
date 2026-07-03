# Mission Control

Self-hosted command center for AI agent fleets — create agents, dispatch
tasks over Kanban boards, and watch them ship.

![Sessions](https://raw.githubusercontent.com/argyelan-ai/mission-control/main/docs/assets/mc-sessions.png)

## Highlights

- **Kanban dispatch** — describe work, drag it to *Ready*, an agent picks it
  up and reports back with deliverables.
- **Live sessions** — talk to running agents in real terminal sessions from
  the browser.
- **Multi-runtime** — Claude Code, vLLM, LM Studio, and any
  OpenAI-compatible API; switch per agent with one click.
- **Knowledge vault** — agents share notes, decisions, and learned lessons.
- **Budgets & cost tracking** — token and cost limits per agent and per day.

A first-run wizard walks you through creating the admin account and
connecting an LLM provider. The catalog deployment runs the core stack;
host-level agent runtimes (Docker fleet extras) are available on a manual
install — see the [project README](https://github.com/argyelan-ai/mission-control).
