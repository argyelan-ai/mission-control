# ADR-086 — Harness and runtime are freely and cross-wise switchable; local first

**Status:** Accepted · amends ADR-085 (§6 rule 2, §7 decision 1, §8 stop 1)
**Datum:** 2026-09-23
**Scope:** Architecture/Direction · Infra/Runtime · Agent Protocol · Docs

## Context

ADR-085 made "a head per job" the normal path: one short-lived coding-CLI
session per job, in its own worktree, following a procedure written as files.
Two of its rules bind that path to one vendor and push the operator's own
hardware to the edge:

- **House rule 2 (ADR-085 §6):** "Procedure and data are portable, the
  operator surface is not." In practice this made one vendor's coding CLI
  (its remote start, goal loop, push questions, session list) the only way to
  start and watch a head.
- **Decision 1 (ADR-085 §7) and stop 1 (§8):** "Local GPU hosts move from
  main engine to helper and fallback", with "Is the helper role of the GPU
  hosts acceptable?" as a stop-1 question.

On 2026-09-23 the operator set a principle that contradicts both: **harness
and runtime must be switchable at any time, and cross-wise** — any coding
harness with any model runtime, as far as technically possible — and **local
AI is a main component, not a side role.** The reasoning: models, engines and
harnesses change too fast to bind the working model to one of them, and after
the quiet mode of ADR-085 no local model was doing any work at all.

What already exists, and what this principle builds on:

- **The harness × protocol matrix (ADR-056, consolidated by ADR-084)** in
  `backend/app/services/harness_compat.py`: `HARNESS_PROTOCOLS` (line 74)
  says which wire protocol each harness speaks, `runtime_protocol()`
  classifies each runtime row, `is_compatible()` (line 125) and
  `incompat_reason()` (line 132) answer and explain every pair, and
  `HARNESS_CAPABILITIES` (line 296) records what each harness can take
  (skills, plugins, settings extras).
- **The runtimes table** with the local GPU-box engines, a hosted
  OpenAI-compatible cloud and Claude; the box manager and recipe switcher
  (ADR-077/078) that start and stop local engines.
- **Runtime switching per agent** (`agent_runtime_switch.py`) and the omp
  ACP driver (ADR-081/084).
- **A live probe on 2026-09-23:** the omp harness in its print mode, pointed
  at a local mid-size model on a GPU box, solved a small job in 38 s. The
  endpoint came from the runtimes table, not from a hard-coded address.

What the matrix says today (code, not guesswork):

| Harness ↓ · Runtime → | Local GPU box (OpenAI-compatible) | Hosted OpenAI-compatible cloud | Claude (Anthropic, subscription login) | Vendor-fixed (kimi, grok) |
|---|---|---|---|---|
| **Claude Code** (`claude`) | no — needs a protocol proxy (`incompat_reason`, line 138) | no — same | **yes** | no |
| **OpenClaude** (`openclaude`) | **yes** | **yes** | no — subscription login is Claude-Code-only (`resolve_provider_credentials`, line 177) | no |
| **omp** (`omp`) | **yes** | **yes** | no — same | no |
| **Kimi Code** (`kimi`) | no | no | no | own endpoint only (line 65 ff.) |
| **hermes** (host) | **yes** | **yes** | no | no |
| **grok** (host) | no | no | no | own endpoint only (line 59 ff.) |

So "cross-wise" works today for every OpenAI-speaking harness against every
OpenAI-compatible runtime, and for Claude Code against Claude. The two pairs
most often asked for do **not** work yet: *Claude Code × a local model* (needs
a protocol proxy, which PRINCIPLES §8 rule 5 excludes) and *omp × Claude*
(the subscription login is bound to Claude Code). OpenClaude is the existing
route for a Claude-Code-like harness on a local model.

## Decision

**Every head is a pair "harness × runtime", chosen per job and changeable at
any time — including cross-wise, within the pairs the compatibility matrix
allows. Local runtimes come first wherever they are good enough; the cloud is
the fallback, not the default.**

1. **The pair is the unit.** A job names its harness and its runtime. Both can
   be changed between runs ("restart with another pair") without touching the
   procedure, the worktree rules or the run record format.
2. **The matrix is the single source of truth.** Which pairs work is answered
   only by `harness_compat.py`. UI, launcher and docs read it; nobody keeps a
   second list. A missing pair is shown with its reason (`incompat_reason`),
   never hidden or guessed.
3. **Local first.** The launcher pre-selects a local runtime. A cloud runtime
   is chosen when the job needs it (skill-dependent procedure, capacity,
   quality) or when the local engine is down — and the run record says which
   and why. GPU boxes are a main engine for heads, not only a helper.
4. **Procedure is harness-neutral.** The core of the head procedure lives in
   `AGENTS.md` plus the start prompt, so it works on harnesses without skill
   support (`cli_skills = False` for omp, kimi, hermes, grok in
   `HARNESS_CAPABILITIES`). A vendor skill may add convenience on top, never
   carry rules that only it knows.
5. **The operator surface is swappable too.** Vendor-only features (remote
   start, goal loop, push questions, session list) are never the only way.
   MC offers one neutral path for all harnesses: start a head for a task,
   see its status and heartbeat, stop it, restart it with another pair, and
   read the result (PR plus run record) on the existing task detail.
6. **Closing a gap is its own decision.** Adding a pair to the matrix (for
   example a protocol proxy for Claude Code × local, or API-key access for
   omp × Claude) needs a measured need and its own ADR — the proxy ban in
   PRINCIPLES §8 rule 5 stays until then.

### What this changes in ADR-085

| ADR-085 | Before | Now |
|---|---|---|
| §6 house rule 2 | "Procedure and data are portable, the operator surface is not." | Procedure, data **and** the operator surface are portable; vendor features need a neutral path in MC, not only a fallback in the run record. |
| §7 decision 1 | "Local GPU hosts move from main engine to helper and fallback." | Local runtimes come first where good enough; GPU boxes are a main engine for heads. |
| §8 stop 1 | "Is the helper role of the GPU hosts acceptable?" | Replaced by: "Does local first hold — what share of jobs finish on a local runtime, and at what operator cost?" |

Everything else in ADR-085 stays: head per job, build stop on the fleet
layer, quiet mode, forbidden list, usage limit for Claude heads (30 %), stop
points 2 and 3.

## Alternatives

- **Keep ADR-085 as it is (one vendor surface, local as helper)** → Rejected
  by the operator's principle: it binds the working model to one vendor's
  roadmap and plan rules, and it left the local GPU boxes idle.
- **Rewrite ADR-085 in place** → Rejected: ADRs are never rewritten
  (decisions/README). ADR-085 gets an "Amended by ADR-086" note instead.
- **Build a model proxy now so that Claude Code runs on local models** →
  Deferred: it is a new component in front of the coding CLI (PRINCIPLES §8
  rule 5), and OpenClaude and omp already give a local route. Only a measured
  gap justifies it (decision 6).
- **Let each harness keep its own start path and status** → Rejected: the
  operator would need one surface per harness, which is exactly the lock-in
  this ADR removes.

## Consequences

### Positive

- A new harness or model is an entry in the matrix and the runtimes table,
  not a new working model.
- Local GPU boxes carry real work again; cloud usage becomes the exception
  and is visible in the run record.
- The quarterly swap probe (PRINCIPLES §3 rule 7) turns from a test into
  normal operation.

### Negative

- **Quality varies by pair.** A local mid-size model is not a large cloud
  model; the procedure's own gates (failing test, sabotage probe, fresh
  reviewer) must catch weak results, and the run record must name the pair.
- **The neutral path is MC code.** Starting, stopping and watching heads for
  several harnesses is new surface that must stay small: it reuses the
  matrix, the runtimes table, the worktree code and the task detail, adds no
  persistent agent, and does not touch the frozen dispatch and healer code.
- **Harness-specific start commands exist.** They stay in the existing harness
  modules (`harness_compat.py`, `harness_catalog.py`), not spread over the
  backend (PRINCIPLES §3 rule 4).
- **Print mode for heads.** omp's non-interactive mode is its print mode. The
  ban on headless print-mode (PRINCIPLES §8 rule 10) is narrowed to Claude
  heads; other harnesses use their own official non-interactive mode.
- **Local engine outages hit heads directly**, not only a helper step. A
  local head must end with a clear "failed — runtime unreachable" in the run
  record, and "restart with another pair" is the way back.

### Open source / existing installations

Nothing is removed. Installations without local GPU boxes keep a cloud pair
as their default; "local first" only applies when a local runtime exists.

### Touched ADRs

| ADR | Effect |
|---|---|
| ADR-085 Head per job | **Amended** at §6 rule 2, §7 decision 1 and §8 stop 1 (see table above). |
| ADR-056 Harness/provider decoupling | Confirmed as the single source of truth for pairs. |
| ADR-084 omp ACP as harness property | Unchanged; `HARNESS_CAPABILITIES` is the capability source. |
| ADR-077, ADR-078 Box manager | Kept; local engines are now a main engine for heads. |

## References

- Matrix: `backend/app/services/harness_compat.py` (`HARNESS_PROTOCOLS` line
  74, `is_compatible` line 125, `incompat_reason` line 132,
  `resolve_provider_credentials` line 177 ff., `HARNESS_CAPABILITIES`
  line 296)
- Harness catalog and host harnesses: `backend/app/services/harness_catalog.py`,
  `backend/app/services/host_harness_adapter.py`
- Runtime switch: `backend/app/services/agent_runtime_switch.py`
- Related ADRs: ADR-056, ADR-077, ADR-078, ADR-081, ADR-084, ADR-085
- Principles and stages: [`../PRINCIPLES.md`](../PRINCIPLES.md),
  [`../ROADMAP.md`](../ROADMAP.md)
