# How to: run agents on a local LLM

Mission Control agents do not have to run on Claude. Any endpoint that speaks
the OpenAI `/v1` protocol — LM Studio, vLLM, Ollama Cloud, a hand-rolled proxy
— can drive an agent, and you can move an agent between Claude and a local
model in one click.

This page covers registering such an endpoint as a **runtime**, binding an
agent to it, and switching back and forth.

## How runtimes are stored

Runtimes live in the `runtimes` database table, which is the single source of
truth ([ADR-017](../../decisions/017-runtime-registry-db.md),
[ADR-028](../../decisions/028-runtime-registry-and-session-propagation.md)).
`backend/config/runtimes.json` is only a **seed** for first boot; editing it
after the fact changes nothing. Manage runtimes through the `/runtimes` page or
the `/api/v1/runtimes` API instead.

A runtime row is a *provider*: where the model is served and which model to
ask for. Supported `runtime_type` values are `lmstudio`, `vllm_docker`,
`openai_compatible`, `cloud`, `unsloth` and `unsloth_porsche`. The fields that
matter most:

| Field | What it does |
|---|---|
| `endpoint` | Base URL, e.g. `http://192.0.2.10:8000/v1` — becomes `OPENAI_BASE_URL` in the agent container |
| `model_identifier` | Model name sent as `OPENAI_MODEL` |
| `container_name` | `vllm_docker` only — the container start/stop acts on |
| `lms_identifier` / `lms_cli_path` | `lmstudio` only — what `lms load/unload` operates on |
| `api_key_secret_id` | Optional per-runtime API key from the encrypted vault |
| `launch_command` | Optional re-launch command when the container is gone ([ADR-036](../../decisions/036-runtime-launch-command.md)) |

## 1. Register the endpoint

Make sure the endpoint answers `GET <base>/v1/models` (or `<base>/models` if
your URL already ends in `/v1`) from the machine running MC. Then:

1. Open **Runtimes** → **Add runtime**.
2. Paste the **Endpoint URL** and press **Probe**. MC calls the models list,
   fingerprints the engine (LM Studio via its `/api/v0/models` REST API, vLLM
   via `/version`, everything else as `openai_compatible`) and shows the
   models it found.
3. Pick the **Model** and give the runtime a **Display name**.
4. Choose an API key mode: **No key**, **Existing key** (pick one from the
   vault) or **New key** (store one now). Local vLLM and LM Studio usually need
   none.
5. **Add runtime.**

The same probe is available headless:

```bash
curl -X POST http://localhost/api/v1/runtimes/probe-endpoint \
  -H "Authorization: Bearer <your-admin-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"url":"http://192.0.2.10:8000/v1"}'
```

If the model server runs on **another machine**, MC drives its lifecycle
(start/stop, `lms load/unload`) over SSH. Set `DGX_SSH_HOST` and
`DGX_SSH_USER` in `.env` and mount your `~/.ssh` — see
`docker-compose.override.example.yml`.

## 2. Understand the two axes: harness and provider

Since [ADR-056](../../decisions/056-harness-provider-decoupling.md) an agent has
two independent settings:

- **`harness`** — which CLI binary runs in the container: `claude`,
  `openclaude` or `omp`. This picks the Docker image (`mc-claude-agent`,
  `mc-agent-base`, `mc-omp-agent`).
- **`runtime_id`** — which provider row above it talks to.

Not every combination is valid. The compatibility matrix in v1 is:

| Harness | Works with |
|---|---|
| `claude` | Anthropic-protocol runtimes only |
| `openclaude`, `omp` | OpenAI-protocol runtimes only |

The runtime switch UI reads this from `GET /api/v1/runtimes/compat-matrix` and
refuses an invalid pairing with an explanation instead of silently misrouting
requests. **Claude Code against a local vLLM model is therefore not possible
today** — that needs a protocol-translating proxy, which ADR-056 parks as v2.

## 3. Bind an agent to the runtime

Only Docker agents (`agent_runtime: cli-bridge`) can be bound and switched;
host agents run their own native binary.

From the agent side: open the agent's detail page, use the **Runtime** section,
pick the target runtime (and harness, where offered) and confirm. The modal
shows a dry-run preview first — it calls
`POST /api/v1/agents/{id}/preview-runtime-switch`, which returns exactly what
the real switch would do, without mutating anything.

From the runtime side: on `/runtimes`, each card has **Bind Agent**, which does
the same thing for one or more agents at once.

If the agent is mid-task, the switch is blocked unless you tick the force
toggle in the confirm dialog.

## 4. What happens on a switch

The switch is atomic, with a Redis lock and full rollback on failure
([ADR-027](../../decisions/027-universal-agent-runtime-binding.md)). Two speeds:

- **Same image family** (e.g. one OpenAI endpoint to another): MC respawns the
  CLI window inside the running container via tmux, so the poll loop and the
  recycler survive. Typically under 5 seconds.
- **Image change** (Claude ↔ local model, or a harness change): MC re-renders
  the generated compose file and recreates the container. 30–90 seconds.

If the health check after the restart fails, the database row, the rendered
files, the compose file and the container are all rolled back, and an
`agent.runtime_switch_failed` event is written.

Because the compose file for agents is generated from the database, **do not
hand-edit `docker/docker-compose.agents.yml`** — the next switch overwrites it
(a `.bak` copy is kept).

## 5. How credentials reach the container

Credential routing is centralized so the Anthropic and OpenAI paths can never
mix:

- **Anthropic protocol** → the global `claude_code_oauth_token` secret is
  delivered as `ANTHROPIC_AUTH_TOKEN`, plus `ANTHROPIC_MODEL` when the runtime
  sets a `model_identifier`.
- **OpenAI protocol** → `OPENAI_BASE_URL` from `runtime.endpoint` and
  `OPENAI_MODEL` from `runtime.model_identifier`. The key for `OPENAI_API_KEY`
  is resolved in order: the agent's own secret, then the runtime's
  `api_key_secret_id`. **There is deliberately no global fallback key** — a
  keyless local runtime must stay keyless rather than silently inheriting a
  paid cloud key.

To check what actually arrived:

```bash
docker exec mc-agent-<slug> env | grep -E 'ANTHROPIC_|OPENAI_'
```

An empty result means the bootstrap call failed or the secret key name does not
match.

## 6. Day-to-day runtime operations

Each card on `/runtimes` exposes the lifecycle actions its type supports:
**Start**, **Stop**, **Restart**, **Wake** (for power-managed hosts), **Load** /
**Unload** for LM Studio models, and **Re-probe model**.

For `vllm_docker`, Start first tries `docker start <container_name>`. If the
container no longer exists — recipe launchers with `--rm` remove it on stop —
MC falls back to the runtime's `launch_command` over SSH. Without a
`launch_command`, you get a clear error instead of a confusing "no such
container".

A background watcher probes runtimes periodically and flags **Drift** when the
engine serves a different model than the registry says; the binding syncs on
the next tick, or immediately via **Sync now (force)** — which interrupts a
running task, so it is not the default.

## Limitations

- No Claude Code against OpenAI-compatible endpoints, and no `openclaude`/`omp`
  against Anthropic (ADR-056, v1 scope).
- Changing the harness of a running container without a restart is out of
  scope; a harness change always goes through the image-change restart path.
- SSH-managed runtimes need the SSH key mount to be present in the backend
  container, otherwise lifecycle actions fail while health probes still work.
