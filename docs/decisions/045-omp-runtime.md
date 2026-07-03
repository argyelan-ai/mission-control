# ADR-045: `omp` Runtime Type — Clean-Stream Headless Agent (omp + Qwen)

- **Status:** Proposed — **drive model superseded by [ADR-049](049-omp-native-tui-session.md)** (native TUI + turn-end hook replaces the headless `omp -p` one-shot). The runtime *type*, `omp-qwen` row, OpenAI-compatible token routing, `models.yml` render, and `mc ack/finish/blocked` contract below remain in force; only §4 (headless subprocess) and §5 (readiness sentinel) are revised by ADR-049.
- **Date:** 2026-07-01
- **Deciders:** Operator (owner), Lead architect
- **Supersedes / relates to:** ADR-017 (runtime registry DB), ADR-024 (process recycling), ADR-027/028 (universal runtime binding + session propagation), ADR-041 (compose-renderer emits agent services)
- **Design doc:** [`docs/plans/omp-runtime-design.md`](../plans/omp-runtime-design.md)

## Context

Every MC agent today runs one of two harness images: `mc-claude-agent` (native
`claude` binary) or `mc-agent-base` (`openclaude` + OpenAI shim → an
OpenAI-compatible endpoint, e.g. Sparky → Qwen). Both are driven by the same
mechanism: an **interactive CLI pane in tmux Window 0**, a `poll.sh` loop
(`docker/shared/poll.sh`) that pastes the task prompt into that pane, and a
**screen-scrape supervisor** (`lib/turn-state.sh`, `paste-verify.sh`,
`ui-detect.sh`) that guesses "working / idle / crashed" from pane text.

That scrape supervisor is the source of the **silent-abort gap**: a turn (or the
whole run) can end without the task being complete, and nothing PATCHes a
terminal status. The task hangs `in_progress` for 15–60+ minutes until a backend
stale-check that does not even set `blocked`. The detector is heuristic
(stability = 6×5s unchanged pane) and fragile against model-picker dialogs,
partial paints, and non-deterministic redraws.

`omp` (`omp -p --mode json`) is a headless coding agent that emits a
**structured NDJSON lifecycle stream** (`session` → `agent_start` → `turn_end` →
`agent_end` with `stopReason`). It speaks OpenAI-completions natively, so it can
drive Qwen on the DGX Spark directly (no openclaude shim). A prototype adapter
already exists in this worktree at `docker/omp-bridge/`:

- `bridge.py` — **REAL, tested** NDJSON reducer → classifier → lifecycle mapper.
  Its `drive_run()` acks on the first stream line and always resolves into
  exactly one of `{finish, blocker}` — **there is no path that leaves a task
  `in_progress`**. Golden test green (12 passed) against real captured Qwen
  streams (`docker/omp-bridge/rpc/*.ndjson`).
- `Dockerfile` / `entrypoint.sh` / `omp-recycler.sh` — **SKETCHES** (inert
  `<<< >>>` markers, commented omp install) to be made real in Phase 2.

Proven-working invocation (local, read-only, omp v16.2.13 against Qwen vLLM at
`http://192.0.2.20:8000/v1`):

```
env -u LM_STUDIO_BASE_URL OMP_PROFILE=mc-agent \
  omp -p --model "qwen-spark/nvidia/Qwen3.6-35B-A3B-NVFP4" \
      --auto-approve --mode json --no-session "<task>"
```

We want a **third runtime type** so an agent (starting with Sparky) can be
switched onto omp: appear in `/runtimes`, be switchable via the standard
`switch_agent_runtime` path, and run omp headless driven by `bridge.py`.

## Decision

Introduce a new `runtime_type = "omp"` bound to a new harness image
`mc-omp-agent:latest`, and register a runtime row **`omp-qwen`** pointing at the
DGX-Spark Qwen vLLM endpoint. Concretely:

1. **New runtime type `omp`**, display name *"omp headless (Qwen)"*. It is
   OpenAI-compatible at the transport level, so it routes through the **existing
   OpenAI-style single code path** — it does **not** get anthropic tokens.

2. **Runtime row `omp-qwen`** (seeded, insert-only, idempotent):
   `runtime_type=omp`, `endpoint=http://192.0.2.20:8000/v1`,
   `model_identifier=nvidia/Qwen3.6-35B-A3B-NVFP4`, `supports_tools=true`,
   `enabled=true`. The slug deliberately does **not** start with
   `anthropic-claude-`, so `build_runtime_env` and `docker_agent_sync` emit
   `OPENAI_BASE_URL` + `OPENAI_MODEL` (+ `OPENAI_API_KEY`) — the correct
   OpenAI-compatible routing. `runtime.endpoint` stays the single source of
   truth for the URL.

3. **Three routing branch-points** learn `omp` (no duplicated token logic):
   - `compose_renderer.pick_image_for_runtime` → maps `runtime_type=="omp"` to
     the new `mc-omp-agent:latest` image (today it returns `None` for `omp`,
     which breaks the switch's image-change detection).
   - `internal.build_runtime_env` → an explicit `omp` branch (mirroring the
     `hermes` branch) that emits `OPENAI_BASE_URL` + `OPENAI_MODEL`. Gives us a
     clean hook for `OMP_PROFILE` without slug-prefix routing, and keeps the
     anthropic branch untouched.
   - `docker_agent_sync` (`is_anthropic` + `.env` render) → **no new branch**:
     the non-anthropic slug already takes the OpenAI branch. This is the "do not
     duplicate token routing" guarantee.

4. **Headless driver:** the `mc-omp-agent` entrypoint boots the same 3-window
   tmux + bootstrap pattern, but Window 0 runs `python3 bridge.py --serve` (a new
   persistent poll loop to be written) instead of an interactive pane. omp is a
   short-lived subprocess of `bridge.py`. `poll.sh` and the entire screen-scrape
   supervisor are **not used** for omp — `bridge.py`'s deterministic classifier
   replaces them.

5. **Readiness re-anchor (sentinel-gated, image-change-independent).** Headless
   omp emits no interactive glyph, so readiness gates on an `OMP_BRIDGE_READY`
   sentinel scraped from the Window-0 pane. **Crucially, this must fire on the
   cross-image switch path, which the naive change misses.** `switch_agent_runtime`
   calls `wait_for_agent_healthy(respawn_mode=(not image_change))`
   (`agent_runtime_switch.py:495-499`); the first Sparky openclaude→omp switch is
   cross-image ⇒ `respawn_mode=False` ⇒ `wait_for_agent_healthy` uses
   `docker inspect …==running` (`docker_agent_sync.py:805-815`), which **never**
   scrapes the pane. `_wait_for_window_ready` (and its glyph tuple at
   `docker_agent_sync.py:585`) is only reached on `respawn_mode=True`
   (same-image omp→omp respawns). Consequence: the container-running check reports
   SUCCESS *before* `bridge.py` bootstraps, and — because tmux is PID 1 with a
   window watchdog — a crash-looping Window 0 keeps the container `running`, so a
   dead runtime is falsely reported healthy with **no rollback**. Fix: thread a
   `ready_signals` param through `wait_for_agent_healthy` **and**
   `_wait_for_window_ready`; when the target runtime is omp, pass
   `("OMP_BRIDGE_READY",)` and route to the pane scrape **regardless of
   `image_change`** (sentinel-only match — the default `$ `/`> ` glyphs can appear
   in bridge logs and would false-positive). Only then does readiness actually gate
   the switch and roll back on a stuck bridge. Verified by an intentional
   sentinel-break rollback test (design §6 step 4b).

6. **Config delivery:** omp resolves models profile-first
   (`OMP_PROFILE=mc-agent` → `$HOME/.omp/profiles/mc-agent/agent/models.yml`).
   The entrypoint **renders `models.yml` at boot** from `OPENAI_BASE_URL` /
   `OPENAI_MODEL` (a dedicated `qwen-spark` provider, `auth: none`). This keeps
   MC's env contract as the single source of truth while giving omp its native
   provider config (omp's built-in `openai` provider does **not** resolve a
   vLLM-served model from `OPENAI_BASE_URL`, so a `models.yml` is mandatory).

All production actions (image build, runtime registration, agent switch) are
**gated** and performed by the operator later — this ADR + the design doc make them
*ready*, with exact commands.

## Alternatives considered

- **Keep openclaude, fix the scrape supervisor.** Rejected: the silent-abort
  gap is structural to screen-scraping a non-deterministic interactive pane.
  omp's structured stream removes the guesswork entirely (`bridge.py` proves it).
- **Reuse `runtime_type=openai_compatible` for omp.** Rejected: image selection
  and readiness both branch on the type. Overloading `openai_compatible` would
  pick the openclaude image + glyph readiness — wrong binary, wrong health
  anchor. A distinct type keeps the three branch-points honest and greppable.
- **Route omp via `LM_STUDIO_BASE_URL` env hijack** (the alternate proven path).
  Rejected as the default: it hijacks omp's built-in `lm-studio` slot (silent
  `127.0.0.1:1234` fallback if unset — a footgun) and relies on a mutable env.
  A baked/rendered `models.yml` `qwen-spark` provider survives restarts, is
  discovery-free, and needs no key (`auth: none`). Env path kept documented as a
  fallback.
- **New backend lifecycle endpoint for omp.** Rejected: `bridge.py` shells out
  to the copied `mc` CLI (`mc ack` / `mc finish` / `mc blocked`), reusing the
  exact same lifecycle contract as every other agent. No new endpoint.
- **`mc failed` on abort.** Rejected (mirrors bridge policy): `failed`
  auto-unassigns with no human trace; `blocked` is reversible and visible.

## Consequences

**Positive**
- Silent-abort closed: every omp run resolves terminally (`finish` | `blocked`).
- Deterministic supervision replaces ~3 fragile scrape scripts (`turn-state.sh`,
  `paste-verify.sh`, `ui-detect.sh`) for omp agents.
- Native Qwen driving without the openclaude shim; `runtime.endpoint` stays the
  single URL source; zero duplicated token routing.
- Reversible: remove the seed row / delete the DB row / switch the agent back;
  the openclaude and claude images are untouched.

**Negative / risks (tracked in the design doc §Risks)**
- **`bridge.py --serve` does not exist yet** — the poll loop + ack-dedup +
  real `mc`-CLI lifecycle (replacing `LoggingLifecycle`) is the load-bearing 80%.
  `entrypoint.sh:35` invokes `--serve`, which today's argparse rejects
  (`SystemExit`), so the harness crash-loops until it ships. Mitigation: **port**
  the shipped `scripts/hermes-bridge.py:233-341` poll loop (identical `/me/poll`
  contract, dispatch-dedup cache, SIGTERM handling) and swap only the delivery
  step — hermes pastes into a tmux pane, omp instead spawns a subprocess through
  `bridge.py`'s existing `drive_run()`. Ack-dedup is mandatory because `/me/poll`
  deliberately does not set `ack_at` (`agents.py:2077-2083`), so it re-emits
  `new_task` on every poll until the bridge acks. Do not write the loop from
  scratch.
- **Prompt must be wrapped** to emit the `TASK_COMPLETE` sentinel + 4-field
  German reflection, or every run classifies as `silent_abort_no_sentinel`
  (blocker). Live sentinel/reflection reliability on Qwen is a hard Phase-2 gate.
- **cwd delivery** is new coupling: `bridge.py` must pass the *container-view*
  workspace path (`dispatch._container_workspace_path`) as `omp --cwd`, and
  handle `workspace_path=null` (ad-hoc tasks).
- **Sessions UX shift:** the live terminal shows `bridge.py` structured logs
  instead of an interactive pane; cancel/stop must SIGKILL the omp subprocess
  (no pane ESC analog).
- **Recycler fork** (`omp-recycler.sh`) must track `bridge.py` (long-lived), never
  the short-lived omp; `bridge.py` must manage `.task-active.lock` around each run.
- Requires the MC-convention paperwork: this ADR + `docs/ARCHITECTURE.md` update
  (§6 Runtime Registry gains the `omp` type + the omp image row).
