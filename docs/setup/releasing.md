# Releasing

How a version reaches users. The rules behind it:
[ADR-092](../decisions/092-release-channels.md).

## Steps

1. `main` is green and `CHANGELOG.md` has the release section
   (`## [X.Y.Z] - YYYY-MM-DD`, moved out of `[Unreleased]`).
2. Tag the release commit with a **strict** tag and push it:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git -C . push origin vX.Y.Z  # guard-allow: push-gate (a tag on a main commit that CI already scanned)
   ```
   Only `vX.Y.Z` (and pre-releases `vX.Y.Z-rc1`) start `release.yml`; a loose
   tag such as `v0.5` starts nothing.
3. Wait for **Release Images** (`.github/workflows/release.yml`) to be green.
   It builds every image as `sha-<commit>`, verifies both architectures and
   the worker module, and only then promotes the verified manifests to
   `X.Y.Z`, `X.Y` and `latest` (pre-releases: `X.Y.Z` only).
4. **Only then publish the GitHub release** for the tag. The installer and
   the in-app update hint read "latest release" from GitHub — a release
   published before its images exist sends installs to a version that cannot
   be pulled.

If the workflow fails, nothing user-facing moved: `latest` still points to
the previous release. Tags are protected by a repository rule and cannot be
deleted, so fix on `main` and release the **next patch version**
(`vX.Y.Z+1`); note the unreleased tag in `CHANGELOG.md`. Never publish a
GitHub release for a tag whose workflow failed.

**Dry run first:** before tagging, start `release.yml` by hand on `main`
(*Actions → Release Images → Run workflow*). It builds and verifies every
image under `sha-<commit>` only — nothing is promoted — so a broken build
path shows up before a version number is spent.

## Safety net in the installer

Rule 4 is the primary protection. `install.sh` adds a second one, because a
rule people must remember fails eventually: it checks the registry for the
chosen release's images (`docker manifest inspect`, no download) and, when
they are missing, installs the newest older release that has them. If the
registry cannot be reached at all, it keeps the latest release and builds it
locally — slower, still the same version for code and images.
