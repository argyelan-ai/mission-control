# Changelog

All notable changes to Mission Control are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/) with a `0.x` "expect movement" caveat.

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
