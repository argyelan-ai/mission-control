# Updating

Mission Control never updates itself. The UI hints under **Settings → Über**
when a newer release exists (checked once a day against GitHub Releases,
silent when offline) — applying it is your call.

Full reference: [docs/setup/updating.md](../../setup/updating.md).

## The short way

```bash
cd <your-mission-control-directory>
./install.sh --update        # or: make update
```

`--update` must run inside an installed checkout (it checks for
`docker-compose.yml` + `.env`). It runs `git pull --ff-only`, tries
`docker compose pull backend frontend`, and starts the stack — falling back to
a local `docker compose up --build -d` when the prebuilt GHCR images can't be
pulled.

## Manually

```bash
git pull
docker compose pull backend frontend   # or: docker compose up --build -d
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

`MC_IMAGE_TAG` in `.env` pins the GHCR images
(`ghcr.io/argyelan-ai/mc-backend:${MC_IMAGE_TAG:-latest}` and the frontend
equivalent) instead of following `latest`:

```bash
MC_IMAGE_TAG=0.1.1
```

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
