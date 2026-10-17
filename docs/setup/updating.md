# Updating Mission Control

MC never updates itself — you decide when. The UI shows a hint under
**Settings → Über** when a newer release exists (checked once a day against
GitHub Releases; works offline, the hint just stays silent).

## The short way

```bash
cd <your-mission-control-directory>
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash -s -- --update
```

Running the installer from `main` means you always get the newest updater,
whatever version is installed. (`./install.sh --update` works the same once
your checkout carries this updater.)

What it does on the default **stable** channel:

1. looks up the latest release (GitHub API; when that is rate-limited or
   offline, the highest `vX.Y.Z` tag of the repository),
2. checks out exactly that tag — your agent fleet file is carried across,
3. pins `MC_IMAGE_TAG` and `MC_AGENT_IMAGE_TAG` in `.env` to the same
   version (`0.3.0` for `v0.3.0`),
4. pulls those prebuilt images (local build of the same tag if that fails),
   restarts the stack — migrations run automatically on backend start.

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
targets one specific release instead of the latest.

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
