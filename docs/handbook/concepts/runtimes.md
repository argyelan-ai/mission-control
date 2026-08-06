# Runtimes

A runtime answers the question "which model, on which machine, answers this agent's requests?". Mission Control keeps that separate from the agent itself: agents live on boards and own their identity and permissions, runtimes live in a registry and own an endpoint and a model. Bind one to the other and the agent starts thinking with that model; rebind it and — after a container restart — it thinks with a different one. The **Runtimes** page is where you manage this: register endpoints, start and stop remote engines, watch which model each one is actually serving, and switch an agent from Claude to a local model in one click, with an automatic rollback if the switch fails.

Do not confuse the two meanings of "runtime" in this codebase. `agents.agent_runtime` says **where the agent process runs** (`cli-bridge`, `host`, `claude-code`, `manual`). The runtime *registry* says **which LLM endpoint it talks to**. This page covers both, in that order.

## Where the agent process runs

| `agent_runtime` | What it is | Terminal access |
|---|---|---|
| `cli-bridge` | A Docker container `mc-agent-<slug>` with a tmux session and a poll loop | PTY proxy → `docker exec tmux attach` |
| `host` | A native process on the host machine, managed by launchd (macOS) | ttyd / host-pty proxy → xterm.js in the browser |
| `claude-code` / `manual` | Other/managed paths | — |

All runtimes are **poll-based** since v0.9: the agent asks the backend for work (`/agent/me/poll`, `/agent/me/next-task`) rather than the backend holding a session open to it. The OpenClaw Gateway that used to sit in the middle is gone ([ADR-039](../../decisions/039-openclaw-gateway-sunset.md)).

Host agents are macOS-only (launchd) and are not part of the one-click switch matrix in the same way containers are — see "Switching" below.

## The runtime registry

Runtimes are rows in the `runtimes` table ([ADR-017](../../decisions/017-runtime-registry-db.md)). `backend/config/runtimes.json` is only a **seed**: on startup, missing entries are inserted by slug (idempotent), and after that the database is the single source of truth for the API, the UI and the runtime manager. You never edit JSON to add an endpoint — you add it in the UI.

A runtime row carries a `slug`, `display_name`, `runtime_type`, `endpoint`, `model_identifier`, capability flags (`supports_tools`, `supports_reasoning`, `supports_streaming`, context lengths), and lifecycle wiring. `runtime_type` decides what MC can *do* to it:

| `runtime_type` | Lifecycle control | Typical setup |
|---|---|---|
| `lmstudio` | `lms load` / `lms unload` over SSH | LM Studio on a machine you own |
| `vllm_docker` | `docker start/stop/restart` over SSH | A self-hosted vLLM container on a GPU box |
| `unsloth` | tmux session start/stop over SSH | Unsloth Studio |
| `openai_compatible` | Health probe only | Any `/v1` endpoint you host or rent |
| `cloud` | Health probe only | Hosted providers (e.g. Ollama Cloud) |
| `unsloth_porsche` | Start/stop via a Flask control plane + Wake-on-LAN | A power-managed Windows box ([ADR-042](../../decisions/042-unsloth-porsche-power-managed-runtime.md)) |

The **hosts** those runtimes live on are themselves database rows ([ADR-048](../../decisions/048-host-registry.md)): a `hosts` table with `kind` = `ssh` | `flask_wol` | `local` plus connection details, and `runtimes.host_id` binding a runtime to its host. A fresh install with no GPU box has zero hosts and zero errors — cloud runtimes need no host at all.

## Claude Code vs. OpenAI-compatible endpoints

There are two credential worlds, and MC keeps them strictly apart.

**Claude Code (Anthropic protocol).** The `claude` binary authenticates with an OAuth token from your Claude Pro/Max subscription — you generate it once with `claude setup-token`, which opens a browser login and prints an `sk-ant-oat01-…` token. MC stores it as the `claude_code_oauth_token` secret and injects it into Anthropic-protocol agents. No per-token API billing is involved.

**OpenAI-compatible endpoints.** vLLM, LM Studio, Ollama Cloud and anything else exposing `/v1` get `OPENAI_BASE_URL` and `OPENAI_MODEL`, and an API key resolved in two stages ([ADR-056](../../decisions/056-harness-provider-decoupling.md)): the agent's own `secret_id` first, then the runtime's `api_key_secret_id`. If neither resolves, **no key is sent at all** — which is the correct behaviour for a keyless local vLLM. An earlier design had a third, global fallback and it was removed deliberately: it meant a free local agent could silently inherit a paid cloud key.

## Harness vs. provider (ADR-056)

The **harness** is the CLI binary driving the container — `claude`, `openclaude`, or `omp`. The **provider** is the runtime it talks to. Until [ADR-056](../../decisions/056-harness-provider-decoupling.md) these were one coupled axis: the harness was implicit in the runtime row, and every new combination needed its own bespoke `runtime_type` plus branch points scattered across three files.

Now they are two independent axes reconciled by one module (`backend/app/services/harness_compat.py`):

- `agents.harness` is the agent-level truth for which CLI runs. Legacy rows where it is still `NULL` fall back to `derive_harness(runtime)`, which reproduces the old coupling exactly — no forced migration.
- `runtime_protocol(runtime)` classifies a runtime as `anthropic`, `openai`, or `None`.
- `HARNESS_PROTOCOLS` is the v1 compatibility matrix: `claude` → anthropic, `openclaude` and `omp` → openai.
- The Docker **image follows the harness, not the runtime**.
- `GET /runtimes/compat-matrix` exposes compatible harnesses and a human-readable reason per incompatible pair, so the UI's harness selector doesn't re-derive the rules client-side.

Be aware of the honest gap: **Claude Code against an OpenAI-compatible endpoint is not supported in v1**, and neither is `omp`/`openclaude` against Anthropic. Cross-protocol combinations would need a translating proxy, which was consciously parked. The UI rejects the pairing with an explanation instead of silently misrouting requests.

## Switching an agent's runtime

Switching is not a config edit — it is an orchestrated, atomic operation ([ADR-018](../../decisions/018-runtime-switch-via-restart.md) → [ADR-027](../../decisions/027-universal-agent-runtime-binding.md)). Under the hood, `switch_agent_runtime()` runs:

1. **Validate** — runtime exists, is enabled, agent is switchable, harness/protocol compatible (rejected *before* any mutation).
2. **Busy check** — an agent with a task in progress is blocked, with a force toggle in the UI.
3. **Snapshot** the old state (runtime, harness, files).
4. **Lock** on Redis (`mc:agent:{id}:runtime-switch`, 120 s TTL) so two switches can't overlap.
5. **Render** the new compose file *before* touching any container, if the image changes.
6. **Commit** the new binding, re-render `.env` and `settings.json`.
7. **Restart** in the cheapest mode that works (below).
8. **Health-gate** the result, and on any failure **roll back everything** — database, files, compose, container — and emit `agent.runtime_switch_failed`.
9. On success, publish a terminal-remount signal so the Sessions page reconnects on its own.

Three restart modes, which is why "one click" is sometimes 5 seconds and sometimes 90:

| Mode | When | Cost |
|---|---|---|
| `respawn_window_only` | Same-image switch — only tmux window 0 is respawned; the poll loop survives | < 5 s |
| default restart | Environment refresh, no image change | ~5 s |
| `force_recreate` | Cross-image switch (different harness) | ~30–90 s |

There is a **dry run**: `POST /agents/{id}/preview-runtime-switch` returns the same result shape without mutating anything, and it is what fills the confirmation modal with its warnings and image banner.

Host agents with a registered adapter switch **in place** ([ADR-064](../../decisions/064-host-harness-adapter.md)): the binding is committed, `agent.env` is rewritten, and the adapter reloads the process sequentially — never two processes against the same state at once.

## Model drift: the engine leads, MC follows

If you change the model loaded in LM Studio or restart vLLM with different weights, MC notices. The runtime watcher ([ADR-054](../../decisions/054-runtime-watcher.md)) probes every enabled, probeable runtime roughly every 90 seconds via `GET {endpoint}/v1/models` and writes a live snapshot to Redis, which drives the live dot on the Runtimes page.

When the served model no longer matches the registry row, a **two-probe confirmation** (guarding against warm-up flapping) updates the row and then propagates to bound agents:

- **Idle agent** → config re-sync and restart, `agent.model_synced`.
- **Busy agent** → flagged `pending_runtime_sync` with a banner, synced on the next tick after its task ends. Nothing is interrupted mid-task.
- **Three failed sync attempts** → circuit breaker, `agent.model_sync_failed`, manual restart required.

Unreachability is handled separately and deliberately: an endpoint that cannot be reached only updates live status and fires `runtime.unreachable` after three consecutive failures. The registry row is left alone — MC never records a "drift to nothing".

## Related

- [Agents and souls](agents-and-souls.md) — what gets bound to a runtime
- [Scopes and security](scopes-and-security.md) — where provider keys and OAuth tokens are stored
- [`docs/ARCHITECTURE.md`](../../ARCHITECTURE.md) — the full runtime registry section, including power-managed and `omp` specifics
