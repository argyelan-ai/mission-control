# ADR-094 — Self-update from Settings: one engine, the bridge as door, the journal as truth

**Status:** Accepted
**Datum:** 2026-10-17
**Scope:** Install · Infra/Release · Backend/API · Backend/DB · Frontend/Settings · cli-bridge

## Kontext

Today a user updates MC only from a terminal: `install.sh --update` checks
out the latest release tag, pins `MC_IMAGE_TAG`, pulls images and runs
`docker compose up -d` (ADR-092). The UI only shows a hint
(`GET /api/v1/system/version`, `backend/app/routers/system.py`). ADR-093
asks how a user changes things later without a terminal; for the MC version
itself the answer so far was "not at all".

The terminal path also has gaps that matter more once a button can start it:

- It reports "Update complete" right after `up -d` (`install.sh`), before
  anything has proven healthy. There is no automatic way back.
- No backup is taken; `backup.sh` is a separate, manual step.
- `mc_latest_release_tag` falls back to the highest git tag when the GitHub
  API fails (`install.sh`) — fine with a human watching, too soft for a
  button.
- `boot_stack` pulls *every* service, so third-party images on `:latest`
  (qdrant, livekit in `docker-compose.yml`) change silently and cannot be
  rolled back.
- The agent fleet file is parked inside `.git/` during the checkout; a crash
  between parking and copying back loses it.
- Nothing survives a power cut mid-run: after a reboot Docker restarts the
  *old* containers on the *new* checkout.

A self-update replaces code and database schema on machines we do not see.
The update plan compared four designs (an updater container, a host helper
service, a supervisor, the existing bridge) with two juries and an external
architecture review; the operator approved its recommendations on
2026-10-17.

## Entscheidung

1. **The MC stack may be updated from the UI.** Settings → System shows the
   available release, a preflight, the run, the result and the history.
   Docker itself, system packages, host services and the bridge are **not**
   updated from the UI — they stay with the installer (ADR-093 decision 3).

2. **Exactly one update engine** — `scripts/mc_update.py`, Python standard
   library only — for terminal and UI. `install.sh --update`, `--rollback`
   and `--resume` become a thin door that calls it, so terminal users get the
   same safety nets. No second lifecycle. From the `switch` phase on, the
   engine runs the copy of itself taken from the **target tag**
   (`git show <tag>:scripts/mc_update.py`), so a checkout never replaces the
   running script under itself.

3. **Phases and the journal.** `preflight → drain → fetch → verify_release →
   pull → backup → switch → up → verify → done`; on failure
   `rolling_back → rolled_back` or `rollback_failed`; stopping before
   `switch` ends `aborted` (nothing changed). Every phase writes its intent
   *before* and its result *after* the change into a journal on the host
   (`~/.mc/update/journal/<run-id>.json`, atomic temp file + `fsync` +
   `rename`). The journal is the truth; Postgres keeps the history, Redis only
   mirrors it for the UI. A file lock with PID and start time allows one run
   at a time, also against a parallel terminal update.

4. **Mandatory backup, success only after four checks, rollback by
   restore.**
   - The backup is taken **after** the pull and **right before** `switch`,
     so the window in which data is written after the backup shrinks to
     seconds. It uses the backup feature's location and rules
     (`MC_BACKUP_DIR` outside the repo and outside `~/.mc`; `.env` only
     encrypted with the user's passphrase) under its own prefix
     `mc_backup_preupdate_*`, kept apart from the daily rotation (3 runs,
     14 days). For an unattended rollback the run keeps its own copy of
     `.env` and the fleet file in the journal (file mode 0600, never leaves
     the machine, removed with the run).
   - An update counts as successful only when (1) `backend`, `mc-worker`
     and `frontend` are `healthy`, (2) each of these containers runs exactly
     the pulled target image and its `RepoDigests` contain the signed
     digest, (3) `/api/v1/system/version` reports the target version, and
     (4) `alembic_version` equals the target tag's head. The health wait
     counts failures only after the backend's `start_period`; limit 12 min,
     25 min on arm64.
   - **R2 rule against `schema_before`:** before `switch` the engine records
     `alembic_version` as `schema_before`. On failure:
     `alembic_version == schema_before` → **R1** (old tag, old `.env`
     written in place, old fleet file, `up -d`, four checks against the old
     version); anything else, including "cannot read it" → **R2** (stop
     `backend` **and** `mc-worker`, restore the database from the pre-update
     backup, then R1, then also check `alembic_version == schema_before`).
     Comparing against the *new* head would miss a migration chain that
     stopped half-way.
   - Never a migration downgrade; the way back is always a restore.
   - `.env` is always written in place (it is mounted into containers as a
     single file; a `rename` would leave the mount on the old inode).
   - Old images are deleted only after the new version ran healthy for 24 h.
   - The manual "back to the previous version" in the UI is limited to the
     run's **own** pre-update backup within its 14 days, behind step-up and
     with the warning "everything since <time> is lost". A general restore
     of arbitrary backups stays out of the app.

5. **Target integrity** — only stable, only forward, only published
   releases, only signed images, gated releases:
   - **Release gate on GitHub:** protected `v*` tags (maintainers only) and a
     release workflow with a `release` environment that needs an explicit
     approval. The workflow stops when the environment is missing.
   - **Only published, non-prerelease releases** from the GitHub API, with
     **no** fallback to git tags. The button's preflight is fail-closed: API
     unreachable → red, "release list not available". The terminal may keep
     its fallback with a warning.
   - **cosign keyless signing is mandatory before the first release that
     ships the button.** CI signs the images; the engine verifies with a
     cosign container pinned by digest in the repo
     (`docker run --rm <cosign>@sha256:… verify …`) — no host install. If no
     arm64 cosign image exists, the fallback is a static cosign the installer
     installs after an explicit yes.
   - **Digest check without changing the tag format:** `MC_IMAGE_TAG` stays
     `X.Y.Z`. After the pull the engine compares the local image's
     `RepoDigests` with the signed digest; `up -d` uses exactly that image.
     The image's `revision` label must equal the tag's commit, which ties the
     signed build to the engine code taken from that tag. The engine checks
     that an image exists for this machine's architecture.
   - The target is chosen by the engine. A target sent by the backend
     (`expected_target`) is only compared; a mismatch is refused (`409`).
   - **What the signature proves, literally:** an image was built by this
     repository's CI, by workflow `release.yml`, on a tag. It protects
     against a compromised registry, re-tagging and a swapped image. It does
     **not** protect against a release that was approved on purpose but is
     malicious or broken, nor against a compromised maintainer account.

6. **The bridge is the door and the owner of resumption.** No new
   long-running service.
   - The cli-bridge on the host gets exactly five routes under
     `/mc-update/*` — `preflight`, `start`, `status`, `rollback`, `resume` —
     with its own token scope, no free commands, no free tag, ref or URL.
     One run at a time (`423`); at most 3 **starts** per 24 h (`429`,
     `MC_UPDATE_MAX_STARTS_PER_DAY`); preflight, status, resume, rollback
     and starts that failed before `switch` on network or download do not
     count.
   - The engine runs detached from the bridge (Linux: `systemd-run --user`
     with its own cgroup and `KillMode=process` in the bridge unit; macOS:
     double fork + `setsid` and `AbandonProcessGroup` in the plist), so a
     bridge restart does not kill a run.
   - **Resumption:** on start the bridge reads the journal. If a run is
     neither `done`, `aborted`, `rolled_back` nor `rollback_failed` and its
     lock PID is dead, it waits for `docker info` (max. 10 min) and starts
     the engine in resume mode. **Resume rule:** state before `switch` →
     abort (`aborted`, drain off), never "carry on blindly"; state `switch`,
     `up` or `verify` → put the fleet file and `.env` back from the journal
     if missing, `up` for the MC services, the four checks with a fresh
     limit, then `done` or R1/R2; state `rolling_back` → run the way back
     again from the start (idempotent).
   - Without a bridge (terminal users) `install.sh` detects the open run and
     offers `--resume`; the UI shows that command.
   - On Linux the installer sets `CLI_BRIDGE_BIND` to the Docker gateway IP so
     the backend container can reach the bridge; the token stays the actual
     protection.

7. **Drain in Postgres, one Docker guard.**
   - The drain ("no new work") lives in Postgres (`system_state`), not
     Redis: it travels with the data the backup holds and R2 restores, it is
     one truth instead of two, and a deliberate `FLUSHALL` must not lift it.
     It carries the run id and an expiry; the bridge's start routine lifts an
     expired drain without an active run and records that in the history.
   - "Running work" = tasks `in_progress`, active group-chat rounds, active
     loops, and scheduled jobs due within 15 minutes. In the drain, dispatch,
     group runner, loop runner and scheduler start nothing new; due jobs are
     caught up after the update. Agent work is never cancelled, and there is
     no "update anyway" button.
   - Every backend call that creates, replaces, starts, restarts or removes
     containers (`compose up`, `run`, `create`, `restart`, `rm`) goes through
     **one** gateway that reads the drain (10 s cache) and answers
     "deferred" while it is set. Read-only calls (`ps`, `inspect`, `logs`)
     stay free. A test fails when code calls `docker` directly outside the
     gateway.
   - As a second belt the engine stops `mc-worker` at the start of `switch`.
   - The engine uses the **real** compose project name of the running stack;
     if it differs from the backend's fixed `mission-control`, preflight
     shows a yellow notice (not a block).

8. **Only MC images change.** The engine pulls `backend` and `frontend`
   (`mc-worker` shares the backend image). Third-party images keep their
   versions and are pinned in `docker-compose.yml` instead of `:latest`.
   Agent images are only pinned for the next recreate; running agents are not
   touched.

9. **Who may start it.** Admin role plus a fresh password confirmation
   (step-up, single use, valid 5 minutes) for `start` and `rollback`.
   Requests authenticated with `LOCAL_AUTH_TOKEN` are refused on these
   routes, and accounts without a password get no button in v1 (the UI
   points to the terminal). The UI never receives a token or the raw log,
   only filtered log lines.

10. **Exempt:** the **edge** channel (local builds of `main`) is updated in
    the terminal only. The **operator's own install** sets
    `MC_UPDATE_MANAGED=deploy`: no button, API and bridge answer `409`; it is
    updated only through the deploy procedure (ADR-092).

11. **Threat model, honestly.** A compromised backend is close to host root
    today, independent of this feature: it writes `volumes:` entries into
    the fleet file, that file is mounted writable, and it applies it with
    `docker compose up --force-recreate` through the socket proxy. This ADR
    does not change that and does not claim to; ADR-095 is the track that
    closes it. The security value of the self-update is **target
    integrity** (decision 5) and a narrow door (decision 6), not a stronger
    backend boundary. Bridge versus a separate narrow update service is
    re-evaluated once ADR-095 has landed.

12. **Target picture: release slots.** Later (update plan PR 9, after the
    release gate), each release lives in its own slot (`releases/<tag>` as a
    git worktree, a `current` symlink switched with one atomic `rename`), and
    mutable state — fleet file, `.env`, certificates, backups — moves out of
    the checkout. v1 must not make this harder: no new mutable state inside
    the checkout, paths resolved through `MC_REPO_PATH`.

13. **Release gate for the button itself.** The button ships in a release
    only after the engine, backup/R2, the terminal door with its crash
    matrix, the bridge door, the Docker gateway, step-up, the API, the UI,
    signing and the restart page are merged and accepted on test machines
    (Raspberry Pi, Linux x86, a macOS test install — never the operator's
    production install). Until then it is behind a feature switch, off. The
    first update *to* the button version still runs in the terminal; the
    release notes say so.

## Alternativen

- **Updater container with the Docker socket** → Rejected: it makes the proxy
  hole official, a container with a raw socket "only for updates" is host
  root, and `docker logs` is no journal that survives a power cut.
- **A separate narrow host helper service** → Rejected for now: the cleanest
  boundary on paper, but a second service in the same user account adds
  little while the backend is already close to host root, and it costs a
  second lifecycle. Re-evaluated after ADR-095.
- **A supervisor service** → Rejected: permanent RAM on a Pi, blind with
  `BUILD=0` in its fallback, the repo writable inside the container and a
  delicate self-update. Its journal discipline and success checks are taken
  over.
- **Engine in bash** → Rejected: nine phases, two ways back and resumption
  after a crash are a state machine that bash cannot test well. The bridge
  already needs python3, so Python adds no dependency where the button
  exists.
- **Signature as a digest file next to the images** → Rejected: it comes
  through the same channel as the image and proves little.
- **`MC_IMAGE_TAG=X.Y.Z@sha256:…`** → Rejected: it breaks the published-image
  check and the agent renderer; the digest is checked after the pull instead.
- **Drain flag in Redis** → Rejected: see decision 7. (Redis does keep the
  flag across restarts thanks to AOF; that is not the reason.)
- **Auto-update / Watchtower** → Rejected: an update is always a click (or a
  command) by a person.

## Später ändern (ADR-093)

Settings → System is the one place: the user sees the installed and the
available version, runs the preflight, starts the update, follows its phases,
reads the history (who updated when from which version to which) and goes
back to the previous version within 14 days. The start limit
(`MC_UPDATE_MAX_STARTS_PER_DAY`) and the backup location (`MC_BACKUP_DIR`,
shared with the backup feature) stay installer/`.env` values on purpose:
they are host-level choices, read by the engine and the bridge, not by the
browser. Disabling the button for an install is `MC_UPDATE_MANAGED=deploy`.
Bridge, Docker and host packages are changed through the installer (ADR-093
decision 3). No registry entry is needed: there is exactly one engine.

## Konsequenzen

### Positiv
- Users update from the phone with a backup, a proven result and an automatic
  way back; terminal users get the same safety nets.
- A run survives backend restarts, bridge restarts and power cuts; the bridge
  finishes or rolls back on its own.
- Only gated, published, signed releases can be installed through the button.
- Third-party images stop changing silently.
- Step-up and the Docker gateway are reusable building blocks (vault export,
  user deletion; any future drain).

### Negativ
- The engine is the most critical code MC has and runs on machines we do not
  see. It needs a crash matrix (kill at every journal boundary), an end-to-end
  run "release N-1 → N" and acceptance on three platforms.
- A rollback after a migration loses what was written since the backup —
  seconds, but webhooks and chat messages in that window are gone; the
  warning says so.
- The Docker gateway is a refactor across the backend.
- cosign is a new third-party component (PRINCIPLES §2 rule 5, chosen on
  purpose) and every release needs one more approval click on GitHub.
- The release gate depends on GitHub settings that are not in the repo
  (protected tags); `docs/setup/updating.md` describes them.
- `rollback_failed`, or no bridge after a power cut, still needs one terminal
  command (`./install.sh --rollback` / `--resume`). There is deliberately no
  emergency page over the network.
- The proxy hole stays open until ADR-095 is implemented.

## Nachtrag (2026-10-19) — what PR 4c implements differently from decision 7 and 9

Decided while building the backend side (update plan PR 4c); this section is
the binding version where it differs from the text above.

- **Running work = tasks `in_progress` + open group-chat rounds.** One SQL
  function, `update_running_work()` (migration 0219), shared by the engine
  and the UI. **Loops** are not counted as such: a loop runs for hours and
  would block every update. Its current round is a task (counted); the next
  round waits for the drain. **Scheduled jobs due within 15 minutes** are not
  waited for either: they are a red preflight check, and `start` refuses
  with `422` while one is due. Jobs that come due during the drain are
  recorded and run afterwards.
- **The update drain also holds continuation flows** (review and test
  hand-offs, rework), unlike the Redis `DRAINING` mode: the code switch
  follows as soon as running work reaches zero. Nothing held back is lost:
  a held dispatch is recorded with the card's status and assignee and
  delivered after the drain if the card is still there; a developer's
  hand-in for review is recorded as its own kind and the hand-off (reviewer
  choice included) runs after the drain while the card is still on `review`
  with its author. What the catch-up drops because the card moved on is
  logged and shown as an activity event (`system.update_drain_skipped`).
- **Step-up on every `rollback` call, a cancel included.** Without a live run
  the same call performs a real rollback; a cheaper cancel would race with
  the run ending. When the bridge provably did not get the request
  (connection refused), the confirmation is handed back with its original
  expiry, so a retry needs no second password; after a lost answer it stays
  spent.
- **Missing step-up answers `403 step_up_required`, not `401`** (decision 9
  of the step-up building block: a 401 signs the UI out).
- **Binding for the engine (update plan PR 6):** after `drain_set` the engine
  waits a short settle time (at least the Docker gateway's drain cache, 10 s)
  before it counts running work. Without it, a dispatch that read "not
  drained" just before the drain was set, or a gateway call served from its
  cache, can still start work after the count reached zero.

## Referenzen

- Betroffene Dateien (planned): `scripts/mc_update.py`, `install.sh`,
  `backup.sh`, `scripts/cli-bridge.py`, `scripts/cli-bridge-service.sh`,
  `backend/app/services/docker_gateway.py`, `backend/app/routers/system.py`,
  `backend/app/routers/auth.py`, `.github/workflows/release.yml`,
  `caddy/Caddyfile`, `docker-compose.yml`, `docs/setup/updating.md`
- Today's behaviour referenced above: `install.sh` (`mc_latest_release_tag`,
  `boot_stack`, `with_fleet_carried`, "Update complete"),
  `backend/app/routers/system.py` (`/system/version`),
  `backend/app/services/docker_agent_sync.py` (`COMPOSE_PROJECT_NAME`,
  `--force-recreate`), `scripts/cli-bridge.py` (`CLI_BRIDGE_BIND` default),
  `backup.sh` (`KEEP_LAST`, restore stops only `backend`)
- Verwandte ADRs: ADR-092 (release channels), ADR-093 (changeable in MC),
  ADR-095 (socket proxy hardening), ADR-043 (open-source release contract)
- Externe Quellen: Sigstore cosign keyless signing
  (https://docs.sigstore.dev/cosign/signing/overview/)
