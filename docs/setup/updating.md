# Updating Mission Control

MC never updates itself — you decide when. The UI shows a hint under
**Settings → Über** when a newer release exists (checked once a day against
GitHub Releases; works offline, the hint just stays silent).

## The short way

```bash
cd <your-mission-control-directory>
./install.sh --update
```

That pulls the latest code, refreshes the images (prebuilt from GHCR when
available, local rebuild otherwise), restarts the stack and runs database
migrations.

> **Crossing the release that takes the agent fleet out of version control?**
> Do the four commands under *Your agent fleet leaves version control* below
> FIRST — the `install.sh` sitting in your checkout does not know about that
> release yet and stops at the pull.

## Manually

```bash
scripts/migrate-agents-yml.sh save     # no-op unless your fleet file is still tracked
git pull
scripts/migrate-agents-yml.sh restore
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

From then on you never think about it again: `./install.sh --update` wraps
every pull in `scripts/migrate-agents-yml.sh save` / `restore`, which do
exactly the above and are no-ops once your file is untracked.

## Pinning a version

Set `MC_IMAGE_TAG` in `.env` (e.g. `MC_IMAGE_TAG=0.1.0`) to pin the GHCR
images instead of following `latest`. Check the
[CHANGELOG](../../CHANGELOG.md) before jumping versions.

## Before big jumps

- Migrations are forward-only — take a database backup first (`./backup.sh`).
- Read the release notes; breaking changes are called out there.
