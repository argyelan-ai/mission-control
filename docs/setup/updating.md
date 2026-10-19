# Updating Mission Control

MC never updates on its own — no auto-update, nothing in the background.
You decide when: today by running the command below; later also with a
click under **Settings → System**, where the click is that decision (see
[ADR-094](../decisions/094-self-update-from-settings.md) — the button is not
shipped yet). The UI shows a hint under **Settings → Über** when a newer
release exists (checked once a day against GitHub Releases; works offline,
the hint just stays silent).

The button will reach your machine through the **cli-bridge** — the helper
service that also runs host agents. Set it up once, from inside the install
folder: `./install.sh --bridge` (the installer also offers it after an install
or update). On Linux it asks to enable *linger* (`sudo loginctl
enable-linger <user>`), so the bridge keeps running after logout and after a
reboot — that is what lets an update interrupted by a power cut finish on its
own. Details: [cli-bridge-auth.md](cli-bridge-auth.md#the-update-door-mc-update-adr-094).

Two switches in `.env` decide whether the button can appear at all (both are
read by the backend and the worker; recreate them after a change):

| Setting | Effect |
|---|---|
| `MC_SELF_UPDATE_ENABLED` | `false` (default) until the button has passed its tests on real machines; while it is off, the update API answers `404` and no button is shown. |
| `MC_UPDATE_MANAGED=deploy` | For an install that is updated only through its own deploy procedure: no button, ever — the update API **and** the bridge door answer `409`. Wins over the switch above. |

While an update runs, MC hands out no new work: running tasks and group-chat
rounds finish, nothing new starts, and scheduled jobs that come due are run
right after the update instead.

## The short way

```bash
cd <your-mission-control-directory>
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash -s -- --update
```

Running the installer from `main` means you always get the newest updater,
whatever version is installed. (`./install.sh --update` works the same once
your checkout carries this updater.)

On the default **stable** channel `--update` hands over to the **update
engine** (`scripts/mc_update.py`, [ADR-094](../decisions/094-self-update-from-settings.md))
— the same program the button in Settings → System will start. The terminal
shows the engine's own lines. It needs `python3` (3.9 or newer — macOS ships
it; on Linux `sudo apt install -y python3`); the installer checks that with
its other prerequisites. `MC_PYTHON=python3.11` picks another python.

In this order:

1. **checks** — nothing changes yet: an installed folder, channel stable, no
   other update running or unfinished, Docker answers, no second Mission
   Control stack, no local changes in tracked files, the latest **published**
   release (GitHub API; when it cannot be reached the update stops — pick a
   release yourself with `--version vX.Y.Z`), never backwards, the database
   answers, free disk space,
2. **waits for running work**: no new work is handed out, tasks already in
   progress finish first (up to 30 minutes, `MC_UPDATE_DRAIN_TIMEOUT`).
   Agents are never cut off — there is no "update anyway",
3. **downloads** only Mission Control's own images (`backend`, `frontend`);
   qdrant, livekit, Postgres and the other third-party images keep their
   version. A failed download changes nothing,
4. **backs up** with `./backup.sh preupdate` right before the switch:
   database, pins and — with a passphrase set — the encrypted `.env`, under
   its own prefix `mc_backup_preupdate_*` (newest 3, at most 14 days; the
   daily rotation never removes them). No backup → no update,
5. **switches**: checks out exactly that tag (your agent fleet file is
   carried across), pins `MC_IMAGE_TAG` / `MC_AGENT_IMAGE_TAG` in `.env` (in
   place, so running containers see it), runs `./setup.sh` for new `.env`
   keys,
6. **starts** `backend`, `mc-worker` and `frontend` with exactly the images
   it downloaded — migrations run on backend start,
7. **proves** the new version: all three containers healthy, each running
   the downloaded image, `/health` reporting the new version, the database on
   the release's schema — up to 12 minutes (25 on ARM boards), the backend's
   5-minute start period does not count as failure.

Only then: **"✅ Update complete"** with the name of the pre-update backup.
Anything else **goes back on its own** — the previous release, its `.env`
and fleet file, and, when the database already changed, the database from
the pre-update backup (everything written since that backup is lost — the
message says from when):

```text
✗ new version: not healthy after the start period — backend=unhealthy
→ going back to v0.3.0 …
→ the database changed (schema 0219, before 0218) — restoring the pre-update backup mc_backup_preupdate_2026-10-17_09-00-00.sql.gz …
✓ previous version: healthy, expected images, version 0.3.0, schema 0218
✗ Update failed (…). Mission Control was set back to v0.3.0; the database was restored from the pre-update backup … — everything written since 2026-10-17T07:00:00Z is lost.
  What failed and why: docker compose logs backend --tail 50 · the run's log: ~/.mc/update/run-<id>/engine.log
```

Every run keeps a journal in `~/.mc/update/` (`MC_UPDATE_HOME`), so it
survives a closed terminal or a power cut:

- **`./install.sh --resume`** finishes an interrupted run: before the switch
  it stops with nothing changed, after it it completes the update or goes
  back. `--update` finds an unfinished run first and offers the resume (at a
  terminal; otherwise it names the command). A re-run of the installer never
  boots the stack on top of one.
- **`./install.sh --rollback`** undoes the last update within 14 days (its
  own pre-update backup) — it asks first; unattended: `--rollback --yes`.
  When a way back itself failed (`rollback failed`, rare), fix the cause the
  message names and run `--rollback` again.

It refuses to start when another Mission Control stack runs on the machine,
and on an install marked `MC_UPDATE_MANAGED=deploy` (updated through its own
deploy procedure). `--no-backup` and `--ignore-running-tasks` exist only for
the edge channel.

**Installed before release pinning?** Older installers cloned `main`. On
the first `--update`, such an install moves to the latest release only if
its database schema is part of that release; otherwise the update stops
before anything changes — stay on `main` for now:
`./install.sh --update --channel edge` (local build), until the next
release catches up.

**Developers:** `--channel edge` (alias `main`) follows the `main` branch and
builds the images locally (`MC_IMAGE_TAG=edge`); this path stays in the
installer — it backs up with `./backup.sh` first (`--no-backup` skips it),
stops while tasks run without a terminal to confirm (`--ignore-running-tasks`
goes ahead) and waits for `/health`; on failure it prints the commands back. Not recommended for users —
`main` is unreleased and can break between releases. `--version vX.Y.Z`
targets one specific release instead of the latest. Channels and their
rules: [ADR-092](../decisions/092-release-channels.md); how releases are cut:
[releasing](releasing.md).

## What an update accepts

The in-app update (ADR-094, not shipped yet) installs a release only when all
of this holds, and stops before changing anything otherwise:

- it is a **published, non-prerelease** GitHub release — read from the GitHub
  API only; when the API cannot be reached, the update does not start (the
  terminal `--update` keeps its fallback to git tags, with a warning),
- it is **newer** than the installed version,
- `mc-backend` and `mc-frontend` are **signed** by this repository's release
  workflow for exactly that tag and commit, and exist for this machine's
  architecture (a Raspberry Pi needs arm64),
- after the pull, the local images are the signed ones (digest) and were
  built from the tag's commit (`revision` label).

The signature check runs cosign in a container pinned by digest — nothing to
install. What the signature proves and what not: [releasing](releasing.md#what-the-signature-proves--literally).
Check a release yourself: `python3 scripts/mc_update_verify.py release vX.Y.Z --repo-path .`

## Manually

```bash
git fetch origin tag v0.3.0            # the release you want
scripts/migrate-agents-yml.sh save     # no-op unless your fleet file is still tracked
git checkout v0.3.0
scripts/migrate-agents-yml.sh restore
# in .env: MC_IMAGE_TAG=0.3.0 and MC_AGENT_IMAGE_TAG=0.3.0 (no "v")
docker compose pull backend frontend   # or: docker compose up --build -d
docker compose up -d                   # migrations run automatically on backend start
```

## One-time: your agent fleet leaves version control

`docker/docker-compose.agents.yml` describes *your* machine — agent names,
project references, mount paths — and Mission Control rewrites it while it
runs. It used to sit in the repo anyway, so every commit published its
author's fleet. From this release on, the repo ships only the agent-free
template `docker/docker-compose.agents.example.yml`; your copy is yours and
is git-ignored.

If you installed before this release, the plain `git pull` above does one of
two things, both bad: it deletes your file without a word (when it happens to
match the committed version), or it refuses to pull at all (when it doesn't).
Carry it across by hand — four commands, nothing to install first (the helper
script only arrives *with* this pull, so it cannot rescue the pull that brings
it):

```bash
cp docker/docker-compose.agents.yml ../my-agent-fleet.yml   # 1. keep a copy
git checkout -- docker/docker-compose.agents.yml            # 2. let the pull through
git pull --ff-only                                          # 3. the file disappears here
cp ../my-agent-fleet.yml docker/docker-compose.agents.yml   # 4. put yours back
```

Step 2 is the one that matters: it puts the committed version back so the pull
has nothing to trip over, while your real file waits outside the repo. After
step 4 the file is untracked and git-ignored — hand-added mounts and all.

From then on you never think about it again: `install.sh --update` (the
update engine, which keeps a copy in its run journal) carries the file across
every switch itself (copy aside, clean path for git, switch,
copy back) — in both directions, also onto an older release that still
tracked it. `scripts/migrate-agents-yml.sh save` / `restore` do the same for
the manual path above.

## Pinning a version

Every install is pinned: `install.sh` writes `MC_IMAGE_TAG` (and
`MC_AGENT_IMAGE_TAG`) into `.env`, matching the checked-out release. Change
versions with `--update --version vX.Y.Z`, never by editing `MC_IMAGE_TAG`
alone — images of one release on the code of another is the mismatch that
breaks the stack (e.g. a worker service whose module the image lacks). Check
the [CHANGELOG](../../CHANGELOG.md) before jumping versions.

## Before big jumps

- Migrations are forward-only — take a database backup first (`./backup.sh`).
- Read the release notes; breaking changes are called out there.

## Coming from the fleet model?

If your install predates [ADR-085](../decisions/085-head-per-job.md)
(22.09.2026), updating does not touch your data — your agents, boards and
tasks all stay exactly as they were. What changes is which agents receive
*new* work:

- Every agent you had keeps running if it was already running; none of them
  are deleted, renamed or reconfigured by the update.
- New work after the update goes to whichever agent has `is_board_lead`
  (your **lead agent**) and, for local jobs, to a local **omp agent** — not
  spread across your old role-based fleet the way it used to be.
- Nothing forces you onto this: if you don't touch the new chat features or
  the `HEADS_ENABLED`/`NIGHT_SHIFT_ENABLED`/`VOICE_LIVE_ENABLED` flags
  (all default off), your install keeps behaving the way it always did.
- Read [Heads and remote control](../handbook/concepts/heads-and-remote-control.md)
  for what actually changed and why, and [the FAQ](../handbook/faq.md#where-did-the-agent-fleet-go)
  for the short version.
