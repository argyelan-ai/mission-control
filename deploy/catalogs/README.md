# App-Store / Catalog Manifests

Prepared manifests to get MC into self-hosting catalogs — **distribution**:
this is where homelab users look for apps.

> **Shared topology rule:** the browser must enter through the bundled
> Caddy `proxy` service (`/api/*` + `/health` → backend, rest → frontend).
> The prebuilt frontend image makes relative (same-origin) API calls —
> exposing it directly yields a dead UI. Verified end-to-end locally
> (full catalog stack on a scratch port: health, API through proxy,
> first-run wizard).

| Catalog | Files | Submission path |
|---|---|---|
| **Portainer** | `portainer-template.json` | No central store: users add this file's raw URL as an App Template source (Settings → App Templates). Promote the URL in the README. |
| **CasaOS** | `casaos-app.yml` | PR to [IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore) (`Apps/mission-control/` with docker-compose.yml + icons). |
| **Umbrel** | `umbrel/mission-control/` + `umbrel/icon.svg` | PR to [getumbrel/umbrel-apps](https://github.com/getumbrel/umbrel-apps): commit only the app folder; icon (256×256 SVG, square corners) + 3–5 raw screenshots go in the PR description — the Umbrel team produces the final gallery assets. Their guidance lives in the repo's `.claude/skills/umbrel-*` files. Expect review iteration (naming, restart policies, digests). |
| **Runtipi** | `runtipi/apps/mission-control/` | ⚠️ The official [runtipi-appstore](https://github.com/runtipi/runtipi-appstore) **permanently stopped accepting new apps** (README + maintainer statement, runtipi/runtipi#2317) — new-app PRs are auto-closed. Options: publish our folder as a **custom app store** repo (users add its URL under Settings → App Stores), or ask a maintainer-endorsed community store (steveiliop56, Lancelot-Enguerrand, JigSawFr) to adopt it. |

## Image pinning

Umbrel requires `tag@sha256:` pins; Runtipi discourages `latest`. Current
pins (all multi-arch amd64+arm64, digests read from the registry APIs):

| Image | Tag | Digest |
|---|---|---|
| ghcr.io/argyelan-ai/mc-backend | 0.3.1 | `sha256:199aaf4d668ff6833d2fc9957c6b30f11c5f3c573471653de86d84a4724e0c72` |
| ghcr.io/argyelan-ai/mc-frontend | 0.3.1 | `sha256:0ecb3f68ac53d70b943dc489cf0e9f387f6610bf1082ee9680f82b3b8949c7de` |
| postgres | 16-alpine | `sha256:e013e867e712fec275706a6c51c966f0bb0c93cfa8f51000f85a15f9865a28cb` |
| redis | 7-alpine | `sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99` |
| caddy | 2-alpine | `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |

The backend/frontend digests above are the multi-arch index digest
(`docker buildx imagetools inspect ghcr.io/argyelan-ai/mc-backend:0.3.1
--format '{{json .Manifest}}'` → `.digest`), each verified to contain both
`amd64` and `linux/arm64` manifests.

When bumping a release: update tags AND digests in the CasaOS/Umbrel/Runtipi
manifests (`docker buildx imagetools inspect` or the registry API), and
re-check the compatibility note below against the new release's
`docker-compose.yml` / `backend/app/config.py` diff — a required env var or
service added upstream has to land in the catalog files too, or the
container crashes on boot in a store install (no `setup.sh` to catch it).

## Compatibility check — catalog composes vs. the installable release (0.3.1)

Catalog stores cannot run `setup.sh` or `install.sh`, so every env var the
backend requires at boot has to be wired into the store manifest directly.
Checked against `git diff v0.2.0 v0.3.1 -- docker-compose.yml backend/app/config.py setup.sh .env.example`:

- **`INTERNAL_BOOTSTRAP_SECRET` is now a hard boot requirement.** Added in
  PR #404 (`backend/app/config.py:295`); `validate_boot_secrets()`
  (`backend/app/config.py:895-932`) raises `RuntimeError` and the backend
  refuses to start whenever `ENVIRONMENT=production` and this value is
  empty — and all three catalog manifests already set
  `ENVIRONMENT: production`. Without a fix, every catalog install of
  0.3.1 would crash-loop the backend container. **Fixed in this bump:**
  the var is now wired through as a required secret, same pattern as
  `SECRETS_ENCRYPTION_KEY` — CasaOS: user-set env (`casaos-app.yml`);
  Umbrel: derived in `exports.sh` via `derive_entropy` (no Fernet encoding
  needed — it is a plain bearer token, not an encryption key, see
  `backend/app/config.py:287-295`); Runtipi: a new `type: random`
  `form_fields` entry (`config.json`) mapped to `INTERNAL_BOOTSTRAP_SECRET`
  (`docker-compose.json`).
- **The new `mc-worker` container is optional for catalog installs.**
  0.3.1 splits background services (scheduler, watchdog, Telegram/Slack
  inbound, vault compactor) into a second container with
  `ENABLE_BACKGROUND_SERVICES=true`, while the API container gets
  `ENABLE_BACKGROUND_SERVICES=false` explicitly (`docker-compose.yml`
  diff, "Architektur E Teil 2"). The catalog composes set neither var on
  the single backend service they run, and the setting's default is
  `true` (`backend/app/config.py:643`, kept specifically "as a fallback
  for compose-free installs" per the inline comment) — so the one backend
  container in a catalog install keeps doing both jobs, same as before
  the split. No `mc-worker` service was added here; if a future release
  changes that default or makes the split mandatory, this note needs a
  real second service.
- **`PUID`/`PGID` (Linux host-uid alignment, `.env.example` / `setup.sh`
  diff) are not required.** `backend/docker-entrypoint.sh` only remaps
  `mcuser`'s uid/gid when `PUID`/`PGID` are set; left unset it keeps the
  image default (`mcuser` = uid/gid 1000), which is what all three catalog
  manifests already do implicitly (no `user:` override, no PUID/PGID env).
  This only becomes a problem if the NAS/host runs Docker with a non-1000
  data-owning user for the bind-mounted volumes — out of scope for a
  version bump, left as-is.
- **`stop_grace_period` / `init: true` (graceful-shutdown hardening added
  to the main `docker-compose.yml`)** are reliability improvements, not
  boot requirements — not ported here to keep this PR to the version bump
  plus the one breaking requirement above.

## Secrets in catalog deployments

Catalogs cannot run `setup.sh`, so secrets come from the store:

- **CasaOS**: required env vars the user sets at install time.
- **Umbrel**: derived deterministically in `exports.sh` via
  `derive_entropy`. The Fernet key for the secrets vault is built from the
  derived hex (43 chars + `=` padding = valid 32-byte URL-safe base64).
- **Runtipi**: `form_fields` with `type: random` — the store generates
  random strings at install time.

Since mc-backend 0.2.0 the backend derives a proper Fernet key from any
passphrase (`backend/app/services/encryption.py`) — the CasaOS and
Runtipi flows rely on this (0.1.0 required a Fernet-formatted value).

Since mc-backend 0.3.1, `INTERNAL_BOOTSTRAP_SECRET` is a fourth required
secret (see the compatibility note above) — any stable string works, no
Fernet encoding needed; it is wired through the same per-store pattern as
the other three.
The Umbrel package derives a valid Fernet key in `exports.sh` either way.

The Runtipi package is published as a custom app store:
**github.com/argyelan-ai/tipi-store** (synced from this directory).

## Scope note

Catalog deployments run the core stack (boards, agents via API runtimes,
vault, sessions). Host-level fleet extras (Docker socket, host launchd
runtimes) need a manual install — the descriptions link to the Quickstart.
