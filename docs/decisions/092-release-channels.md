# ADR-092 — Release channels: stable and edge

**Status:** Proposed
**Datum:** 2026-10-17
**Scope:** Install · Infra/Release

## Kontext

The one-line installer cloned `main` but pulled `ghcr.io/argyelan-ai/mc-*:latest`.
Those images are only built for release tags, so every fresh install ran the
current compose file on the last release's images — on 2026-10-17 a fresh
install crash-looped because `mc-worker` started `python -m app.worker`, a
module the v0.2.0 image does not contain. Nothing tied the code a user got to
the images they got.

A second, separate risk: the operator's own install runs on the same kind of
machine people test the installer on. A test run of `install.sh` must not
replace or duplicate that stack.

## Entscheidung

There are exactly two release channels, and code and images always come from
the same version:

- **stable** (default) — the latest published release tag. `install.sh`
  clones that tag and pins `MC_IMAGE_TAG` / `MC_AGENT_IMAGE_TAG` to the same
  version; it pulls the prebuilt images of that version and falls back to a
  local build of the same tag. `--update` moves only forward, and never below
  the database schema.
- **edge** (`--channel edge`) — the `main` branch, always built locally
  (`MC_IMAGE_TAG=edge`, a tag no registry publishes). For developers; not
  recommended for users.

Images reach a release tag only after they are verified (both architectures,
worker module present): `release.yml` pushes `sha-<commit>` first and promotes
to `X.Y.Z` / `X.Y` / `latest` afterwards. The GitHub release is published only
after that workflow is green ([releasing](../setup/releasing.md)); the
installer additionally skips a release whose images are not there yet.

**The operator's own install is updated only via the deploy procedure, never
via `install.sh --update`.** `install.sh` refuses to start a second stack or
to recreate a stack that runs from another directory.

## Alternativen

- **Publish `:edge` images on every push to `main`** → Rejected for now: CI
  minutes per merge for two architectures, and users would still need to be
  kept off it. Edge stays a local build.
- **Keep cloning `main`, but pin images to the commit (`sha-…`)** → Rejected:
  a commit only has images if it was released, and users would run
  unreleased code.
- **Only a release-time rule ("publish after the workflow is green")** →
  Kept, but not alone: a human rule fails eventually, so the installer checks
  the registry as well.

## Konsequenzen

### Positiv
- A fresh install always runs a consistent version; the old crash class (new
  compose, old image) is gone.
- A broken image can never land under `latest`.
- Test installs cannot take over the operator's stack.

### Negativ
- New users get the last release, not `main`: fixes reach them only with the
  next release. Releases have to be cut regularly.
- Installs on a release older than this ADR carry the old `install.sh`; they
  update via `curl …/main/install.sh | bash -s -- --update` until a release
  ships the new one.

## Referenzen

- Betroffene Dateien: `install.sh`, `.github/workflows/release.yml`,
  `scripts/release-image-tags.sh`, `docs/setup/updating.md`,
  `docs/setup/releasing.md`
- Tests: `backend/tests/test_install_release_pinning.py`,
  `backend/tests/test_release_image_tags.py`
- Verwandte ADRs: ADR-043 (open-source release contract)
