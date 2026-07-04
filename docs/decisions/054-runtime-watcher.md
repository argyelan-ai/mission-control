# ADR-054 — Runtime Watcher: periodic model-drift probing (supersedes D-22)

**Status:** Accepted
**Datum:** 2026-07-05
**Scope:** Backend/Runtime · Backend/DB · Backend/Services · Frontend/Runtimes

## Kontext

"Runtime & Model Management v1" (`docs/plans/2026-07-04-runtime-model-management-design.md`)
set the goal "engine leads, MC follows": when an operator swaps the model
directly at the inference engine (vLLM reload, LM Studio load/unload, a
different OpenAI-compatible upstream), Mission Control should notice and
re-align the `runtimes` row and every bound agent — without the operator
having to click a re-probe button or edit anything by hand.

This is exactly the case Phase 16 rejected. ADR-028
(`docs/decisions/028-runtime-registry-and-session-propagation.md`) recorded
**D-22**:

> **Periodisches Background-Probing aller Runtimes.** Verworfen (D-22) —
> Cost/Benefit ungünstig, der Operator probet bei Bedarf manuell. Re-probe-
> Button macht das auf einen Klick zugänglich.

At the time, a manual re-probe button was judged sufficient because the
common case was an operator-initiated MC-side switch, not an out-of-band
engine change. Since then, direct engine-side model swaps (bring your own
vLLM reload, LM Studio hot-swap) became a normal workflow on the DGX Spark,
and a manual button means the Runtime Registry silently drifts from reality
until someone remembers to click it — the exact opposite of "engine leads,
MC follows". That property is unachievable without an actively observing
loop; a manual-only re-probe cannot detect drift it isn't told to look for.

## Entscheidung

**`RuntimeWatcher`** (`backend/app/services/runtime_watcher.py`) is a
singleton background loop, same lifecycle pattern as `IntelligenceService`
(asyncio task, Redis lock for multi-worker dedup, `settings.runtime_watcher_enabled`
kill-switch, `settings.runtime_watcher_interval` default 90 s).

Every tick:

1. **Probe** every `enabled` runtime whose `runtime_type` is probeable
   (`vllm_docker`, `lmstudio`, `openai_compatible`, `unsloth` —
   `anthropic_*`/`cloud` are skipped). A probe is a single cheap
   `GET {endpoint}/v1/models` (reuses the existing `probe_runtime_model`
   helper from Phase 15), timeout ~5 s. This is the "billiger GET auf
   LAN-Endpoints" the design doc bets on — the cost side of D-22's
   cost/benefit call no longer holds once the check is this cheap.
2. **Publish live status** to Redis (`mc:runtime-live:{slug}`, TTL = 3×
   interval): `reachable`, `served_model`, `latency_ms`, `last_probe_at`.
   This feeds the `/runtimes` cockpit live-dot regardless of drift.
3. **Confirm drift with two consecutive identical probes** before acting.
   A single mismatched probe is stored as a Redis-cached "candidate"
   (`mc:runtime-drift-candidate:{slug}`, TTL = 3× interval); only when the
   *next* probe returns the same served model does the watcher commit it.
   This guards against flapping during engine warm-up/reload — exactly the
   failure mode a naive "act on first mismatch" probe would hit.
4. **On confirmed drift:** persist the new `model_identifier` on the
   `runtimes` row, invalidate the resolver cache
   (`runtime_model_resolver.invalidate_cached_model`), emit
   `runtime.model_changed` (old → new), and flag every bound cli-bridge
   agent `pending_runtime_sync = true` (`runtime_propagation.mark_agents_for_sync`,
   new column, migration `0141`). Host agents (Boss/Hermes/Jarvis,
   launchd-managed) are skipped — the activity event is their only signal.
5. **Down-detection** is separate from drift: an unreachable endpoint only
   updates live status; `runtime.unreachable` fires after 3 consecutive
   failed probes (no event-spam on a single blip), and the row is left
   untouched (no false "drift to nothing").
6. **Every tick ends with a propagation sync pass**
   (`runtime_propagation.sync_pending_agents`) over every flagged agent
   that is currently idle.

**Propagation is a plain `docker restart`, not `respawn_window_only`.**
`respawn_window_only` (ADR-028, the same-image fast path used by manual
runtime switches) only respawns the tmux window — the container process and
its exported environment survive, so a stale `OPENAI_MODEL` would keep
being exported into the new window. A `docker restart` re-runs the
container entrypoint, which re-calls `/internal/bootstrap` and receives the
fresh `OPENAI_MODEL`/`OPENAI_BASE_URL` from the DB row (for the omp image
this also re-renders `models.yml` and the model selector). That is the
entire reason this path restarts instead of respawning.

**Busy agents stay flagged until a later watcher tick — no `task_lifecycle`
hook.** An agent with `current_task_id` set is skipped by
`sync_pending_agents` and simply stays `pending_runtime_sync = true`; the
next tick (≤ `runtime_watcher_interval`, i.e. ≤ 90 s after the task ends)
picks it up. Wiring a dedicated hook into `task_lifecycle` was considered
and rejected — the watcher already runs a sync pass every tick, so a hook
would only shave off a worst-case 90 s latency at the cost of a new
coupling into the task-completion path. `/runtimes` shows the `pending
sync` badge and a force-sync action for operators who don't want to wait.

**Circuit breaker:** each failed sync attempt increments a Redis counter
(`mc:agent:{id}:model-sync-fails`); after `MAX_SYNC_ATTEMPTS = 3` the flag is
cleared, `agent.model_sync_failed` is emitted, and the agent is left as-is
(no restart-loop against a broken container). A successful sync clears the
counter and emits `agent.model_synced`.

**Force-sync route:** `POST /runtimes/db/{slug}/sync-agents` lets an
operator sync all bound agents immediately (busy included, via `force=True`)
instead of waiting for the next tick or task end.

## Alternativen

- **Keep D-22 as-is (manual re-probe only).** Rejected — this is precisely
  the case D-22 didn't anticipate: an engine-side model swap the operator
  didn't perform through MC leaves the registry silently stale until
  someone happens to click re-probe. Directly contradicts "engine leads,
  MC follows".
- **Act on the first probe mismatch (no two-probe confirmation).** Rejected
  — a vLLM reload or LM Studio load/unload transiently serves no model or
  an intermediate one; acting on the first sighting would fire spurious
  `runtime.model_changed` events and unnecessary agent restarts during
  normal engine operations.
- **`respawn_window_only` for propagation (reuse the ADR-028 fast path).**
  Rejected — it does not re-run the entrypoint, so the stale
  `OPENAI_MODEL` exported into the tmux environment would survive the
  respawn. A full `docker restart` is the only path that re-triggers
  `/internal/bootstrap`.
- **`task_lifecycle` hook for busy-agent sync.** Rejected — the watcher's
  own per-tick sync pass already retries pending agents; a dedicated hook
  only trades a ≤90 s latency win for a new coupling between task
  completion and runtime propagation, adding a failure surface without a
  clear benefit.
- **Sub-30 s polling interval.** Rejected — 90 s keeps LAN GET volume
  negligible while still being fast enough that drift-to-detection latency
  is imperceptible relative to normal task durations; the design explicitly
  avoids scope creep into MC-side engine control (still out of scope, see
  the design doc's "Nicht im Scope").

## Konsequenzen

### Positiv
- `/runtimes` reflects what the engine is actually serving, not just what
  MC last wrote — closes the exact gap D-22 left open.
- Drift and propagation are fully automatic for idle agents (no operator
  action) and bounded to one extra tick (≤90 s) for busy agents.
- Two-probe confirmation + down-detection thresholding keeps the activity
  feed quiet during normal engine warm-up/restart noise.
- Circuit breaker prevents a broken container from being restart-looped
  by the watcher.
- omp's hardcoded Spark model defaults are removed as part of this work —
  `model_identifier` starts `null` in seeds and is filled by the first
  probe, matching "engine leads" end-to-end.

### Negativ
- One more always-on background loop (per-worker Redis lock, `2×N` extra
  HTTP requests per interval where `N` = probeable runtimes) — negligible
  at current fleet size, worth re-checking if the runtime count grows by
  an order of magnitude.
- Busy-agent sync latency is bounded by the watcher interval (≤90 s), not
  by task completion directly — an intentional trade-off (see Alternativen)
  that trades a small window for coupling avoidance.
- A `docker restart` is heavier than `respawn_window_only` (full container
  bootstrap vs. tmux respawn) — but that's the entire point: only a full
  restart guarantees a fresh `OPENAI_MODEL`.
- Migration `0141` adds `agents.pending_runtime_sync` (nullable-safe bool,
  default false) — one more column on an already-wide table.

## Referenzen

- Betroffene Dateien: `backend/app/services/runtime_watcher.py`,
  `backend/app/services/runtime_propagation.py`,
  `backend/app/services/agent_runtime_switch.py` (probe helpers, reused),
  `backend/app/routers/runtimes.py` (`GET /live-status`,
  `POST /probe-endpoint`, `POST /db/{slug}/sync-agents`),
  `backend/app/routers/agents.py` (`GET /agents/{id}/runtime-switch-progress`),
  `backend/app/alembic/versions/0141_*` (`agents.pending_runtime_sync`),
  `docker/omp-bridge/entrypoint.sh`, `docker/omp-bridge/launch-omp.sh`,
  `backend/config/runtimes.json`, `docker/omp-bridge/register-omp-runtime.sh`
  (provider renamed `qwen-spark` → `mc-openai`, `model_identifier: null`).
- Design doc: `docs/plans/2026-07-04-runtime-model-management-design.md`
- Verwandte ADRs: supersedes D-22 (`docs/decisions/028-runtime-registry-and-session-propagation.md`);
  builds on ADR-017 (Runtime Registry in DB), ADR-018 (switch via restart),
  ADR-027/ADR-028 (agent↔runtime binding, `respawn_window_only` fast path
  this ADR deliberately does *not* reuse), ADR-045/ADR-049 (omp runtime).
