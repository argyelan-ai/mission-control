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

## The short way

```bash
cd <your-mission-control-directory>
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash -s -- --update
```

Running the installer from `main` means you always get the newest updater,
whatever version is installed. (`./install.sh --update` works the same once
your checkout carries this updater.)

What it does on the default **stable** channel:

1. **stops first when agents are working**: it counts the tasks in progress.
   With a terminal it asks whether to go ahead; without one (cron, scripts)
   it stops. `--ignore-running-tasks` goes ahead anyway — the restart
   interrupts those runs,
2. looks up the latest release (GitHub API; when that is rate-limited or
   offline, the highest `vX.Y.Z` tag of the repository) — and skips a
   release whose images are not published yet, taking the newest one that
   has them,
3. **takes a backup** with `./backup.sh` right before the switch. When the
   backup fails, the update does not start. `--no-backup` skips it — only
   when you have a fresh backup of your own. The backup needs the database:
   **a stopped stack now stops the update** (before, `--update` just started
   it). Start the database first (`docker compose up -d db`) or pass
   `--no-backup`. The backup is a normal `./backup.sh` run, so it also
   archives `~/.mc` (can be several GB) and rotates old backups — the
   update may remove your oldest backup (up to 0.3.x: `./backups`, the last
   4 are kept; newer versions: `MC_BACKUP_DIR`, `MC_BACKUP_KEEP`),
4. keeps a copy of your current `.env` as `.env.backup-pre-update`
   (git-ignored, owner-only) for the way back,
5. checks out exactly that tag — your agent fleet file is carried across,
6. runs `./setup.sh`, which adds `.env` keys the new version needs (it never
   regenerates an existing `.env`),
7. pins `MC_IMAGE_TAG` and `MC_AGENT_IMAGE_TAG` in `.env` to the same
   version (`0.3.0` for `v0.3.0`) — written in place, so running containers
   that mount `.env` see the change,
8. pulls those prebuilt images (local build of the same tag if that fails),
   restarts the stack — migrations run automatically on backend start,
9. **waits until the backend answers `/health`** — up to 6 minutes, longer
   than the 5 minutes a slow first boot is allowed (`MC_HEALTH_TIMEOUT`
   changes it).

Only then does it say **"Update complete"**. Anything else ends with
**"Update failed"**, the backup file it took and the commands back, for
example:

```text
✗ Update failed — Mission Control is not confirmed running on the new version.
  Backup taken right before the switch: ~/mc-backups/mc_backup_2026-10-17_09-00-00.sql.gz
  Re-running the same update is safe. To go back to v0.3.0:
    git checkout -q v0.3.0
    cp .env.backup-pre-update .env
    ./backup.sh restore ~/mc-backups/mc_backup_2026-10-17_09-00-00.sql.gz
    docker compose up -d
```

The restore line only appears when the database schema changed during the
failed update; otherwise it says that no restore is needed.

It refuses to start when another Mission Control stack runs on the machine
(a second stack would fight it for ports 80/8000), or when a stack with the
same compose project name runs from a different directory.

Code and images therefore always come from the same release. It refuses to
move backwards: no lower release than the installed one, and no release
whose migrations do not include your database's current schema.

**Installed before release pinning?** Older installers cloned `main`. On
the first `--update`, such an install moves to the latest release only if
its database schema is part of that release; otherwise the updater stops
and tells you to stay on `main` for now:
`./install.sh --update --channel edge` (local build), until the next
release catches up.

**Developers:** `--channel edge` (alias `main`) follows the `main` branch and
builds the images locally (`MC_IMAGE_TAG=edge`). Not recommended for users —
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

From then on you never think about it again: `install.sh --update` carries
the file across every switch itself (copy aside, clean path for git, switch,
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
