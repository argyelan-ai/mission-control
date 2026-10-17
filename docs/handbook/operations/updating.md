# Updating

Mission Control never updates itself. The UI hints under **Settings → Über**
when a newer release exists (checked once a day against GitHub Releases,
silent when offline) — applying it is your call.

Full reference: [docs/setup/updating.md](../../setup/updating.md).

## The short way

```bash
cd <your-mission-control-directory>
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash -s -- --update
```

This always runs the newest updater, whatever version you have installed.
(`./install.sh --update` / `make update` work too once your install is on a
release that ships this updater.)

`--update` must run inside an installed checkout (it checks for
`docker-compose.yml` + `.env`). On the default **stable** channel it looks up
the latest release, checks out exactly that tag, pins `MC_IMAGE_TAG` in `.env`
to the same version, pulls those images and restarts — code and images always
match. It falls back to a local build of the same tag when the images can't be
pulled, and it refuses to move backwards (a lower release, or a release older
than your database schema).

On the **edge** channel (`--channel edge`, developers only) it follows `main`
and builds locally.

## Manually

```bash
git fetch origin tag v0.3.0               # the release you want
git checkout v0.3.0
# set MC_IMAGE_TAG=0.3.0 and MC_AGENT_IMAGE_TAG=0.3.0 in .env (no "v")
docker compose pull backend frontend      # or: docker compose up --build -d
docker compose up -d
```

## Migrations

Database migrations run **automatically inside the backend on start** — there
is no separate migrate step in the normal flow. To run them by hand:

```bash
make migrate    # docker compose exec backend alembic upgrade head
```

Set `MC_SKIP_MIGRATIONS=1` in `.env` if you manage migrations yourself.

Migrations are **forward-only**. There is no downgrade path, so take a
[backup](backup-restore.md) before any jump:

```bash
./backup.sh
```

## Pinning a version

`install.sh` pins every install: `MC_IMAGE_TAG` in `.env` selects the GHCR
images (`ghcr.io/argyelan-ai/mc-backend:${MC_IMAGE_TAG}` and the frontend
equivalent) and always matches the checked-out release. To go to a specific
release instead of the latest one:

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash -s -- --update --version v0.3.0
```

Do not change `MC_IMAGE_TAG` alone — images of one version on the code of
another is exactly the mismatch the installer prevents.

Read [CHANGELOG.md](../../../CHANGELOG.md) before jumping versions — breaking
changes are called out there (e.g. Caddy's switch to a `127.0.0.1` bind by
default, which made MC unreachable from other devices until
`MC_BIND_ADDRESS=0.0.0.0` is set; see [Reverse proxy &
remote access](reverse-proxy.md)).

## After updating the backend, update the host side too

If you run the Docker agent fleet, the host checkout that the CLI-Bridge uses
to build agent images must be on the same commit as the deployed backend —
otherwise a one-click CLI update silently builds with the Dockerfile's default
versions instead of the bumped `docker/cli-versions.json`. Details in
[docs/setup/first-agent.md](../../setup/first-agent.md#cli-versions--updates).
Host agents pick up new shared scripts on their next restart.
