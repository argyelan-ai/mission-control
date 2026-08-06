# Hardware requirements

Mission Control is light for what it does: the core stack is seven
containers, and the agents themselves spend most of their lives waiting on
an LLM. The numbers below are **measured on a fresh install** (idle core
stack, Docker on Apple Silicon, 2026-08), not guesses — your workload adds
on top, mainly per active agent.

## Measured baseline (core stack, idle)

| Service | RAM (idle) |
|---|---|
| backend (FastAPI) | ~250 MiB |
| frontend (Next.js) | ~90 MiB |
| qdrant (vector search) | ~65 MiB |
| db (PostgreSQL 16) | ~55 MiB |
| caddy (reverse proxy) | ~40 MiB |
| docker-socket-proxy | ~20 MiB |
| redis | ~10 MiB |
| **Core total** | **~530 MiB** |

CPU at idle is effectively zero on all services.

Each **Docker agent container** adds roughly **130–350 MiB** RAM
(measured across a mixed 12-agent fleet; a Claude Code agent working on a
task sits at the upper end). The LLM itself runs elsewhere — either at a
provider or on your own GPU box — so MC's host does not need GPU or
model-sized RAM.

## Disk

| What | Size |
|---|---|
| Core images (backend, frontend, postgres, redis, qdrant, caddy, proxy) | ~2.7 GB |
| Agent images (`mc-agent-base` + `mc-claude-agent`, built locally) | ~2.7 GB |
| Data volumes (db, qdrant, redis) | small at first, grows with usage |

Plan **20 GB free** to be comfortable with images, volumes, logs and
backups; 10 GB works for a core-only trial.

## Recommendations

| Scenario | RAM | CPU | Disk |
|---|---|---|---|
| Trial / core only (boards, vault, API — no agents) | 2 GB | 1–2 vCPU | 10 GB |
| Small fleet (1–4 Docker agents) | 4 GB | 2 vCPU | 20 GB |
| Comfortable fleet (5–12 agents + voice/browser profiles) | 8 GB | 4 vCPU | 40 GB |

Reference point: the maintainer's fleet — core stack, 12 agent containers
and optional profiles — runs on a Mac mini M4 while staying under a
10 GB Docker memory limit.

## What does NOT run on this host

- **The models.** Claude runs at Anthropic (via your subscription);
  local models run wherever you point the OpenAI-compatible runtime at
  (a GPU box, LM Studio on your desktop, a NAS with Ollama). Sizing GPU
  hardware for local models is a separate topic — see
  [Run a local LLM](../howto/local-llm.md).
- **Voice and browser sidecars** are optional compose profiles
  (`COMPOSE_PROFILES=voice,browser`) and off by default; enable them only
  if you use them.
