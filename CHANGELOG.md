# Changelog

All notable changes to Mission Control are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/) with a `0.x` "expect movement" caveat.

## [Unreleased]

### Added

- **One-click agent creation:** "New Agent" now provisions cli-bridge
  agents automatically (host-helper render → config sync → container
  start) and can bind the LLM runtime directly in the create dialog.
  If the cli-bridge helper isn't running, the UI says so with the exact
  start command — and the agent detail page shows the latest
  provisioning failure inline instead of burying it in the activity feed.
- `GET /api/v1/cli-bridge/health` — host-helper reachability for the UI.
- **Automatic daily backups** — `make backup-schedule` installs a 03:00
  run of `backup.sh` (launchd on macOS, cron on Linux); `make backup`
  runs one now. The installer recommends it after first boot.

### Changed

- **BREAKING (security): Caddy now binds to `127.0.0.1` by default** instead
  of all interfaces. Nothing is reachable from the LAN until you opt in —
  previously anyone on a shared network could reach the app and, before the
  first admin registered, even claim the admin account. If you access MC
  from other devices (e.g. phone via Tailscale), set `MC_BIND_ADDRESS=0.0.0.0`
  in `.env` and run `docker compose up -d caddy`.
- **Boot guard (security):** in production the backend now refuses to start
  with a placeholder `JWT_SECRET_KEY` (forgeable admin tokens) or an empty
  `SECRETS_ENCRYPTION_KEY`, with a clear "run ./setup.sh" message — a bare
  `docker compose up` without setup.sh used to boot silently insecure.
- Container logs are capped (10 MB × 3 files per service) — they previously
  grew unbounded until the disk filled.

### Fixed

- **`backup.sh restore` now actually restores:** it recreates the database
  (drop + create — importing into a non-empty DB errored on every table)
  and also restores the matching `mc_data_*.tar.gz` (`~/.mc`: vault key
  material, agent configs, deliverables), which was silently skipped before.
- Version consistency: the frontend claimed `2.0.0` while the product is
  `0.1.1`; CasaOS manifest now digest-pins images like Umbrel/Runtipi;
  two-component release tags (e.g. `v0.5`) are now recognized by the
  update banner.
- `AgentCreate.agent_runtime` defaulted to the retired `openclaw` runtime
  — API callers omitting the field hit a CHECK-constraint 500.

## [0.1.1] — 2026-07-03

### Added

- **App-store packages** (`deploy/catalogs/`) — prepared submissions for
  Umbrel (digest-pinned multi-arch images, `exports.sh` secret derivation)
  and Runtipi (custom-store layout), plus a Portainer template and a fixed
  CasaOS manifest. All bundle a small Caddy proxy: the prebuilt frontend
  makes same-origin API calls, so `/api/*` must be routed alongside it.
- **Makefile** — self-documenting entry points (`make help`): setup, up,
  test, build, seed, update.
- **Dev/release Docker targets** — `make build-dev` builds backend/frontend
  images with hot reload and test extras; untargeted builds stay production.
- **Vertical tutorial** — `docs/setup/build-a-vertical.md`
  (community contribution, #9).
- README: full feature list, supported-runtimes matrix, live sessions
  screenshot.

### Changed

- `SECRETS_ENCRYPTION_KEY` may now be any passphrase — the backend derives
  a proper Fernet key from non-Fernet values (app-store installs can only
  supply random strings). Existing valid keys are used unchanged.

### Fixed

- Watchdog: review-stuck escalation no longer fires for tasks on archived
  boards.
- CI: all actions SHA-pinned (org policy); leak gate runs the license-free
  gitleaks CLI, digest-pinned.

## [0.1.0] — 2026-07-02

Initial public release. 🎉

### Highlights

- **Multi-runtime agent fleet** — CLI agents in Docker (Claude Code or any
  OpenAI-compatible runtime via the omp bridge) plus optional host-side
  agents, all dispatched through a poll-based lifecycle with ACK handshake,
  review gates and a safety watchdog (silent-abort auto-block, ADR-046).
- **Boards & pipeline view** — tasks flow inbox → in_progress → review →
  done across swim lanes; phase-based orchestration with a board lead that
  plans, delegates and reviews.
- **Runtime switching** — move an agent between LLM runtimes (Anthropic ↔
  local vLLM/LM Studio/Ollama) with one PATCH; containers are recreated
  atomically with rollback (ADR-027/028).
- **Agent git workflow** — repo per project, branch per task, PR on review,
  squash-merge on approve.
- **Knowledge base & memory** — board memory, agent lessons, global
  knowledge with timeline view; optional Obsidian vault export.
- **Secrets vault** — encrypted provider keys (Fernet), agent tokens hashed
  with PBKDF2, scope-based agent permissions (16 scopes).
- **Security posture** — backend reaches Docker only through a filtering
  socket-proxy (ADR-047); lean-core default boot with `voice`/`browser`
  compose profiles; honest threat model in SECURITY.md.
- **One-line install** — `curl -fsSL .../install.sh | bash` checks
  prerequisites, pulls the prebuilt GHCR images (multi-arch, local build
  as fallback), boots, migrates and opens the browser; `install.sh
  --update` updates an existing install. CI runs the installer end-to-end
  on every push (empty DB → full migration chain → first API call).
- **First-run wizard** — registering the first admin lands in a guided
  setup: connect an LLM provider key (encrypted vault), seed a demo
  board, provision the first agent.
- **Update story** — the UI hints when a newer release exists (daily
  check, silent offline); pin versions via `MC_IMAGE_TAG`.
- **Windows (experimental)** — WSL2 path or native PowerShell
  (`setup.ps1`); see `docs/setup/windows.md`.

### Notes

- Licensed under **AGPL-3.0**: free to use, self-host and modify; modified
  versions offered as a service must be published under the same license.

- Parts of the UI and code comments are German (the project's working
  language) — see the language note in the README.
- The private development history is not part of this repository; releases
  are published as sanitized snapshots (see `docs/decisions/043`).
