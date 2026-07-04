# ADR-049: omp Native-TUI Session — Real Scrollable omp CLI on the Sessions Page

- **Status:** Proposed
- **Date:** 2026-07-04
- **Deciders:** Operator (owner), Lead architect
- **Supersedes / relates to:** **Supersedes the DRIVE MODEL of [ADR-045](045-omp-runtime.md)** (the headless `omp -p --mode json` one-shot). ADR-045's runtime *type* (`runtime_type="omp"`), the `omp-qwen` runtime row, the OpenAI-compatible token routing, the `models.yml` render, and the `mc ack/finish/blocked` lifecycle contract all **remain in force**. Relates to ADR-024 (recycling), ADR-046 (lifecycle safety watchdog).

## Context

ADR-045 shipped the `omp` runtime as a **headless** driver: `bridge.py --serve`
ran in tmux Window 0 and spawned a fresh `omp -p --mode json` subprocess per
task, reducing its NDJSON stdout to a lifecycle decision. That closed the
silent-abort gap, but it changed the **Sessions-page UX**: the live terminal
showed `bridge.py` structured logs, not an interactive agent. Every other agent
(claude, openclaude) shows a real, scrollable, human-readable CLI session on the
Sessions page. The omp agent should too — a human watching Sparky work should
see omp *thinking, reading files, and answering*, not a JSON reducer's logs.

The blocker for a native TUI had been a Bun `fetch failed` at model-call time —
but that was verified to be a **macOS-local** artifact only; inside the Linux
`mc-omp-agent` container the native TUI renders full ANSI chrome **and** reaches
Qwen. Every mechanism below was proven hands-on in a throwaway container against
the real DGX-Spark Qwen (`nvidia/Qwen3.6-35B-A3B-NVFP4`).

## Decision

Rework the `mc-omp-agent` runtime so **tmux Window 0 runs the real native omp
TUI** (what the Sessions page attaches to), driven **non-brittly** by the bridge
from a separate window — replacing the headless one-shot. Concretely:

1. **Window layout.** Window 0 = the native TUI
   (`omp --hook … --model qwen-spark/<model> --cwd <task-cwd> --approval-mode yolo`,
   via `launch-omp.sh`). Window 1 = `bridge.py --serve` (the poll driver).
   Window 2 = the recycler (now tracks **both** the TUI and the bridge).

2. **Wizard skip.** The TUI must boot **straight to the chat prompt**. Verified:
   `omp config set startup.setupWizard false` + `omp config set setupVersion 1`
   (a hand-written `config.yml` is NOT honored — omp normalizes its own store).
   The entrypoint runs both before the TUI starts.

3. **Completion via hook, not screen-scraping.** `omp --hook turn-end-hook.mjs`
   loads an ESM hook (`export default (api) => { api.on('turn_end', …) }`) that
   appends one JSON line per lifecycle event to a signal file the bridge tails.
   `turn_end` fires on every turn incl. errors; `message.stopReason ∈
   {stop, toolUse, error, aborted, length}`. Mapping: a **non-toolUse** turn is
   terminal for the user message — `stop` → apply the completion contract
   (finish|silent-abort), `error`/`aborted` → error family, `toolUse`/`length` →
   keep waiting (agentic loop / auto-compaction). The reduced `RunOutcome` flows
   through the **unchanged** `classify()` / `decide_lifecycle()` /
   `drive_live_run()` — same taxonomy, same `mc ack/finish/blocked` + the
   `finish → blocked` fallback.

4. **Task injection via `@file` send-keys.** The multi-line dispatch is written
   to `$OMP_HOME/tasks/task-<id>.md` and injected as an `@/abs/path` mention
   (`tmux send-keys`), never pasted. Verified sequence (the `@` opens a
   file-mention autocomplete popup that eats a bare Enter): type `@path` →
   `Escape` (dismiss popup, keep text) → `Enter` (submit) → omp `Read`s the file.

5. **Per-task isolation via TUI relaunch.** Between tasks the bridge
   `tmux respawn-window -k`s Window 0 with the new task's `--cwd`. This is the
   isolation mechanism **and** the cwd rebind (omp's `/new` slash-reset cannot
   change cwd) **and** the fresh-context reset. A brief visible relaunch on the
   Sessions page is acceptable.

6. **Silent-abort watchdog (non-negotiable).** If no terminal `turn_end` arrives
   within the per-task deadline, a no-progress idle timeout trips, or the TUI
   child dies, the bridge **SIGKILLs + relaunches** the TUI
   (`respawn-window -k`) and the task ends **blocked** (`ABORT_HANG`), never left
   `in_progress`. Verified: a 4s deadline against a long task yields
   `watchdog_killed=True → abort_hang → blocker`, with a fresh
   `session_start` proving the relaunch.

7. **Readiness re-anchor (supersedes ADR-045 §5).** Window 0 now runs the TUI,
   so its readiness anchor is the **TUI chat glyph** (`╭─` / `❯` / `> ` in the
   visible pane — the same anchor every interactive agent uses), **not** the
   `OMP_BRIDGE_READY` sentinel (which the headless bridge printed into Window 0
   and now prints into Window 1). This implies a **one-line backend change**:
   for `runtime_type=="omp"`, `switch_agent_runtime` should pass the default
   glyph `ready_signals` (or `("╭─", "❯")`) instead of `("OMP_BRIDGE_READY",)`
   (see `agent_runtime_switch.py`). That change is outside this rework's file
   scope and is tracked as the single required backend follow-up.

## Alternatives

- **Keep the headless one-shot (ADR-045).** Rejected: it works but shows JSON
  logs on the Sessions page, not a native session — the whole point of this
  rework.
- **Screen-scrape the TUI for completion.** Rejected: that is exactly the
  fragile heuristic ADR-045 removed. The `turn_end` hook is a structured,
  deterministic oracle — no pane-text parsing.
- **Paste the full dispatch body via send-keys.** Rejected: multi-line paste
  risks bracketed-paste corruption and re-triggers autocomplete. `@file` pulls
  the body cleanly and keeps the injected keystrokes tiny.
- **`/new` slash-reset for isolation (same process).** Rejected as the default:
  it cannot rebind `--cwd` per task. Relaunch does isolation + cwd + fresh
  context in one step. `/new` is kept exposed (`OMP_ISOLATION=slash`) for
  same-cwd reuse.
- **Idle-timeout as the primary watchdog.** Rejected as primary: a legitimately
  long single generation emits no `turn_end`, so the hard per-task deadline +
  child-liveness are primary; idle is a generous secondary guard fed by the
  hook's `turn_start`/`tool_execution_end` progress markers.

## Consequences

**Positive**
- The Sessions page shows the **real native omp session** — parity with claude /
  openclaude agents; a human can read and scroll omp working against Qwen.
- Completion stays **deterministic** (hook `turn_end`, not scraping) and reuses
  the proven `classify()` taxonomy — every run still resolves to
  `finish | blocked`, never left `in_progress`.
- Per-task isolation + correct `--cwd` come free from the relaunch.
- The watchdog is strictly stronger: child-death, wall-clock, and idle all
  trigger SIGKILL + relaunch + terminal block.

**Negative / risks**
- **One backend follow-up** (the `ready_signals` re-anchor, §7) is required for
  the switch health-gate to pass on the native TUI; until then a same-image
  respawn's health check would look for `OMP_BRIDGE_READY` in the TUI pane and
  time out. Small, isolated, reversible.
- A visible **per-task relaunch** flashes the Sessions pane briefly.
- The `@file` inject depends on omp's autocomplete behavior (mitigated by the
  `Escape`-then-`Enter` sequence, verified on omp v16.2.13).
- Live Qwen reflection-format reliability (the `TASK_COMPLETE` + 4-field block)
  remains the same gate as ADR-045; a `stop` without it is correctly caught as a
  silent-abort blocker.
