# ADR-056 — Harness/Provider-Decoupling (`agents.harness` + compat matrix)

**Status:** Accepted
**Datum:** 2026-07-05
**Scope:** Backend/Runtime · Backend/DB · Backend/Auth · Frontend/Runtimes

## Kontext

Before this ADR, a cli-bridge agent's **harness** (which CLI binary drives
the container — Claude Code, OpenClaude, or omp) was implicit in its
**runtime** (`agents.runtime_id` → `runtimes.runtime_type`/`slug`).
`compose_renderer.pick_image_for_runtime` picked the Docker image straight
off the runtime row, `internal.build_runtime_env` branched on
`runtime_type`/slug prefixes, and `docker_agent_sync` repeated the same
slug-prefix checks. Harness and provider were one coupled axis.

That coupling broke down exactly on Sparky (ADR-045/ADR-049): to run the
omp harness against an Ollama-hosted model, the runtime row itself had to
carry `runtime_type == "omp"` as a special case, even though "omp" is a CLI
harness, not a wire protocol — the actual protocol underneath is plain
OpenAI-completions. Every future harness × provider combination (e.g.
Claude Code against an OpenAI-compatible endpoint via a future LiteLLM
proxy) would have needed its own bespoke runtime_type and its own
slug-prefix branch scattered across three files, none of which shared a
single source of truth for "is this combination even valid?".

## Entscheidung

**Two independent axes: `agents.harness` (which CLI) and `runtimes.*`
(which provider/protocol), reconciled through one central matrix.**

1. **`agents.harness`** (migration `0142`, nullable string: `claude` |
   `openclaude` | `omp`) is the agent-level source of truth for which CLI
   binary runs in the container. It is independent of `runtime_id`.
2. **`harness_compat.py`** (`backend/app/services/harness_compat.py`) is
   the single classification module, replacing the scattered slug-prefix
   checks in `internal.py`, `docker_agent_sync.py`, and
   `compose_renderer.py`:
   - `runtime_protocol(runtime)` classifies a runtime row into
     `"anthropic"` | `"openai"` | `None` (special/unknown, e.g. hermes —
     not part of the switch matrix). `omp` is kept as a legacy
     `runtime_type` value that classifies as plain OpenAI — a runtime row
     with that type is just an OpenAI provider once the harness moved to
     the agent.
   - `HARNESS_PROTOCOLS` is the **v1 compatibility matrix**: `claude` →
     `{anthropic}`, `openclaude`/`omp` → `{openai}`. `is_compatible(harness,
     runtime)` and `incompat_reason(harness, runtime)` (German UI tooltip
     text) are derived from it.
   - `derive_harness(runtime)` is the **legacy fallback** for agents whose
     `harness` column is still `NULL`: it reproduces the pre-ADR-056 image
     coupling (`omp` runtime_type → `omp`; anthropic protocol → `claude`;
     openai protocol → `openclaude`) so unmigrated rows behave exactly as
     before.
3. **Image follows harness, not runtime.**
   `compose_renderer.pick_image_for_harness(harness, runtime)` looks up
   `HARNESS_IMAGES[harness]` first; only when `harness` is `None` (legacy
   row / host agent) does it fall through to the old
   `pick_image_for_runtime(runtime)` path. `detect_image_change` now takes
   both `old_harness`/`new_harness` alongside the runtime pair, so a
   harness-only switch (same runtime, different CLI) is detected as an
   image change too.
4. **`agent_runtime_switch.switch_agent_runtime`** gained a second,
   independent switch axis: an optional `new_harness` parameter. The
   effective harness for validation and persistence resolves through
   `new_harness` (explicit request) → `agent.harness` (current) →
   `derive_harness(runtime)` (legacy fallback) — so an unmigrated agent
   still has a usable effective harness for the compat check.
   Incompatible combinations (e.g. `claude` against an OpenAI-protocol
   runtime) are rejected with `incompat_reason` before any DB/container
   mutation; the switch snapshot/rollback path now restores `agent.harness`
   alongside `agent.runtime_id` on failure.
5. **Three-stage provider credential resolution**
   (`harness_compat.resolve_provider_credentials`), the single source used
   by both `/internal/bootstrap` and the `.env` render so the two can never
   drift:
   - Anthropic protocol → global OAuth token (`claude_code_oauth_token`
     secret).
   - OpenAI protocol → **(1)** `agent.secret_id` (per-agent key) → **(2)**
     `runtime.api_key_secret_id` (new writable column on `runtimes`,
     `routers/runtimes.py` compat-matrix/patch endpoints) → **(3)** global
     `ollama_api_key` fallback. Each stage falls through on a resolution
     miss (e.g. an agent secret_id that no longer resolves logs a warning
     and falls back) rather than hard-failing.
6. **`GET /runtimes/compat-matrix`** (`routers/runtimes.py`) exposes
   `HARNESSES`/`HARNESS_LABELS` plus, per runtime, `compatible_harnesses`
   and per-incompatible-harness `incompat_reason` text — the frontend
   harness selector (runtime switch modal, add-runtime wizard) reads this
   instead of re-deriving compatibility client-side.
7. **Migration `0142` backfill** derives `harness` for every existing
   cli-bridge agent from its current runtime binding (mirrors
   `derive_harness`'s logic in SQL) so the rollout is a no-op for running
   agents; `harness` only gets materialized going forward on the **first**
   explicit switch that sets it, otherwise legacy rows stay `NULL` and
   keep resolving through the fallback chain indefinitely.

## Alternativen

- **Combination rows** (one `runtimes` row per harness × provider pair,
  e.g. `claude-vllm-docker`, `omp-vllm-docker`). Rejected — row explosion:
  every new provider would need N rows (one per compatible harness)
  instead of one, and the registry would duplicate provider connection
  data across rows that differ only in which CLI reads them.
- **Ship the LiteLLM-proxy shim immediately** (to make `claude` × OpenAI
  and `omp`/`openclaude` × Anthropic work in v1 by translating protocols).
  Rejected — a new always-on component with its own failure surface,
  unneeded today: no agent currently needs a cross-protocol combination,
  and the compat matrix already blocks the invalid pairing cleanly with an
  explanatory error instead of silently misrouting requests. Parked as v2.

## Konsequenzen

### Positiv
- Runtime rows are now **pure providers** — a `runtimes` row never again
  needs to encode "and also this is secretly a CLI harness", closing the
  exact gap the omp/Sparky case exposed.
- One shared classification module (`harness_compat.py`) instead of three
  independent slug-prefix checks that could silently drift from each
  other.
- Adding a new harness × provider combination in the future is a one-line
  `HARNESS_PROTOCOLS` edit, not a new `runtime_type` plus three new
  branch-points.
- Per-runtime API keys (`runtimes.api_key_secret_id`) let two agents on
  different harnesses share one provider row with different credentials
  where needed, instead of forcing a single global key.
- Legacy agents (`harness IS NULL`) keep working unmodified via
  `derive_harness` — no forced migration, no big-bang cutover.

### Negativ
- Two independent switch axes (`runtime_id`, `harness`) means the switch
  service, its snapshot/rollback, and the UI modal all carry twice the
  state to reconcile correctly — verified via router-level harness tests
  (see commit `46079dae`) covering the 422 case (harness-only patch
  without a runtime binding).
- `claude` × OpenAI and `omp`/`openclaude` × Anthropic are **not in
  scope** for v1 — an operator who wants Claude Code against a local vLLM
  model still can't do it until the LiteLLM-proxy shim (v2) exists.
  `incompat_reason` gives a clear German explanation rather than a bare
  422, but the capability itself is deferred.
- Migration `0142` adds one more nullable string column on the already-wide
  `agents` table.
- Hot-swap (changing the harness of a running container without any
  restart) is explicitly out of scope — a harness switch still goes
  through the existing image-change restart path (`force_recreate`), same
  cost as a cross-image runtime switch today.

## Referenzen

- Betroffene Dateien: `backend/app/services/harness_compat.py` (neu),
  `backend/app/services/agent_runtime_switch.py` (`new_harness` axis,
  effective-harness resolution, rollback),
  `backend/app/services/compose_renderer.py`
  (`pick_image_for_harness`, `detect_image_change`),
  `backend/app/routers/runtimes.py` (`GET /compat-matrix`, writable
  `api_key_secret_id`), `backend/app/routers/internal.py` (bootstrap uses
  `resolve_provider_credentials`), `backend/app/models/agent.py`
  (`harness` column), `backend/app/models/runtime.py`
  (`api_key_secret_id` column), `backend/alembic/versions/0142_agent_harness.py`,
  `docker/omp-bridge/entrypoint.sh` (renders `models.yml` `apiKey` when the
  resolved provider is keyed), frontend: `RuntimeSwitchModal` (harness
  selector + compat wiring), add-runtime wizard (api-key step), agent card
  harness badge, runtime card key badge.
- Verwandte ADRs: builds on ADR-027/ADR-028 (agent↔runtime binding,
  respawn/force-recreate switch modes), ADR-045/ADR-049 (omp runtime — the
  case that exposed the coupling), ADR-054 (runtime watcher — propagation
  paths this ADR's harness axis now also feeds into).
