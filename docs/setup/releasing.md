# Releasing

How a version reaches users. The rules behind it:
[ADR-092](../decisions/092-release-channels.md).

## Steps

1. `main` is green and `CHANGELOG.md` has the release section
   (`## [X.Y.Z] - YYYY-MM-DD`, moved out of `[Unreleased]`).
2. Tag the release commit with a **strict** tag and push it:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```
   Only `vX.Y.Z` (and pre-releases `vX.Y.Z-rc1`) start `release.yml`; a loose
   tag such as `v0.5` starts nothing.
3. **Approve the release.** **Release Images** (`.github/workflows/release.yml`)
   builds every image as `sha-<commit>` and verifies both architectures and
   the worker module. Then its *Approve and sign verified images* job waits:
   the run shows **Review deployments** — open it, tick `release`, **Approve
   and deploy**. Only after that click are the verified images signed (cosign
   keyless) and promoted to `X.Y.Z`, `X.Y` and `latest` (pre-releases:
   `X.Y.Z` only). The last step runs the update engine's own verifier
   (`scripts/mc_update_verify.py`) against `X.Y.Z` — if what CI signed and
   what installs will check ever drift apart, the run goes red here.
4. **Only when the run is green, publish the GitHub release** for the tag. The
   installer and the in-app update hint read "latest release" from GitHub — a
   release published before its images exist sends installs to a version that
   cannot be pulled. The update engine also refuses tags that are not a
   published, non-prerelease release.

If the workflow fails — or nobody approves it — nothing user-facing moved:
`latest` still points to the previous release, and nothing was signed or
promoted. Tags are protected by a repository rule and cannot be
deleted, so fix on `main` and release the **next patch version**
(`vX.Y.Z+1`); note the unreleased tag in `CHANGELOG.md`. Never publish a
GitHub release for a tag whose workflow failed.

**Dry run first:** before tagging, start `release.yml` by hand on `main`
(*Actions → Release Images → Run workflow*). It builds and verifies every
image under `sha-<commit>` only — nothing is signed or promoted — so a broken
build path shows up before a version number is spent.

## One-time setup: the release gate (repository settings)

Two settings that live on GitHub, not in the repository
([ADR-094](../decisions/094-self-update-from-settings.md) decision 5). A
repository admin sets them once; the workflow refuses to sign without the
first one.

**1. Environment `release` with a required reviewer** — the approval click.

*Settings → Environments → New environment* → name `release` →

- **Required reviewers:** add the maintainers who may approve a release.
- **Deployment branches and tags:** *Selected branches and tags* → *Add
  deployment branch or tag rule* → type **Tag**, pattern `v*`.
- Leave **Prevent self-review** off while one maintainer cuts and approves
  releases alone — with it on, nobody could approve that person's tag.

Check it (exit 0 = gated):

```bash
GH_TOKEN=$(gh auth token) GITHUB_REPOSITORY=argyelan-ai/mission-control \
  python3 scripts/release_gate_check.py
```

GitHub creates a missing environment on the fly when a workflow names it —
**without** protection. That is why the signing job starts with this same
check: a tag pushed before the environment is set up stops at "release gate
missing", nothing is signed or promoted. Set the environment up, then re-run
the failed jobs of that run.

**2. Only maintainers create version tags.** The existing tag ruleset
(*Settings → Rules → Rulesets*, target `refs/tags/v*`) already blocks
deleting, moving and force-pushing version tags. Add to the same ruleset:

- rule **Restrict creations**,
- **Bypass list:** *Repository admin* (and *Maintain*, if maintainers who are
  not admins cut releases) — *Always allow*.

Without a bypass entry nobody could create a version tag at all. With it, a
token or workflow that is not a maintainer can no longer push `v*` and start a
release; the approval click in step 1 remains the second lock.

### What the signature proves — literally

The images are signed in CI with [cosign keyless
signing](https://docs.sigstore.dev/cosign/signing/overview/): no key to
store, the job's GitHub identity is the signer and the signature is logged
publicly (Rekor). A valid signature says: *this exact image digest was built
and signed by `argyelan-ai/mission-control`'s `.github/workflows/release.yml`,
on a push of tag `vX.Y.Z`, at the tag's commit.* The update engine checks
exactly that, plus the `org.opencontainers.image.revision` label = tag commit
and an image for the machine's architecture.

It protects against a compromised registry, a moved tag and a swapped image.
It does **not** protect against a release that was approved on purpose but is
malicious or broken, nor against a compromised maintainer account.

Check a release by hand (Docker only, nothing installed):

```bash
python3 scripts/mc_update_verify.py release vX.Y.Z --repo-path .
```

### Updating cosign

The verifier runs a cosign container pinned by digest (`COSIGN_IMAGE` in
`scripts/mc_update_verify.py`); the workflow installs the same version
(`cosign-release`). Bump both in one PR — a test keeps the versions equal —
and take the new digest from `docker buildx imagetools inspect
ghcr.io/sigstore/cosign/cosign:<version>` (the index digest, so arm64 and
amd64 hosts resolve the same pin).

## Safety net in the installer

Rule 4 is the primary protection. `install.sh` adds a second one, because a
rule people must remember fails eventually: it checks the registry for the
chosen release's images (`docker manifest inspect`, no download) and, when
they are missing, installs the newest older release that has them. If the
registry cannot be reached at all, it keeps the latest release and builds it
locally — slower, still the same version for code and images.
