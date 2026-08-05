# Agents and Souls

An agent in Mission Control is a row in the database plus a process somewhere that keeps asking "do you have work for me?". The database row is the identity: name, role, permissions, token, personality, and which runtime it is bound to. The process is a real CLI — Claude Code, an OpenAI-compatible harness, or a host-side binary — running in a tmux session you can attach to from the browser. Everything the agent knows about itself and about your system arrives as generated Markdown files (`SOUL.md`, `TOOLS.md`, `USER.md`, `CARD.md`) that are rendered from templates whenever the agent is provisioned or synced. This page explains that split and the pieces you configure.

## Registered vs. provisioned

Creating an agent and making it *run* are two steps, and the `provision_status` field tracks which one you are at:

| `provision_status` | Meaning |
|---|---|
| `local` | The row exists. Token generated, `TOOLS.md` rendered. Nothing is running yet |
| `provisioning` | Files are being staged / the container is coming up |
| `provisioned` | Config files are in place for this agent |
| `error` | Provisioning failed |

This is deliberately separate from the agent's *runtime* status (online/offline/restarting), which the watchdog maintains from heartbeats. A registered-but-not-provisioned agent is a perfectly normal state — it just cannot receive work.

The **agent wizard** (Agents → New) is the single creation path for every runtime ([ADR-063](../../decisions/063-agent-onboarding-wizard-host-provisioning.md)). It walks you through runtime and harness, scopes, SOUL, and finishes with a readiness check. Two details worth knowing:

- The wizard renders a **live SOUL preview** through a side-effect-free endpoint (`POST /agents/preview-soul`) against a transient, never-persisted agent — so you can read the actual system prompt before you commit to it.
- For **host agents**, provisioning only *stages* the launchd files (`.plist`, `run.sh`, `agent.env`) into `~/.mc/agents/<slug>/`. Actually loading the launchd job is gated behind `host_agent_autoload_enabled`, which defaults to **off**; when disabled, the response hands you the exact `launchctl bootstrap` command to run yourself. A fresh install never silently registers background processes on your machine.

The agent's API token is generated once at creation (PBKDF2-SHA256, 200 000 iterations) and shown **exactly once** in the response. Only the hash is stored. If you lose it, reset it — you cannot recover it.

## Roles

The role decides the agent's default permissions and shapes its SOUL. `AgentRole` in `backend/app/scopes.py` defines:

`lead` · `developer` · `reviewer` · `tester` · `planner` · `researcher` · `deployer` · `writer` · `orchestrator` · `relay`

`relay` is a legacy role from the retired OpenClaw Gateway ([ADR-039](../../decisions/039-openclaw-gateway-sunset.md)) — ignore it for new agents. Roles also drive dispatch logic: `developer` and `deployer` count as worker roles, while `planner`, `researcher`, `writer` and `orchestrator` are explicitly non-workers and are skipped by checks that assume someone is executing.

Each role maps to a default scope set (`DEFAULT_SCOPES`), covered in [Scopes and security](scopes-and-security.md). `lead` and `orchestrator` get everything.

Separately from the role, `is_board_lead` marks the agent that receives unassigned work first and is exempt from the mandatory reflection.

## Templates for one-click roles

Seven builtin agent templates are seeded on startup (idempotently) from `backend/app/services/template_seeder.py`:

**Lead · Developer · Reviewer · Tester · Researcher · Writer · Deployer**

Each template carries a role, an emoji, a default model, a skill list, the role's default scopes, and a full `soul_md`. Instantiating one gives you a working agent without writing a system prompt. You can edit the SOUL afterwards — per-agent overrides live in the database, not in the template.

## SOUL, TOOLS, USER, CARD

Agent config files are **rendered from Jinja2 templates** in `backend/templates/`, and the templates are the single source of truth ([ADR-006](../../decisions/006-jinja2-template-source-of-truth.md)): the database supplies the inputs, the template supplies the logic, and the files on disk are artefacts. Editing a rendered file by hand works until the next sync overwrites it.

| File | Source | What it carries |
|---|---|---|
| `SOUL.md` | `SOUL.md.j2` | Persona, role rules, lifecycle, review policy, reflection charter, reporting and deliverable rules |
| `USER.md` | `USER.md.j2` | Who *you* are — the operator profile the agent works for |
| `MEMORY.md` | `MEMORY.md.j2` | Skeleton for the agent's private notes |
| `CARD.md` | `CARD.md.j2` | The short operating card: hard gates the agent must never skip |
| `agent.env`, `settings.json`, worker/poll scripts, `agent.plist` | `cli_agent.env.j2`, `cli_agent_settings.json.j2`, `cli_agent_worker.sh.j2`, `host_agent_*.j2`, `agent.plist.j2` | Runtime wiring for Docker and host agents |

**`TOOLS.md` is the exception** — it is not a `.j2` template. It is generated in Python by `backend/app/services/tools_md_builder.py`, because each section is gated on the agent's scopes: an agent without `memory:write` never sees the memory-write examples. The same builder also varies phrasing by runtime (a host agent gets host paths like `~/.mc/vault/...` where a container agent gets `/vault`).

Two things follow from this design that regularly surprise people:

1. **Changing a template requires re-syncing the agents.** Until you run sync-config or reprovision, running agents still hold the old rendered files.
2. **Changing an agent's scopes changes its `TOOLS.md`.** Permissions and documentation cannot drift apart, because they are generated together.

### Personas

`agents.soul_persona_md` holds ~80–120 tokens of character voice per agent, rendered as the first content block of the SOUL ([ADR-021](../../decisions/021-agent-personas.md)). The intent is practical rather than cosmetic: when you read a stack of reflection comments, you should be able to tell who wrote which one. Personas are seeded idempotently — only `NULL` values are filled, so your hand-edits survive re-runs.

The same ADR moved the **reflection charter** and its four required fields into `backend/app/constants.py`, so the SOUL template, the enforcement code in the agent router, and the error messages all read the same definition. Change it in one place, and everything follows.

## Skills, CLI plugins and MCP servers

Four nullable JSON columns on the agent control capabilities: `cli_skills`, `cli_plugins`, `mcp_servers`, and `skill_filter`. They are **allowlists** with a consistent convention — `null` means "everything available", `[]` means "nothing", and a list means exactly those entries.

The **Skills** page is where you manage this across the fleet: a skill matrix and a plugin matrix (agents × capabilities), the MCP server registry, and a shell for installed plugins.

Agents can *request* installs but never perform them ([ADR-015](../../decisions/015-install-approval-flow.md)). A request goes through a **source allowlist** (`backend/app/services/install_allowlist.py`) before it can even become an approval — only skills from `~/.mc/skills/` or a handful of trusted GitHub orgs, plugins from the official plugin source, MCP servers from specific npm scopes or repos whose name contains `mcp`. Adding a new source is a code change, not an API call; that regex list is the trust boundary. What passes the allowlist becomes an approval in your **Inbox**, and only your approval triggers the executor — which updates the agent's allowlist, installs, syncs the config, and rolls back on failure.

MCP servers are synced to disk per agent ([ADR-016](../../decisions/016-mcp-registry.md)): the allowlist filters the registry's manifests into the agent's own `.mcp.json`, which its CLI reads at the next session start.

## Where an agent's knowledge lives

The SOUL is identity, not memory. Durable knowledge goes to the **vault** (`~/.mc/vault/agents/<slug>/`), where each agent owns its own directory and writes cross-agent notes through an inbox rather than into someone else's folder. Lessons extracted from reflections come back into the next dispatch briefing. See [Knowledge](knowledge.md).

## Related

- [Runtimes](runtimes.md) — what actually executes the agent, and how to switch it
- [Scopes and security](scopes-and-security.md) — tokens, scopes, and what each role gets by default
- [Dispatch and lifecycle](dispatch-lifecycle.md) — how work reaches the agent
- [`docs/flows/agent-provisioning.md`](../../flows/agent-provisioning.md) — the code-level walkthrough
