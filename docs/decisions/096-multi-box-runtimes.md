# ADR-096 — Multi-box runtimes: one membership path, recipes state needs, boxes state facts

**Status:** Proposed
**Datum:** 2026-10-19
**Scope:** Backend/DB · Infra/Runtime · Frontend/Runtimes · Install · Node-agent
**Amends:** ADR-077 (rule 5; P3 decision 1 "NULL fabric_ip falls back to the SSH address"), ADR-048 (resolver reads members)
**Relates:** ADR-042, ADR-054, ADR-078, ADR-086 (not touched), ADR-093

> Basis: `origin/main` @ 8ef7862e, after PR #878 (writes `runtime_hosts` on
> `POST/PATCH /runtimes/db`). Line numbers below are from that commit.
> Evidence marks: [C] code, [L] live read on 2026-10-19, [A] assumption.
> Number: 096 was reserved for this ADR; ADR-097 (delivered is not working,
> PR #884) landed first.

## Kontext

A runtime that spans two boxes (head + worker) is recorded in two places:
`runtimes.topology` (`nodes`, `worker_host_id`) and `runtime_hosts` (one row
per member). Exactly one function writes `runtime_hosts`:
`recipe_switcher.set_runtime_members` (recipe_switcher.py:1137) [C]. It can
express two boxes only (head rank 0, one worker rank 1) [C]. It is called from
`start_recipe_on_host` (:1453) — at **start**, not at catalog install (install
creates no runtime, `plan_duo_install` :1044) — and not at all when the
instance already runs: that branch reads `topology.worker_host_id` and returns
early (:1325–1345) [C]. Since PR #878 it is also called from
`POST/PATCH /runtimes/db` (runtimes.py:1266), and `nodes >= 2` without a valid
worker answers 422 [C]. Import and any other path still leave
`runtime_hosts` empty.

Consequences seen on 2026-10-19 [L]:
- The Runtimes page builds the duo stage from `member_hosts` only and ignores
  `topology.nodes`. A `nodes: 2` runtime without rows shows as solo; its worker
  looks free and can be handed out twice.
- "Loaded" is probed on the head only (`probe_running`, recipe_switcher.py:514).
- Memory preparation runs on the head only (`prepare_for_runtime(runtime,
  host=resolved)`, runtime_manager.py:2231) and ends on the head only
  (`finish_for_runtime` :2254/2258; watcher `_finish_memory_prep`
  runtime_watcher.py:937) [C]. A worker held 29 GiB page cache; the recipe's
  `start.sh` aborted with "only 111.2 GiB available on worker, 112 GiB needed".
- A DeepSeek duo runtime is a hand row (no `recipe_slug`); its fleet values
  live in the recipe checkout: `scripts/local.sh` sets the worker's SSH target
  and the NFS server address. The recipe's `scripts/config.sh` loads `local.sh`
  as bash **before** `.env`, and "where both set a key, local.sh wins" [L].
  A stranger cannot be expected to edit shell files on each box.
- The fabric placeholders fall back silently: `head_fabric_ip` /
  `worker_fabric_ip` take `ssh_host` when `fabric_ip` is empty
  (recipe_env.py:110–112) [C]. PR #887 (open, two-box DeepSeek catalog entry)
  found that for `NFS_SERVER` this picks the wrong network — the export is
  granted to the worker's link address, a mount through the LAN address is
  refused — and had to leave the key unset; it also found no catalog field for
  the root setup (the export) the head needs [C, PR #887 description].
- An engine died during a load and the operator only saw that the model was
  gone ("why does the watcher just switch off?"). What happened on 2026-10-19:
  the DeepSeek duo's rank-0 container was killed by **the recipe's own memory
  guard** at 18:44:25 UTC and stayed `Exited (137)` until a manual restart at
  about 19:07 [L: read on the box at 21:09 and recorded in the run record;
  not re-read for this ADR]. MC did not stop it. MC noticed it, but late and without a cause: `runtime.unreachable` at
  18:48:18, 3 min 53 s after the death (3 failed probes at the 90 s interval,
  `UNREACHABLE_EVENT_THRESHOLD`, runtime_watcher.py:89/342; config.py:499)
  [L: `activity_events`]. That one death produced **three** events within
  100 ms — for the DeepSeek row, for the GLM row (which was not running at all)
  and for the box's slot row — because all three share the box's `:8000`
  endpoint and "down" is decided per endpoint [L: `activity_events`,
  `runtimes`]. Nothing restarted it: the row is a hand row and the box has
  `autostart_enabled` off [L].
- MC **does** stop engines on its own in exactly one case: a confirmed crash
  loop. `_check_crash_loop` (runtime_watcher.py:732–880) compares Docker's
  `RestartCount` against a baseline; at `CRASH_LOOP_RESTART_THRESHOLD = 3`
  restarts within `CRASH_LOOP_WINDOW_SECONDS` (10 min) it runs
  `docker update --restart=no` + `docker stop`, reads the last
  `_CRASH_LOG_LINES = 200` lines of the current boot
  (`_read_container_logs` :911), picks a reason (`_last_error_line` :167,
  `CRASH_LOOP_LOG_PATTERNS` :141), writes the live status
  `status=failed, reason=…` and emits `runtime.crash_loop_stopped` [C]. It did
  not fire on 2026-10-19 [L: no `runtime.crash_loop_stopped` event; the
  container stayed exited instead of restarting]. Apart from that, MC stops
  engines only through an operator stop or the eviction done by a start [C].
  Auto-recovery (`_maybe_auto_recover` :1024) runs only if the box has
  `autostart_enabled` (default off), and then starts the box's
  `autostart_recipe_slug` — not necessarily the runtime that died — at most
  once per `AUTO_RECOVERY_COOLDOWN` (15 min) and `AUTO_RECOVERY_MAX_ATTEMPTS`
  (2) times within `AUTO_RECOVERY_FAILURE_TTL` (6 h) (:98–104) [C]. For a death
  that is not a crash loop (exit, memory guard, kernel OOM), no cause is read
  anywhere [C].

Mechanisms that already exist and are reused, not rebuilt:
- placeholders `head_ip`, `worker_ip`, `head_fabric_ip`, `worker_fabric_ip`,
  `head_ssh`, `worker_ssh`, `port` (recipe_env.py:58–72), rendered into the
  recipe's `.env` with read-back [C]. `{head_fabric_ip}` already exists (ADR-077
  P3); the working note that asked for it as "new" was wrong.
- the `config_checks` registry pattern (services/config_checks/registry.py, 0218) [C].
- the start policy for occupancy: `box_guard.check_displacement`,
  `check_engine_idle`, `ensure_exclusive_host` (recipe_switcher.py:1406,
  1466) — eviction, not refusal [C].
- the eviction marker `get_evicted` that keeps auto-recovery away from a
  runtime stopped on purpose (runtime_watcher.py:1070; one hour,
  `EVICTED_TTL`, runtime_grace.py:112) [C].
- crash-loop detection and stop (`_check_crash_loop`, `_read_container_logs`,
  `_last_error_line`, `CRASH_LOOP_*`, `_CRASH_LOG_LINES`), the live status
  fields `status`/`reason`, and auto-recovery with its budget
  (`_maybe_auto_recover`, `AUTO_RECOVERY_*`) [C]. §11 extends these; it adds
  no second log reader, reason field or restart budget.
- the per-row ownership check `_served_answer_is_own` (runtime_watcher.py:604:
  container running / process alive, slot rows always own) [C], today used
  for drift only; §11 also uses it for up/down.
- the node-agent with a fixed list of root-owned wrappers that reconciles
  desired vs. actual state, including `min_free_kbytes` (plans a write whenever
  actual ≠ desired) [C, scripts/node-agent/mc-node-agent.py].

Gaps: no invariant or healer between `topology` and `runtime_hosts` (only
`runtime_hosts → topology` on restart, runtime_multinode.py:49–78) [C]; no
pre-flight for gated HF repos, image availability, architecture, head→worker
SSH, fabric reachability [C]; placeholders are substituted raw into
`install_template` shell (launch_template.py:131 `return str(value)`) and
`ssh_host`/`ssh_user` are free strings — only `fabric_ip` is validated
(routers/hosts.py) [C]; two writers of `vm.min_free_kbytes` (node-agent desired
state and `host_memory_prep`) [C]; recipe switches (abliterated weights, draft
model) are set by editing files on the box, not in MC [L]; a recipe that needs
root setup on the head (an NFS export) has no field to say so [C].

External practice (LeaderWorkerSet, SGLang `--node-rank`, NVIDIA Dynamo,
GPUStack, DGX Spark playbooks, hf-preflight): leader + workers are one atomic
object, one artifact with the rank as a start parameter, a registry fed by
devices, checks before download. Ollama, with no membership model in the core,
is the counter-example: every cluster is a hand-made fragment.

## Begriffe

- **Recipe** (catalog entry, `local_recipes`): the *need* — engine, model,
  number of boxes, roles, link requirement, port, memory, disk, architecture,
  image, gated repos, options, root setup. Never a box, address, user name or
  path that differs between fleets.
- **Runtime** (`runtimes`): a recipe (or a legacy hand row) bound to boxes.
  `runtimes.host_id` = head = the box MC talks to.
- **Solo runtime**: `nodes <= 1`, one box. **Duo runtime**: `nodes = 2`.
  Both are first-class (§10).
- **Host** (`hosts`): a registered box. Operator fields are the render source.
- **Member** (`runtime_hosts`): (runtime, host, role, rank). Head = rank 0 and
  **is** in the table for multi-box runtimes (fixes the runtime_host.py
  docstring "head never in the table" and the defensive head filter in
  runtimes.py).
- **Fabric link**: box↔box network the engine uses (QSFP/RoCE, 10 GbE, LAN).
- **MC view vs. box view**: `{*_ip}`/`{*_ssh}` are how **MC** reaches a box.
  Anything box↔box uses `{*_fabric_*}`.

## Entscheidung

### 1. Membership: one writer, one truth, structural rules only

1.1 New module `backend/app/services/runtime_membership.py` owns
`runtime_hosts`. `set_runtime_members` moves there (re-exported from
`recipe_switcher` for one release):

    set_members(session, runtime, members: list[MemberSpec], *, recipe=None, commit=True)
    MemberSpec = (host_id, role, rank)
    members_of(session, runtime) -> list[Member]      # head first, then ranks

Delete-then-insert in one transaction (rank uniqueness, as today), under a row
lock on the runtime (`SELECT … FOR UPDATE`) so concurrent writers — API and
worker are separate processes — are serialised (§2). It is the
only backend code that writes `RuntimeHost` — an architecture test over
`backend/app` rejects `RuntimeHost(`, `insert(RuntimeHost`,
`delete(RuntimeHost`, `RuntimeHost.__table__` and `session.delete(` on member
rows elsewhere.

1.2 `set_members` checks **structure only** (`MembershipError(status, sentence)`):
- ranks `0..n-1` contiguous and unique; rank 0 = `runtime.host_id`, role
  `head`; others `worker` → 422
- `n == topology.nodes`; with a recipe, `n == recipe nodes` → 422
- hosts distinct, existing, enabled → 422
- no member is `power_managed` (ADR-042) until "wake all members" exists → 422
  "<box> sleeps on demand; multi-box runtimes cannot use it yet"
- the runtime is **running** and the requested set differs from the stored set
  → 409 "runs on <box>; stop it before changing members". Filling missing rows
  from known data is allowed.

It does **not** check occupancy and **not** SSH reachability:
- Occupancy stays start policy (`check_displacement`, `check_engine_idle`,
  `ensure_exclusive_host`). Switching between two duos — or between a solo and
  a duo — on the same boxes is the normal case and must keep working: members
  are written, then the boxes are evicted (recipe_switcher.py:1453–1470).
  `POST/PATCH /runtimes/db` may return a non-blocking warning from
  `FleetState.exclusive_busy`.
- Reachability belongs to start pre-flight; a sleeping or rebooting box must
  not make *creating* a runtime impossible.

1.3 Every path calls it: recipe start (including the already-running branch,
which now verifies/fills instead of returning early), `POST /runtimes/db`,
`PATCH /runtimes/db/{slug}` (`host_id` change moves rank 0; `nodes` change
re-validates), import. "Adopt" a hand row = `PATCH` with
`topology.recipe_slug` + `members` — no separate endpoint. Install still
creates no runtime.

1.4 Request contract: optional `members: [{host_id, role, rank}]`; short form
`worker_host_id` (and legacy `topology.worker_host_id`) is accepted and
translated. `nodes >= 2` without a valid worker → 422 (as in PR #878).

1.5 `runtime_hosts` is the only truth. `topology.worker_host_id` is **no
longer written**; API responses and `to_registry_dict()` **derive**
`worker_host_id` from rank 1 so old readers keep working. Existing values are
backfilled into rows once (§2) and then ignored. Backend readers that switch to
`members_of` (as of 8ef7862e): the start path and its already-running branch
(recipe_switcher.py:1338, :1447, :1576), the watcher autostart
(runtime_watcher.py:1283), `routers/host_recipes.py` (:60–108),
`routers/local_registry.py` (:243–373), `runtime_multinode.py`.

1.6 Ranks are general in the database and in `set_members`. Selection
(`plan_duo_install`, `worker_candidates`, `evaluate_capacity`), start,
placeholders and UI stay at **2 boxes**. N > 2 is "not supported" in the docs
and needs a follow-up ADR (including per-peer fabric addresses:
`hosts.fabric_ip` is one address per box).

### 2. Invariants and healer (writes once at start, marks in the cycle)

- **I1** `nodes <= 1` ⇔ zero member rows (slot runtimes, ADR-078, excluded).
- **I2** `nodes = n >= 2` ⇔ exactly n rows, ranks 0..n-1, rank 0 = `host_id`.
- **I3** with `topology.recipe_slug`: `nodes` equals the recipe's.
- **I4** every member host exists and is enabled.

"One exclusive runtime per box at a time" is **not** a membership invariant;
the start enforces it by eviction (§1.2).

`membership_audit()`:
- **At worker start** (background.py, which runs in the `mc-worker` container;
  the API runs with `ENABLE_BACKGROUND_SERVICES=false`, docker-compose.yml),
  **before** `runtime_watcher.start()` (background.py:199): repair what is
  repairable from existing data — legacy `topology.worker_host_id` without
  rows → rows via `set_members`. The watcher's own first cycle (and with it
  the autostart) runs only after the audit returned. The API process keeps
  running while the worker restarts, so operator starts and `POST/PATCH` are
  **not** ordered after the audit. Two rules close that race: `set_members`
  takes a row lock on the runtime (`SELECT … FOR UPDATE` on `runtimes`) for
  its delete-then-insert, so every writer is serialised; and the audit skips
  a runtime that has an active switching marker (`get_switching`) and
  re-checks I1–I4 under the lock before it writes. Each
  repair writes an activity event that also says what changes for eviction
  ("<box> now counts as used by <runtime>; starting <other> there will stop
  it"). The operator decided on 2026-10-19: write and report, not report only.
- **In the watcher cycle**: read-only. A broken runtime gets the health reason
  `members_incomplete`; one notification per runtime (deduplicated). Own step
  after the probe loop, own try/rollback.
- **Never guesses a box.** `nodes >= 2` with no known worker → start answers
  409 with a sentence; the card offers "Pick worker".

### 3. Honest display, one status per runtime

3.1 `GET /runtimes` returns `nodes`, `members` (head included: host, role,
rank, name) and `members_complete`. The frontend builds the duo stage from
`nodes`; a missing rank is an empty slot "Worker not assigned"; that worker is
not in the free list. A solo runtime shows exactly as today.

3.2 Worker liveness comes from the **node-agent**, not from SSH per tick: the
agent reports running container/process names that match an allowlist
(`requirements.roles.<role>.anchor`, sent in desired state as fixed strings).
No agent or no anchor → member reads "unknown", never "running". A "Check now"
button may run one read-only SSH probe on demand.
**Precondition [A]:** container anchors need the node-agent's service user to
read Docker (`docker ps` / the socket). Not verified on the boxes. Fallback if
it may not: the agent reports process names only (`kind: process`), and
container anchors read "unknown" until the operator grants read access. The
agent PR states which case it found.

3.3 One status per runtime: the watcher derives `running` (head up and every
known member anchor up), `degraded` (head up, a member down) or `stopped`.
`degraded` notifies; it does **not** trigger a restart — recovery of workers
is the recipe's job.

3.4 Amends ADR-077 rule 5: MC still starts only the head and lets the recipe
orchestrate workers. MC may **read** members (liveness, pre-flight, cause of
death §11) and **prepare memory** on members (§4). It never starts or stops
engines on a worker.

### 4. Memory preparation on every member

4.1 `prepare_for_runtime` runs for every member, in parallel, one `PrepHandle`
per box (`prep_key(box_key, runtime_id)` is already per box,
host_memory_prep.py:250). If one box cannot free enough memory, the start stops
with a sentence naming that box; handles already taken are restored.

4.2 **The window ends on every member, too.** All three places that end it
close the handles of **all** members: the first successful watcher probe
(`_finish_memory_prep`, runtime_watcher.py:403/937, which today resolves only
the head), a failed start (`finish_for_runtime`, runtime_manager.py:2254/2258,
which takes a list of handles instead of one), and the orphan sweep
(`recover_orphaned_preps`, already per box, stays as the safety net).
Otherwise the dropper container keeps running on the worker and its watermark
stays lowered until the 30-minute sweep — which could then reset it in the
middle of a long load [A].

4.3 The watermark write keeps today's mechanism (`docker run --privileged`
over SSH, host_memory_prep.py:345–352) — every container recipe already needs
the SSH user in the docker group, so it works on foreign boxes too.

4.4 Conflict with the node-agent: when a box's desired state sets
`min_free_kbytes`, the agent reconciles any difference and would undo
memprep's temporary value during a load [A: timing depends on
attempts/paused_until, not measured]. Fix: desired state gains
`min_free_kbytes_hold: {value, until}`; memprep sets the hold instead of
writing directly on agent-controlled boxes; the agent honours it until `until`
and then returns to the desired value itself. One owner (the agent), one
bounded exception. Lowering stays possible — it is memprep's purpose
(host_memory_prep.py:13, 49). If the orphan sweep clears a handle while a hold
is active, both are fine together: the sweep ends the dropper, the agent ends
the hold at `until`.

### 5. Placeholders: typed, validated at render, no silent wrong fallback

5.1 Every placeholder has a type, checked **at render time** (catches old
data): `ip` (Python `ipaddress`), `host` (IP or RFC 1123 name), `user`
(`^[a-z_][a-z0-9_-]{0,31}$`), `ssh` (`user@host`), `port`, `path`
(`^[A-Za-z0-9_./~-]+$`), `option` (§7.3). A failing value blocks with a
sentence naming the box and field. Reason: values are substituted raw into
`install_template` shell (launch_template.py:131). Sabotage test:
`ssh_user = 'x$(touch /tmp/pwn)'` → render error, no file. (`.env` is written
quoted and upstream recipe scripts *parse* it, "read, never run" [L]; the
typed check still applies to it.)

5.2 New placeholders (only what current recipes need):

| Placeholder | Type | Source | Missing |
|---|---|---|---|
| `{head_user}`, `{worker_user}` | user | `hosts.ssh_user` / settings default | block |
| `{head_ssh_fabric}`, `{worker_ssh_fabric}` | ssh | `user@fabric_ip` | as fabric (5.3) |
| existing `{*_fabric_ip}` | ip | `hosts.fabric_ip` | see 5.3 |
| `{opt:<KEY>}` | option | the runtime's chosen option (§7.3) | recipe default, else block |

No `{node_<r>_*}`, `{*_hf_cache}`, `{*_fabric_iface}`, `{head_nfs_path}` now:
2-box only (§1.6); upstream recipe scripts find link, interface and rendezvous
address themselves [L]; and an NFS placeholder would promise an export MC does
not provide (§7.4 covers the setup need instead).

5.3 **Fabric placeholders are strict** (changes ADR-077 P3 decision 1):
`{*_fabric_ip}` and `{*_ssh_fabric}` render the box's `fabric_ip` or **block**
with "Box↔box address of <box> is missing — set it under Devices". No silent
fallback to `ssh_host`, LAN or Tailscale. A recipe may opt into a fallback with
`requirements.fabric: preferred | none`; then `ssh_host` is used **only** if it
is not in 100.64.0.0/10 and not a `tailscale_host`, and the pre-flight shows it
as a warning, never silently. A recipe without `requirements` counts as
`required` when `nodes >= 2` and as `none` when solo. The dry run in 5.5 runs
before this is switched on.

5.4 Device-reported data is **never** a render source — only a suggestion the
operator takes over with "Use detected", passing the same validator.

5.5 Catalog lint (at sync): unknown placeholder → entry listed but "not
startable: unknown placeholder {x}"; literal that looks like an IP,
`user@host` or home path → warning; a box↔box key (`WORKER*`, `MASTER_ADDR`,
`*HOST_IP*`, `NCCL*`, `NFS_SERVER*`) filled from `{*_ip}`/`{*_ssh}` → warning
"MC view, not box view". Before enabling, a dry run against all current
entries (20 rows [L]) so nothing turns "not startable" by surprise.

5.6 Shadowing check, no recipe cooperation needed: after writing `.env`, grep
the recipe's `*.sh` / `scripts/*.sh` for assignments of the env_map keys
outside `.env`; each hit is a warning with file:line ("scripts/local.sh sets
WORKER — it overrides MC's value; remove it").

5.7 Same key, different meaning is allowed and documented: the GLM TensorFold
recipe uses `WORKER = {worker_ssh}` (LAN) and checks it in its
`install_template`; the DeepSeek duo will use `{worker_ssh_fabric}`. Nobody
"unifies" GLM.

### 6. Pre-flight register (fresh, not stored)

`services/recipe_preflight/` follows the `config_checks` registry pattern: one
entry per check with `id`, `applies(recipe, plan)`, `run(ctx) -> Result`,
`phase` (install | start). `Result` = `pass | warn | block | skip`, reason
code + params (UI words it DE/EN), `box`, `fix` (link | action | copyable
command). Adding a check is one entry. Results are computed fresh at install
and start and **not stored** (no second "is it ok?" truth); HF answers are
cached in memory for 10 minutes per repo. Solo and duo recipes run the same
register; checks that need a worker `skip` on solo.

Existing checks move in unchanged (SSH alive, env_map, placeholders, RAM vs.
`min_vram_gb`, disk warning, port, handle, switch lock). Wave 1 adds:

| id | Check | Level |
|---|---|---|
| `hf_gated` | each repo: public → pass; gated + granted → pass; gated + not granted → block + "Request access" link; gated + no token → block + "Add HF token" | block |
| `disk_per_member` | free space on the HF-cache filesystem of each member vs. its share of the weights | block below need, warn below need × 1.1 |
| `arch_match` | `uname -m` per member over SSH vs. recipe arch; members equal | block |
| `head_to_worker` | from the head: `ssh -o BatchMode=yes -o ConnectTimeout=3 <worker_ssh_fabric> true` and one ping over the fabric address; targets = registered members only | block, fix = exact `ssh-copy-id` command to copy (MC never distributes keys) |
| `image` | `requirements.image` present on each member / pullable for its arch / must be built | warn "builds on the box, about N min"; block on arch mismatch |
| `capacity_by_memory_kind` | unified memory → `mem_total`; discrete → sum of GPU memory; TP across unequal boxes → block | block |
| `shared_storage` | if `requirements.shared_storage = nfs`: export visible from the worker | block with setup hint (§7.4) |
| `root_setup` | each `requirements.root_setup` item present on its box (§7.4) | block, fix = button on the box's settings |

The install dialog groups lines by box; any `block` disables "Install".

### 7. Recipe needs and options are typed objects

7.1 `RecipeSpec` (local_registry.py:86, `validate_vocabulary`) gets a typed
`requirements`: `fabric: required|preferred|none`, `rdma:
required|optional|none`, `hf_repos`, `image`, `disk_gb_per_role`,
`shared_storage`, `roles.<name>.anchor` as `{kind: container|process, name}`,
`roles.<name>.engine_rank`, `root_setup` (7.4), `death_markers` (§11.3).
`topology` keeps only `{nodes, roles: {name: count}}`.

7.2 Storage: one additive migration with two nullable columns,
`local_recipes.requirements` and `local_recipes.options`. Existing typed
columns (`arch`, `min_vram_gb`, `est_weights_gb`, `port`) stay the source for
what they already hold; `requirements` never duplicates them.

7.3 **Options per recipe** (ADR-093: what a user may change later is
changeable in MC). A recipe declares the switches a user may set, instead of
the user editing `.env` or `local.sh` on a box:

    options:
      ABLIT:   {type: bool, default: false, label: {en: "Abliterated weights", de: "Abliterierte Gewichte"}}
      DRAFTER: {type: enum, values: ["none", "dflash"], default: "dflash", label: {…}}

Types: `bool`, `enum` (fixed values), `int` (with min/max). Free strings are
not allowed — every value passes the `option` validator, so options cannot
carry shell. The runtime's choices live in `runtimes.topology.options`
(no new runtime column) and render through `{opt:<KEY>}` in `env_map`. The card
shows an "Options" block; a change on a running runtime is saved and shown as
"takes effect at the next start" — MC does not restart on its own. Solo and duo
recipes declare options the same way.

7.4 **Root setup on a box** (e.g. the NFS export a duo needs on its head). A
recipe declares it, it never runs it:

    root_setup: [{role: head, kind: nfs_export, path_option: MODEL_DIR, clients: fabric}]

`kind` is a fixed vocabulary (wave 1: `nfs_export`), each kind backed by one
node-agent wrapper with typed parameters — never free shell. The pre-flight
check `root_setup` shows a missing item with a button on that box's settings;
the operator confirms, the node-agent applies it through desired state. A
recipe start never changes desired state on its own (§9). Until the wrapper
exists, the check blocks with a setup hint and the handbook names the manual
step.

### 8. New multi-box recipes only from the catalog

8.1 `local_registry_sources` gains a **local folder source** (`file://` path,
default `~/.mc/recipes/*.json`) that runs through the **same** sync, schema
validation and lint as URL registries. An own recipe is a file; a private
recipe never leaves the box (the DeepSeek duo recipe stays private, operator
decision 2026-10-19). Same slug in two sources → sync error naming both.

8.2 Once 8.1 exists, `POST /runtimes/db` with `nodes >= 2` and no
`recipe_slug` → 422 pointing to "own recipe" (operator decision 2026-10-19).
Existing hand rows are grandfathered: they keep working, get §1–§4 and §11,
show "hand entry — not managed by the catalog", no autostart (as today). Solo
hand rows stay allowed.

### 9. Root and trust, stated honestly

- Recipes never call `sudo`; host tuning or root setup a recipe needs is a
  **requirement** shown in pre-flight with a button on the box's settings
  (node-agent desired state). A recipe start never changes desired state on
  its own.
- The node-agent wrappers are order and visibility, not a security boundary:
  MC's SSH user is in the docker group and memprep already runs
  `--privileged`. The handbook says plainly: "MC has root power on your boxes
  through Docker."
- Pairing is unauthenticated and the device's `ssh_host` is ignored on purpose
  (routers/nodes.py); this ADR keeps that line for all device data (§5.4).
- Registries deliver shell (`install_template`); trust is per registry
  source. Local folder entries need the admin role to add a source.

### 10. Solo and duo are equal

The operator's rule (2026-10-19): "We keep solo-Spark and dual-Spark models and
can manage both." Nothing in this ADR may make solo runtimes worse.

- Catalog, install, pre-flight, options, start/stop, card, autostart and
  engine-death reporting (§11) work the same for solo and duo. Duo-only parts
  (members, worker checks, worker memprep) are additions that `skip` on solo.
- Solo runtimes keep **zero** member rows (I1; no rewrite of existing rows).
  `members_of(solo)` returns the implicit head member `(host_id, head, 0)`,
  computed, not stored, so readers have one code path.
- Switching between solo and duo **on the same boxes** is a normal recipe
  switch and uses the existing start policy: a duo start evicts solo runtimes
  on head and worker; a solo start on either box of a running duo evicts the
  whole duo (both boxes become free, memprep windows close on all members);
  two solos on two boxes run side by side. That a solo start on the *worker*
  box evicts the duo depends on §1 (today the worker can look free) [A until
  PR 1's tests show it].
- A model can be offered as a solo and as a duo recipe: two catalog entries
  (different `nodes`), two runtimes, switching = recipe switch.
- Tests: a property sequence solo → duo → solo → duo on the same two boxes
  keeps I1–I4 and leaves no box double-booked; a solo regression suite runs in
  every PR of this series.

### 11. Engine death: detected, explained, reported

This section extends the existing watcher mechanisms (`_check_crash_loop`,
`_read_container_logs`, `_last_error_line`, `_maybe_auto_recover`, the live
`status`/`reason` fields, `runtime.unreachable`, `runtime.crash_loop_stopped`).
It adds no parallel log reader, reason field or restart budget.

11.1 **Planned vs. unplanned.** Every stop MC causes has a stop intent (who,
why, since): operator stop, eviction by a start (the existing `get_evicted`
marker), recipe switch, pause (`runtimes.enabled`), and **MC's own crash-loop
stop** (`_check_crash_loop`; its `runtime.crash_loop_stopped` event stays the
report for that case — it is never counted as a death a second time). The
eviction marker expires after an hour (`EVICTED_TTL`); a runtime that was
switched away therefore also gets a durable intent "not running — switched
away", **without TTL**, cleared only by its own next start. A transition to
down **without** an intent is an engine death. Slot rows (ADR-078) report
through their recipe row only, as today. "Its own next start" includes a
start MC only observes (`start.sh` by hand after a stop via MC, a restart-policy
container after a box reboot): the row's own anchor runs and the port answers,
at least 10 minutes after the stop was asked (`STOP_INTENT_SETTLE`; an engine
still answering while its stop runs is not a start). A neighbour's answer on
a shared port never lifts it.

11.2 **Detect per runtime, not per endpoint.** On the Sparks several rows
share one endpoint (`:8000`); a dead engine makes all of them unreachable at
once (live: three events for one death). "Was up" is therefore decided per row
by its own anchor (`_served_answer_is_own`: own container running / own
process alive): only a row whose last healthy probe was its **own** answer can
die. A row whose anchor never ran (the GLM row on 2026-10-19) gets no death,
no notification and no restart — whether or not its eviction marker has
expired. One engine death produces exactly one death. The slot row follows its
recipe row (11.1). Whether the last answer was a row's own is remembered
durably (days), not only in the live snapshot (3 probe intervals), so an
outage that began while the backend was down is still judged per row. A
matching model id counts as own only when no other row on the port carries
the same id; otherwise the anchor decides.

The plain `runtime.unreachable` (no death declared) fires once per endpoint
outage and stays quiet only for silence MC knows is planned: the row, or the
port's holder, was stopped on purpose. The holder is the row whose last
answer was its own, **or a row MC launched that has not answered yet**
(pending start, no TTL tied to the grace): a switch whose new recipe never
comes up (it hangs or dies while loading) ends in that one alarm when the
20-minute grace runs out, as before this section. When nothing is known about
the holder, the alarm fires. Every start MC issues opens a new window on the
port: the one-death / one-alarm keys of the outage before it are closed, so
"Restart now" or "Other model" on a dead card (including an auto-recovery
start) that never comes up ends in its own one alarm instead of silence.

**Detection target: ≤ 2 probe intervals** after the death (operator
requirement, 2026-10-19; today up to 3 intervals plus the tick offset, live
3 min 53 s). Fast path: when a row that was up (own answer, `serving_since`
set) gets **connection refused** — nothing listens, as opposed to a timeout,
which a stalling box also produces — the watcher checks the row's anchor once
(node-agent report §3.2, else one SSH `docker inspect` / process check). Anchor
gone or exited → death declared at that probe. Anchor still running, or the
answer is a timeout → the existing threshold of 3 applies unchanged (a box
mid-stall must not be declared dead). The fast path never stops or starts
anything by itself. For member anchors (§3.2) a member death is `degraded`,
or — if the recipe takes the head down with it — a death with cause
`member_down`.

11.3 **Explain**, once per death, read-only, best effort, bounded to about
10 s, by **reusing** the crash-loop readers: `_read_container_logs` (current
boot, `_CRASH_LOG_LINES` = 200) and `_last_error_line`, with the pattern list
`CRASH_LOOP_LOG_PATTERNS` extended by the recipe's
`requirements.death_markers` (e.g. the line the recipe's memory guard prints)
and a built-in list (CUDA out of memory, NCCL error, `Killed`). Patterns
explain, they never decide (as in `_check_crash_loop`). Plus the anchor's exit
state from the node-agent report (exit code, `OOMKilled`, finished at) or the
same `docker inspect` the fast path already ran, and kernel OOM from the
node-agent where available. Example: 2026-10-19 would read exit 137 + the
guard's marker line → `guard`. The result goes into the **existing** live
fields: `status=failed`, `reason=<sentence>` (as the crash-loop stop writes
them today), plus `reason_code` (`guard | oom | kernel_oom | exit |
member_down | crash_loop | unknown`), `box` and `at`. No separate
`stop_reason` field. `unknown` is a valid, honest answer.

11.4 **Report**: one notification per death (deduplicated per runtime and
start; rows sharing the endpoint add none) with a reason sentence in DE/EN
naming the box ("<runtime> stopped on <box>: the recipe's memory guard ended
the engine at 21:14"); the Runtimes card shows the reason, the time and a link
to the last log lines; the `runtime.unreachable` event carries the cause and
fires once, for the dead row. Recipe rows are probed before slot rows in each
watcher tick, so a death declared at the threshold (timeout path) is never
preceded by the slot row's plain alarm for the same outage.

11.5 **Work does not fail silently**: agents bound to a dead runtime keep
their queued work; delivery waits while the runtime is down (relates to open
PR #884, whose final shape is not decided here [A]), and every affected task in
progress gets one visible system comment naming the death and its reason.

11.6 **Restart is optional and bounded — by the existing budget.** It stays
the box's decision (`hosts.autostart_enabled`, default off, ADR-077 P3) and
stays in `_maybe_auto_recover`; there is no second restart mechanism. Changes
to it: (a) the cooldown becomes a backoff (15 → 30 → 60 min) on the existing
`AUTO_RECOVERY_COOLDOWN`; (b) `AUTO_RECOVERY_MAX_ATTEMPTS` (today 2) and the
6 h `AUTO_RECOVERY_FAILURE_TTL` become Settings values (ADR-093), default
unchanged; (c) **no** automatic restart when the same `reason_code` hits twice
in a row — the same rule the crash-loop stop already applies to Docker
restarts, now for MC's own attempts; MC stops trying and says so; (d) it
restarts only the runtime that **died** (11.2), and only when that runtime is
the box's `autostart_recipe_slug` — otherwise it reports and does nothing, so
a DeepSeek death can never start GLM. A duo restarts only as a whole recipe
start (memprep on all members, §4); MC never restarts a worker alone (§3.4).
Every attempt and every give-up is notified.

The motivating case does **not** get a restart from PR 3: the DeepSeek duo is
a hand row (§8.2: no autostart) and both Sparks have `autostart_enabled` off
[L]. Until PR 7 makes it a private recipe and the operator switches on
autostart for the box, a DeepSeek death is detected, explained and reported —
not restarted.

### Rollout

Eight PRs after this ADR (operator decision 2026-10-19: lean scope), critical
path 1 → 2 → 3: (1) `runtime_membership` + architecture test + solo regression
suite; (2) memprep on every member, start **and** end of the window;
(3) honest card, healer, `degraded`, engine-death detection/reason/report and
bounded restart; (4) node-agent reports anchors and exit state; (5) typed
placeholders, strict fabric, lint, shadowing check; (6) pre-flight register +
`requirements`/`options` migration + options block; (7) folder source, lock
on new multi-box hand rows, DeepSeek duo as private recipe, handbook;
(8) watermark hold in the node-agent.

PR 3 carries at least these tests for §11: (a) two rows share one endpoint,
DeepSeek dies while GLM has been evicted for more than one hour → exactly one
death, one notification, for DeepSeek, and no autostart of GLM; (b) a ready
engine whose probe gets connection refused and whose anchor has exited is
declared dead within 2 probe intervals; a timeout with the anchor still
running is not; (c) a crash-loop stop is reported once (as
`runtime.crash_loop_stopped`), not again as a death; (d) the same
`reason_code` twice → no third start attempt.

### Later (named, not decided here)

Device facts (`hosts.agent_facts`: interfaces, RDMA, GPU model) with "Use
detected"; transport proof (RDMA vs. TCP on the card); CPU demo duo
(llama.cpp RPC) for strangers and CI (operator: later); power-managed members
with "wake all members" (operator: not now, 422); N > 2 with per-peer fabric
addresses (`host_links`); NFS export provided by MC beyond the `nfs_export`
wrapper; recipe editor UI on top of the folder source.

## Alternativen

- **Two truths, synced both ways** → rejected: every new path must remember the sync; that is today's bug.
- **Members only in `topology` JSON** → rejected: loses FK cascade and the two unique rules of `runtime_hosts` (runtime_host.py).
- **Keep writing `topology.worker_host_id` for one release** → rejected: transition copies tend to stay; deriving it on read keeps old readers working with one stored truth.
- **Occupancy check (409) inside `set_members`** → rejected: would forbid switching between two duos, or solo ↔ duo, on the same boxes; eviction at start is the existing, correct policy.
- **Head row for solo runtimes too** → rejected: more uniform, but rewrites every existing row for no user gain; `members_of` computes the implicit head instead.
- **Healer writing in every watcher cycle** → rejected: silently changes eviction every 90 s and multiplies the race with operator starts; it writes only once at worker start, before the watcher, under the runtime row lock and skipping runtimes that are switching (§2).
- **Per-member SSH liveness probe every tick** → rejected: SSH load on the head path; node-agent report instead.
- **Per-member liveness as a second Redis status** → rejected: two answers to "is it running"; one status with `degraded`.
- **DB trigger for the invariants** → rejected: cross-table trigger, opaque errors, hard to test.
- **mDNS auto-discovery (exo)** → rejected as primary: needs a broadcast LAN, not across Tailscale; the host registry (ADR-048) stays the source.
- **Healer picks a free worker** → rejected: a wrong guess loads hundreds of GB onto the wrong box or evicts another runtime.
- **Device facts as render source / auto-fill `fabric_ip`** → rejected: an unauthenticated device would choose the address the head talks to; injection risk.
- **Keep the fabric fallback, only warn** → rejected: a box↔box address that silently becomes a LAN or WireGuard address breaks NFS mounts and RDMA without a clear error (PR #887).
- **Free-string recipe options** → rejected: options are rendered into shell; only typed values pass.
- **Root setup as a shell snippet in the recipe** → rejected: registry shell running as root on every box; a fixed vocabulary of node-agent wrappers keeps it visible and typed.
- **Peer probes through node-agent desired state** → rejected for now: new steering channel, box as LAN jump host; the head-side SSH check tests the exact path the recipe uses.
- **Storing pre-flight results in `config_checks`** → rejected: stale second truth.
- **Recipe editor and an adopt endpoint now** → rejected: two more creation paths; folder source + PATCH cover both with the existing sync and `set_members`.
- **One owner for `min_free_kbytes` that forbids memprep from writing** → rejected: lowering the watermark is memprep's purpose; a bounded hold keeps one owner.
- **Always restart a dead engine** → rejected: an engine killed by its memory guard or the OOM killer dies again on the same load; a bounded, cause-aware restart plus a clear report is safer.
- **Full N-box support now** → rejected: no recipe, selection or UI uses more than 2 boxes; half support misleads.

## Später ändern (ADR-093)

- **Members:** Runtimes → card → "Members": view, pick or replace the worker,
  per-box status. Same call and validation as the API.
- **Recipe options:** Runtimes → card → "Options" (e.g. abliterated weights,
  draft model); applied at the next start.
- **Box addresses:** Devices → box → `fabric_ip`, `ssh_user` editable,
  validated with the same types as the renderer.
- **Recipes:** add a JSON file to the local recipe folder (or a registry URL
  in Settings → Recipe sources); one sync, one lint. Catalog entries stay
  read-only.
- **Pre-flight:** shown and re-runnable in the install dialog and on the card;
  a new check is one registry entry.
- **Host tuning and root setup:** Devices → box → settings (existing
  device-state UI); a recipe's needs appear in pre-flight with a button there.
- **Restart after a death:** Devices → box → autostart switch (as today);
  attempts and backoff in Settings.
- **Adopt a hand row:** card → "Bind to recipe" (PATCH, same path).

## Konsequenzen

### Positiv
- One write path: a box can no longer look free while it serves a duo.
- Solo and duo runtimes are managed the same way; switching between them on the same boxes keeps working.
- Duo starts no longer fail on a worker's page cache, and no dropper is left behind on the worker.
- Recipes become portable: no box values in recipe checkouts; switches like abliterated weights become options in MC; the shadowing check names leftovers.
- Typed placeholders and options close a shell-injection path before new data sources arrive.
- A box↔box address is either right or a clear error.
- Failures move before the download (gated repo, space per box, arch, link, image, root setup).
- Honest card: "unknown" instead of a false "running"; `degraded` instead of silence; a dead engine comes with its reason.

### Negativ
- One additive migration (`local_recipes.requirements`, `local_recipes.options`).
- Worker liveness and exit state need a node-agent update; boxes without it show "unknown".
- Container anchors depend on the agent reading Docker (§3.2, not verified).
- Catalog lint and the strict fabric rule may mark existing entries; needs the dry run first.
- Power-managed boxes cannot be duo members until "wake all members" exists.
- N > 2 stays out; ranks in the DB are ahead of the rest of the system.
- The first backend start after deploy may write member rows and thereby change eviction for runtimes that share a box (announced per runtime).
- Cause detection is best effort; some deaths will honestly read `unknown`.

## Referenzen

- backend/app/services/recipe_switcher.py:375, :514, :648, :1044, :1102, :1137, :1246, :1325–1345, :1406, :1447–1470, :1576
- backend/app/routers/runtimes.py:1266, :1296, :1321; PR #878 (merged)
- backend/app/services/runtime_multinode.py:34, :49–78
- backend/app/models/runtime_host.py (unique rules, docstring)
- backend/app/services/recipe_env.py:56–72, :95–120
- backend/app/services/launch_template.py:131
- backend/app/services/host_memory_prep.py:13, :49, :250, :345–352, :729, :831, :870, :951; runtime_manager.py:2231, :2254, :2258
- backend/app/services/runtime_watcher.py:89, :98–104, :126–144, :167, :285, :342–369, :403, :604, :732–880, :911, :937, :1024–1110, :1283
- backend/app/services/runtime_grace.py:85 (`get_switching`), :112 (`EVICTED_TTL`), :142 (`get_evicted`); backend/app/config.py:499 (`runtime_watcher_interval`)
- backend/app/background.py:199; docker-compose.yml (API: `ENABLE_BACKGROUND_SERVICES=false`, background services in `mc-worker`)
- backend/app/services/config_checks/registry.py; backend/alembic/versions/0218_config_checks.py
- backend/app/services/local_registry.py:86, :136
- backend/app/routers/nodes.py (pairing ignores device `ssh_host`); routers/hosts.py (`_validate_fabric_ip`)
- scripts/node-agent/mc-node-agent.py (wrappers, `min_free_kbytes` reconcile)
- frontend-v2/src/app/runtimes (FleetStage, Stage)
- Live: DeepSeek duo recipe `scripts/config.sh` (local.sh sourced, wins over `.env`; `.env` parsed, not run); PR #887 (fabric fallback and missing root-setup field)
- Verwandte ADRs: ADR-042, ADR-048, ADR-054, ADR-077 (+P3, P4), ADR-078, ADR-086, ADR-093
- External: LeaderWorkerSet, SGLang `--node-rank`, NVIDIA Dynamo, GPUStack, DGX Spark playbooks, hf-preflight
