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
| ghcr.io/argyelan-ai/mc-backend | 0.1.0 | `sha256:11f7a8af325f73752809bb84a09f3cc593fa81525e97fefc7682025b5cf68755` |
| ghcr.io/argyelan-ai/mc-frontend | 0.1.0 | `sha256:deecaea941d1da1367a826c23b8ef48608cb906f011960f8b3acf96db0a308f7` |
| postgres | 16-alpine | `sha256:e013e867e712fec275706a6c51c966f0bb0c93cfa8f51000f85a15f9865a28cb` |
| redis | 7-alpine | `sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99` |
| caddy | 2-alpine | `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |

When bumping a release: update tags AND digests in the Umbrel/Runtipi
compose files (`docker buildx imagetools inspect` or the registry API).

## Secrets in catalog deployments

Catalogs cannot run `setup.sh`, so secrets come from the store:

- **CasaOS**: required env vars the user sets at install time.
- **Umbrel**: derived deterministically in `exports.sh` via
  `derive_entropy`. The Fernet key for the secrets vault is built from the
  derived hex (43 chars + `=` padding = valid 32-byte URL-safe base64).
- **Runtipi**: `form_fields` with `type: random` — the store generates
  random strings at install time.

`SECRETS_ENCRYPTION_KEY` must be a valid Fernet key in mc-backend 0.1.0.
Since 0.1.1 (unreleased) the backend derives a proper key from any
passphrase (`backend/app/services/encryption.py`), which the CasaOS and
Runtipi flows rely on — **submit those two only after the next release**
and bump their pinned tags. The Umbrel package works with 0.1.0 as-is.

## Scope note

Catalog deployments run the core stack (boards, agents via API runtimes,
vault, sessions). Host-level fleet extras (Docker socket, host launchd
runtimes) need a manual install — the descriptions link to the Quickstart.
