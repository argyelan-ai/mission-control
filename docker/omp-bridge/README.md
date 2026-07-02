# omp-bridge — PROTOTYPE

A runtime-adapter that drives an MC agent with **omp** (`omp -p --mode json`)
instead of the tmux screen-scrape harness (`openclaude` + `poll.sh` +
`turn-state.sh`). It reads omp's structured NDJSON lifecycle stream, decides
**deterministically** whether a run finished or aborted, and maps that to the MC
agent lifecycle (`mc ack` / `mc finish` / `mc blocked`).

**It closes the silent-abort gap:** with the old scrape harness a turn (or the
whole run) can end without the task being complete, and nobody PATCHes a
terminal status — the task hangs `in_progress` forever (15–60+ min until a
backend stale check that never even sets `blocked`). Here **every run resolves
into exactly one of `{finish, blocker}`** — there is no path that ends a run and
leaves the task `in_progress`.

Full design + rationale: [`docs/omp-bridge-design.md`](../../docs/omp-bridge-design.md).

---

## The pieces

| File | What it is | Prototype-stub vs real |
|---|---|---|
| `bridge.py` | The heart. NDJSON reducer → classifier → lifecycle mapper. Importable + CLI + a live-subprocess path with a wall-clock/no-progress watchdog. | **REAL** logic. The MC lifecycle hooks are a **STUB** (log only). |
| `tests/test_bridge.py` | GOLDEN test. Feeds captured + synthetic streams, asserts FINISH vs SET-BLOCKER. Runs under pytest **or** standalone. | **REAL** — run it. |
| `tests/make_fixtures.py` | Generates the synthetic fixtures (finish-with-sentinel, crash, transient-api-error, malformed-reflection, anti-echo). | **REAL** generator. |
| `tests/fixtures/*.ndjson` | Synthetic streams mirroring real event shapes. | test data |
| `rpc/*.ndjson` | **REAL captured omp streams** (json / err2 / maxtime / trivial) = ground truth. | real evidence |
| `Dockerfile` | Build-only image sketch (omp + tmux + mc CLI + bridge entrypoint). | **SKETCH** — omp install line is a commented PLACEHOLDER; do not build. |
| `entrypoint.sh` | 3-window tmux boot; Window 0 = persistent `bridge.py`. | **SKETCH** — bootstrap/omp-serve marked `<<< >>>`. |
| `omp-recycler.sh` | Forked recycler tracking `bridge.py`, not the one-shot omp. | **SKETCH** — respawn calls marked `<<< >>>`. |

### What is a STUB (explicitly)

- **No real backend call.** `LoggingLifecycle` in `bridge.py` only *logs the
  intended* `mc` CLI call (`[mc-stub] mc ack …`). The Phase-2 real impl shells
  out to the copied `mc` CLI.
- **No omp install / no build / no deploy.** The Dockerfile's omp install is a
  commented placeholder; the pinned package for omp v16.2.13 is a Phase-2 gate.
- **Live Claude sentinel reliability is UNVERIFIED.** All fixtures are Qwen-shaped
  or synthetic. False-positive/false-negative sentinel counts on live Claude are
  a hard Phase-2 gate (design §7 #8).
- **`entrypoint.sh` / `omp-recycler.sh`** are boot sketches with inert `<<< >>>`
  markers; they are not exercised by the test.

### What is REAL

- The reducer, the completion contract (sentinel + 4-field reflection), the
  classifier, the retry-then-blocked policy, and the wall-clock watchdog code.
- The golden test — and it runs green against **real captured streams**.

---

## Run the test

```bash
cd docker/omp-bridge/tests

# standalone (no deps):
python3 test_bridge.py

# or under pytest:
python3 -m pytest -v
```

Regenerate fixtures (idempotent): `python3 tests/make_fixtures.py`.

## Try the CLI on any stream

```bash
cd docker/omp-bridge
python3 bridge.py --json tests/fixtures/finish-with-sentinel.ndjson   # -> finish
python3 bridge.py rpc/json-stream.ndjson                              # -> blocker (no sentinel)
python3 bridge.py rpc/maxtime-stream.ndjson                           # -> blocker (max-time cutoff)
cat rpc/err2-stream.ndjson | python3 bridge.py -                      # stdin -> blocker
```

---

## Run outcome → MC lifecycle (the core mapping)

Decision comes from the **stream**, never the exit code (all normal outcomes
exit 0). `mc failed` is **never** used — `FAILED → {INBOX}` only, auto-unassigns,
no auto-redispatch; `blocked` is reversible and human-visible.

| omp run signal | Kind | MC action |
|---|---|---|
| `session` first line | — | `mc ack` (inbox → in_progress) |
| `agent_end` + `stopReason==stop` + trailing `TASK_COMPLETE` + valid 4-field reflection + no trailing tool error | `finish` | `mc finish [--review]` |
| `stopReason==stop` but **no sentinel** | `silent_abort_no_sentinel` | `mc blocked` (the semantic silent-abort catch) |
| sentinel present, reflection missing/<80 chars | `malformed_reflection` | `mc blocked` |
| `stopReason==error` + transient msg (`fetch failed`/`5xx`/…) — the **original openclaude failure** | `abort_transient_api` | retry ×N → `mc blocked` |
| `stopReason==error` non-transient | `abort_error` | retry ×N → `mc blocked` |
| `stopReason==toolUse` / `[Command cancelled]` (`--max-time`) | `abort_maxtime` | retry ×N → `mc blocked` |
| **no `agent_end`** at exit (crash) | `abort_crash` | retry ×N → `mc blocked` |
| watchdog no-progress / wall-clock kill (hang) | `abort_hang` | retry ×N → `mc blocked` |
| exit 1/2, no json session | `launch_preflight` | `mc blocked` (config/credential) |

### Golden test result (real run)

```
12 passed
  FINISH   fixtures/finish-with-sentinel.ndjson   kind=finish
  BLOCKER  fixtures/incomplete-abort-crash.ndjson kind=abort_crash
  BLOCKER  fixtures/transient-api-error.ndjson    kind=abort_transient_api
  BLOCKER  fixtures/malformed-reflection.ndjson   kind=malformed_reflection
  BLOCKER  fixtures/anti-echo-giveup.ndjson       kind=silent_abort_no_sentinel
  BLOCKER  json-stream.ndjson  (REAL)             kind=silent_abort_no_sentinel
  BLOCKER  err2-stream.ndjson  (REAL)             kind=abort_error
  BLOCKER  maxtime-stream.ndjson (REAL)           kind=abort_maxtime
  BLOCKER  trivial-json.ndjson (REAL)             kind=silent_abort_no_sentinel
```

> Note: the real `json-stream.ndjson` finishes with `stopReason==stop` and a
> `"Done."` text but **no sentinel/reflection** (it was captured with a bare
> prompt). Under the completion contract that is correctly caught as a **blocker**
> — a live demonstration of the exact semantic silent-abort the design closes.
> The `finish-with-sentinel.ndjson` fixture adds the §3.4 prompt-wrapping
> contract on the same event shape and is the FINISH case.

---

## Not wired in yet

This prototype is **not selectable** and **not wired to the backend**. Making an
agent actually run on omp is a gated Phase-2 step (design §7): the backend
DB-only watchdog (mandatory, first), `pick_image_for_runtime` branch on
`runtime_type=="omp"`, `internal.build_runtime_env` + `docker_agent_sync` token
routing, a seeded runtime row, the omp package pin, an ADR + ARCHITECTURE.md
update, and a live Claude gate (real transient-error stream + sentinel
reliability). Each is reversible and per-agent.
