# ADR-059 — Solo-Capability-aware Recipe Switching (Engine Control)

**Status:** Accepted
**Datum:** 2026-07-06
**Scope:** Infra/Runtime · Backend/DB

## Kontext

The DGX Spark host has exactly **1 GPU** (GB10). `sparkrun` — the recipe CLI
that drives vLLM containers on the Spark — ships multiple registry variants
of the same model: an `@official` variant tuned for a single GPU (tensor
parallel size `tp=1`, `nodes=1`) and `@eugr`/`@community` variants that
default to `tp=2`/`tp=4` and a `vllm-ray` distributed-executor backend for
multi-GPU clusters.

Mark switched Mission Control's `/runtimes` recipe-switcher to
`@eugr/qwen3.6-35b-a3b-fp8` and got "engine unreachable". Root cause: MC's
recipe machinery had no concept of solo- vs. cluster-capable recipes.

1. `services/sparkrun_manager.list_recipes()` parsed only `name`/`model`/
   `registry` from `sparkrun list` and **discarded the TP and Nodes
   columns** — the exact signal that distinguishes a 1-GPU-safe recipe from
   one that needs a cluster. The UI offered every recipe as if it were
   equally startable.
2. `build_launch_command()` hardcoded `--solo --no-rm --ensure --no-follow`
   but never touched `--tensor-parallel`. `--solo` only controls sparkrun's
   ray/node bootstrap — **not** the tp value baked into the recipe. A
   `tp=2` recipe stayed `tp=2` and failed to come up on a 1-GPU box.
3. Compounding failure: `runtime_manager.start_runtime()` only verified that
   *a container* appeared (`verify_spark_container_started`), not that vLLM
   actually started serving inside it. Some launches keep a `sleep infinity`
   PID1 while vLLM runs as a separate, out-of-band process — that process
   can die (OOM, bad flags) while the container itself stays "running",
   and the old check reported success anyway.

## Entscheidung

`sparkrun_manager.list_recipes()` now parses `tp` and `nodes` from the
`sparkrun list` output and derives `solo_capable` against the **actual**
number of GPUs on the target host (`get_host_gpu_count()`, via
`nvidia-smi -L | wc -l` — never hardcoded, so the same logic holds on a
future multi-GPU box). `switch_recipe()` consults this before touching
anything:

- **Recipe needs >1 physical node** (`nodes > 1`): this single-host
  deployment can never satisfy that — abort the switch **before** evicting
  the currently-running model, with a clear message and a
  `runtime.recipe_switch_rejected` activity event. An unwinnable switch must
  not kill a healthy engine first.
- **Recipe needs more GPUs than the host has, but only 1 node**
  (`tp > host_gpu_count`, `nodes <= 1`): inject a downscaled
  `--tensor-parallel <host_gpu_count>` via `build_launch_command(...,
  tp_override=...)` and proceed. Whether the model actually fits in less
  VRAM at that tp is something only vLLM itself can determine — that's
  exactly what the new post-launch process check (below) exists to catch
  honestly instead of silently.
- **Recipe not found in `sparkrun list`** (unknown/local name) or the
  **guard itself is unavailable** (SSH/list failure): proceed without a
  guard rather than block on missing information — the readiness check
  below is the real safety net either way.

Separately, `runtime_manager.start_runtime()` gained a second post-launch
check, `verify_spark_vllm_process_started()`: after the container-existence
check passes, it polls `docker top` on that container for an actual
`vllm serve` process (reusing the same scan `_container_runs_vllm_server`
already does for discovery) before reporting success. This closes the
original incident's silent-failure mode — sparkrun exits 0 (fire-and-forget
`--no-follow`), the container is "running", but nothing is actually
listening.

## Alternativen

- **Hardcode `--tp 1` for every Spark launch.** Verworfen: breaks recipes
  that genuinely need `tp>1` and are correctly configured for it (once a
  second GPU is added, or on a different host); the whole point of ADR-048's
  host registry is that MC no longer assumes a single fixed box.
- **Filter cluster-only recipes out of the dropdown entirely (no override).**
  Verworfen: this hides the `@eugr`/`@community` variants completely even
  when a tp-downscale override might work fine (same weights, just built for
  a ray cluster by default) — better to attempt it and catch a real failure
  honestly via the readiness check than to guess it away.
- **Predict tp-override feasibility from the `GPU-Mem` column instead of
  attempting + verifying.** Verworfen: that column reports memory usage
  *at the recipe's own declared tp*, not a directly comparable
  cross-variant total — trying to out-guess vLLM's own VRAM budgeting from a
  static list output is exactly the kind of "MC thinks it knows better than
  the engine" mistake ADR-054 ("Engine leads, MC follows") already rejected
  once. The actual launch + the new process-liveness check are the ground
  truth.

## Konsequenzen

### Positiv
- A recipe switch that can never succeed (multi-node) is rejected instantly
  instead of killing the running model first.
- A recipe that's merely oversized for tp gets a best-effort, transparent
  downscale attempt instead of a silent, unexplained failure.
- The container-running-but-vLLM-dead failure mode (the actual incident) is
  now caught and reported with a clear message, not a mysterious
  "unreachable" 5 minutes later.
- `/runtimes`' recipe dropdown now shows tp/nodes and visually flags
  non-solo-capable recipes before the operator even clicks one.

### Negativ
- The tp-override path is still best-effort — MC cannot statically prove a
  downscaled tp actually fits in VRAM; a bad override still costs an
  evict+relaunch cycle before the process check reports the failure.
- Two extra SSH round-trips per recipe-list fetch (`nvidia-smi` +
  `sparkrun list`) and one extra `docker top` poll per launch — negligible
  (lazy-fetched only when the dropdown opens / once per launch), but worth
  remembering if this ever needs to run on a tighter interval.
- `get_host_gpu_count()` falls back to `1` on any SSH/parse failure — a
  transient SSH blip during the guard silently assumes the most
  conservative single-GPU case rather than surfacing the uncertainty to the
  operator.

## Referenzen

- Betroffene Dateien: `backend/app/services/sparkrun_manager.py`,
  `backend/app/services/runtime_manager.py`,
  `backend/app/services/docker_agent_sync.py` (restart-path safety guard),
  `backend/tests/test_sparkrun_manager.py`,
  `backend/tests/test_spark_runtime_eviction.py`,
  `backend/tests/test_agent_restart_never_targets_runtime_container.py`,
  `frontend-v2/src/lib/types.ts`, `frontend-v2/src/lib/api.ts`,
  `frontend-v2/src/components/shared/SparkRecipeSwitcher.tsx`
- Verwandte ADRs: ADR-036 (`runtimes.launch_command`), ADR-048 (Host
  Registry — `get_host_gpu_count` is host-scoped through the same
  `ResolvedHost` chain), ADR-054 (Runtime Watcher, "Engine leads, MC
  follows" — the same principle behind not statically out-guessing VRAM
  feasibility), ADR-057 (Engine Control v0 autostart flag)
- Auslöser: 2026-07-06 Spark solo-capability incident (`@eugr/qwen3.6-35b-a3b-fp8`
  switch, "engine unreachable")
