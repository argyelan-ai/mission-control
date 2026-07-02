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

## Manually

```bash
git pull
docker compose pull backend frontend   # or: docker compose up --build -d
docker compose up -d                   # migrations run automatically on backend start
```

## Pinning a version

Set `MC_IMAGE_TAG` in `.env` (e.g. `MC_IMAGE_TAG=0.1.0`) to pin the GHCR
images instead of following `latest`. Check the
[CHANGELOG](../../CHANGELOG.md) before jumping versions.

## Before big jumps

- Migrations are forward-only — take a database backup first (`./backup.sh`).
- Read the release notes; breaking changes are called out there.
