# Architecture Decision Records (ADRs)

> **Zweck:** Entscheidungshistorie der wichtigsten Architektur- und Design-Entscheidungen in Mission Control. Jedes ADR beantwortet: **Was wurde entschieden? Warum? Welche Alternativen gab es? Was sind die Konsequenzen?**
>
> **Lebende Dokumentation.** Bei jeder signifikanten Architektur-Änderung neues ADR anlegen. Bestehende ADRs nicht umschreiben — stattdessen neues ADR das das alte "supersedes".

## Format

Jedes ADR hat folgende Sektionen:
1. **Status** — Accepted | Deprecated | Superseded by ADR-XXX
2. **Kontext** — Was war das Problem?
3. **Entscheidung** — Was wurde beschlossen?
4. **Alternativen** — Was wurde abgelehnt und warum?
5. **Konsequenzen** — Positive + negative Folgen
6. **Referenzen** — Betroffene Dateien, Commits, verwandte ADRs

## Index

| # | Titel | Status | Datum | Scope |
|---|---|---|---|---|
| [001](001-dispatch-ack-handshake.md) | Dispatch ACK Handshake | Accepted | 2026-02 | Backend/Dispatch |
| [002](002-subagent-dispatch.md) | Subagent Dispatch mit Kill-Switch | Accepted | 2026-03 | Backend/Dispatch |
| [003](003-triple-runtime-architecture.md) | Triple-Runtime-Architektur (openclaw + cli-bridge + Docker V2) | Accepted | 2026-04-07 | Infra/Runtime |
| [004](004-board-memory-unified.md) | BoardMemory als Single Knowledge-Table | Accepted | 2025-XX | Backend/DB |
| [005](005-board-lead-first-dispatch.md) | Board-Lead-First Dispatch (Henry orchestriert alles) | Accepted | 2026-02 | Backend/Dispatch |
| [006](006-jinja2-template-source-of-truth.md) | Jinja2-Template als Single Source of Truth | Accepted | 2025-XX | Backend/Provisioning |
| [007](007-structured-dispatch-messages.md) | Structured Dispatch Messages mit Curl-Callbacks | Accepted | 2026-02 | Backend/Dispatch |
| [008](008-phase-completion-watchdog.md) | Phase-Completion via Watchdog | Accepted | 2026-02 | Backend/Watchdog |
| [009](009-agent-scoped-router-separat.md) | Agent-Scoped Router separat von User-Router | Accepted | 2025-XX | Backend/Auth |
| [010](010-redis-cache-pbkdf2.md) | Redis-Cache für PBKDF2-Token | Accepted | 2025-XX | Backend/Auth |
| [011](011-http-polling-docker-agents.md) | HTTP-Polling für Docker-Agents | Accepted | 2026-04-07 | Infra/Dispatch |
| [012](012-use-alter-foreign-keys.md) | use_alter=True für Zyklus-ForeignKeys | Accepted | 2025-XX | Backend/DB |
| [013](013-docker-v2-live-deployment.md) | MC V2 Docker-Agents Live-Deployment + 8 Fix-Lessons | Accepted | 2026-04-08 | Infra/Runtime |
| [014](014-boss-host-runtime.md) | Boss runs as macOS host process (claude binary, OAuth) | Accepted | 2026-04-17 | Infra/Runtime |
| [015](015-install-approval-flow.md) | Install-Approval Flow für Boss | Accepted | 2026-04-18 | Backend/Auth |
| [016](016-mcp-registry.md) | MCP-Registry + Sync | Accepted | 2026-04-18 | Backend/MCP |
| [017](017-runtime-registry-db.md) | Runtime Registry in DB (JSON als Seed) | Accepted | 2026-04-19 | Backend/DB |
| [018](018-runtime-switch-via-restart.md) | Runtime-Wechsel via Container-Restart (kein Hot-Reload) | Accepted | 2026-04-19 | Infra/Runtime |
| [019](019-claude-fleet-hybrid.md) | Claude Fleet (Hybrid — 9 Docker-Agents auf claude-code, Sparky + Boss unverändert) | Accepted | 2026-04-20 | Infra/Runtime · LLM Auth |
| [020](020-harness-phase2-mc-cli.md) | Harness Phase 2: `mc` CLI + Dispatch Split + Progress SSoT | Accepted | 2026-04-20 | Backend/Dispatch · Agent Protocol |
| [021](021-agent-personas.md) | Agent Personas: Grounded Identities + Shared Reflection Charter | Accepted | 2026-04-20 | Agent Protocol · Template System |
| [022](022-mc-home-workspace-layout.md) | `~/.mc/` Home + Standardized Workspace Layout | Accepted | 2026-04-21 | Infra/Runtime · Backend/Provisioning · Agent Protocol |
| [023](023-review-policy-trust-by-default.md) | Review-Policy: Trust-by-Default + Reflection-Decoupling | Accepted | 2026-04-20 | Backend/Agent-Protocol · SOUL.md · Board-Config |
| [024](024-claude-process-recycling.md) | Claude-Process Recycling im Docker-Agent-Container | Accepted | 2026-04-26 | Infra/Runtime · Backend/Provisioning · Container Lifecycle |
| [025](025-dispatch-agent-scoped-split.md) | Dispatch & Agent-Scoped Split (Phase 4) | Accepted | 2026-04-26 | Backend/Dispatch · Backend/Routing |
| [026](026-context-management-auto-recovery.md) | Context Management & Auto-Recovery (CTX + REC merger) | Draft | 2026-04-27 | Backend/Watchdog · Backend/Task-Runner · Infra/Heartbeat |
| [027](027-universal-agent-runtime-binding.md) | Universal Agent ↔ Runtime Binding (atomic switch + image-aware lifecycle) | Accepted | 2026-04-28 | Backend/Runtime · Backend/DB · Frontend/Runtimes |
| [028](028-runtime-registry-and-session-propagation.md) | Runtime Registry Konsolidierung + Session-Env-Propagation (DB-only, respawn-window, build_runtime_env) | Accepted | 2026-04-29 | Backend/Runtime · Backend/DB · Frontend/Runtimes |
| [029](029-hermes-host-side-tmux-worker.md) | ADR-029 — Hermes als host-side tmux Worker mit eigener Bridge, vLLM Reuse, single-instance non-switchable | Accepted | 2026-04-30 | Infra/Runtime · Backend/DB · Backend/Provisioning · Frontend/Runtimes |
| [030](030-hermes-autonomous-worker-config.md) | Hermes Autonomous-Worker Configuration (board_id auto-assign, --yolo, env_passthrough, MCP-first) | Accepted | 2026-05-01 | Infra/Runtime · Backend/Provisioning · Agent Protocol |
| [031](031-hermes-hardening-poll-claim-and-host-path-and-idle-timeout.md) | Hermes Hardening: poll-claim semantic + per-agent idle timeout + deliverable dual-path | Accepted | 2026-05-01 | Backend/Dispatch · Backend/Watchdog · Backend/Agent-Scoped · Infra/Host-Worker |
| [032](032-content-page-refactor.md) | Content Page Refactor: Von 4 Tabs zu 2 Top-Level Pages | Accepted | 2026-05-10 | Frontend/Pages · UX/Navigation |
| [033](033-secrets-vs-credentials-boundary.md) | Secrets vs Credentials: Boundary kodifizieren statt unifizieren | Accepted | 2026-05-14 | Backend/DB · Backend/Auth · Agent Protocol · UX/Settings |
| [034](034-vault-as-source-of-truth.md) | Vault as Source of Truth (Karpathy-Wiki Memory) | Proposed | 2026-05-14 | Backend/Memory · Backend/Services · Infra/Storage · Agent Protocol |
| [035](035-dispatch-attempt-id-audit-trail.md) | `dispatch_attempt_id` Audit Trail + Race-Safe Initialisation | Accepted | 2026-05-15 | Backend/Dispatch |
| [036](036-runtime-launch-command.md) | Runtime `launch_command` für recipe-launched Container | Accepted | 2026-05-15 | Backend/Runtime · Infra/Runtime |
| [037](037-mc-finish-preflight-pattern.md) | `mc finish` Preflight + Idempotency Pattern | Accepted | 2026-05-16 | Agent CLI · Frontend/Agent-Workflow |
| [038](038-rename-voice-agent-to-jarvis.md) | Voice-Agent → Jarvis Rename (Persona/Infra Boundary) | Accepted | 2026-05-16 | Backend/DB · voice-worker · Agent-Identity |
| [039](039-openclaw-gateway-sunset.md) | OpenClaw Gateway Sunset (RPC entfernt, runtime-aware Dispatch) | Accepted | 2026-05-17 | Infra/Runtime · Backend/Dispatch · Backend/DB · Frontend/State |
| [040](040-portable-file-access.md) | Portable File Access (HTTP streaming primär, native open optional, fs_roots/fs_service + file_index) | Accepted | 2026-06-18 | Backend/Files · Backend/DB · Frontend/Pages · Infra/Reusability |
| [041](041-compose-renderer-emits-new-agent-services.md) | Compose-Renderer emittiert Service-Blöcke für neue cli-bridge-Agenten | Accepted | 2026-06 | Infra/Runtime · Backend/Provisioning |
| [042](042-unsloth-porsche-power-managed-runtime.md) | unsloth_porsche — power-managed Runtime (PORSCHE) + Wake-on-LAN + Runtime-Readiness Dispatch-Gate | Accepted | 2026-06-24 | Infra/Runtime · Backend/Runtime · Backend/Dispatch · Backend/DB |
| [043](043-open-source-release-contract.md) | Open-Source-Release-Contract (Fresh-History-Release, Env-Identitätsvertrag, Zero-Grep-Gate) | Accepted | 2026-07-02 | Infra/Release · Backend/Config · Docs |
| [044](044-vertical-modules.md) | Vertical-Module (strippbare Feature-Bundles, `app/verticals/` + Hook-Registry) | Accepted | 2026-07-02 | Backend/Architecture · Frontend/Architecture · Infra/Release |
| [045](045-omp-runtime.md) | `omp` Runtime-Typ — Clean-Stream Headless Agent (omp + Qwen, `mc-omp-agent`, `bridge.py --serve`) | Proposed | 2026-07-01 | Infra/Runtime · Backend/Runtime · Backend/Provisioning |
| [046](046-lifecycle-safety-watchdog.md) | Lifecycle Safety Watchdog (Silent-Abort Auto-Block, cli-bridge v1) | Accepted | 2026-07-01 | Backend/Task-Runner · Backend/Watchdog |
| [047](047-docker-socket-proxy.md) | Docker-Socket-Zugriff nur über filternden Proxy (tecnativa socket-proxy, DOCKER_HOST) | Accepted | 2026-07-02 | Infra/Compose · Backend/Runtime-Switch |
| [048](048-host-registry.md) | Host-Registry — generische Multi-Host Control-Plane statt neuer runtime_type pro Box | Accepted | 2026-07-02 | Backend/Runtime · Backend/DB · Frontend/Runtimes · Infra/Runtime |
| [049](049-omp-native-tui-session.md) | omp Native-TUI Session — echte scrollbare omp-CLI auf der Sessions-Seite (Hook-Completion + `@file`-Inject + SIGKILL-Watchdog + Per-Task-Isolation), ersetzt das ADR-045-Headless-Modell | Proposed | 2026-07-04 | Infra/Runtime · Backend/Runtime |
| [050](050-repos-registry.md) | Repos Registry — first-class Repo-Modell (`repos` + `projects.repo_id`), per-Repo-Arbeitsregeln in der Dispatch-Directive, `/repos`-Verwaltungsseite, Legacy-Sync-Kontrakt | Accepted | 2026-07-04 | Backend/DB · Backend/Dispatch · Frontend/Pages |
| [051](051-loops.md) | Loops — ergebnisgesteuerte Task-Schleifen als Meta-Controller über normale Tasks (`loops`+`loop_rounds`, Loop-Runner, `loop_gate`-Approval, `/loops`; L1 mit Runden-/Zeitbudget, Circuit-Breaker 2 Fehlrunden, 1 Loop/Board) | Accepted | 2026-07-04 | Backend/DB · Backend/Services · Backend/Approvals · Frontend/Pages |
| [052](052-task-repo-select.md) | Einheitliche Repo-Auswahl in der Task-Maske — `tasks.repo_id` (Registry-Repo, Vorrang Task > Projekt > mc-workspace), `POST /repos/new`, `use_separate_repo` deprecated (registriert jetzt mit), Regeln-Badge via git-info | Accepted | 2026-07-04 | Backend/DB · Backend/Dispatch · Frontend/Task-Maske |
| [053](053-reference-files.md) | Referenz-Dateien für Tasks & Projekte — `reference_files` + Files-Root `references`, Upload-API mit Vererbung (Projekt→Task), absolute `~/.mc`-Pfade in der Dispatch-Directive (Agenten lesen direkt vom Mount), Kaskaden in allen Delete-Pfaden | Accepted | 2026-07-04 | Backend/DB · Backend/Files · Backend/Dispatch · Frontend/Task-Maske |
| [054](054-runtime-watcher.md) | Runtime Watcher — periodic model-drift probing (90s, two-probe confirmation), auto-propagation to bound agents via `docker restart` (not `respawn_window_only`), busy agents synced on next watcher tick, circuit breaker after 3 failed syncs; supersedes D-22 | Accepted | 2026-07-05 | Backend/Runtime · Backend/DB · Backend/Services · Frontend/Runtimes |
| [055](055-github-connection-config.md) | GitHub-Verbindung als First-Class-Anschluss — zentraler Resolver `github_config` (Vault `github_owner`/`github_token` > Env, 30s-TTL + Invalidierung), Auth folgt Token-Rotation live, Status-/Config-API (`github-status?probe`, `github-config`), Settings-GitHub-Sektion + Wizard-Step + install.sh-Abfrage + /repos-Onboarding | Accepted | 2026-07-05 | Backend/Services · Backend/API · Frontend/Settings · Install |
| [056](056-harness-provider-decoupling.md) | Harness/Provider-Decoupling — `agents.harness` + zentrale Compat-Matrix (`harness_compat.py`), Image folgt Harness statt Runtime, zweite Switch-Achse (`new_harness`), 3-stufige Provider-Key-Auflösung (`agent.secret_id` > `runtime.api_key_secret_id` > global), `GET /runtimes/compat-matrix`; v1-Matrix claude→anthropic, openclaude/omp→openai, claude×OpenAI + omp×Anthropic sind v2 (LiteLLM-Proxy) | Accepted | 2026-07-05 | Backend/Runtime · Backend/DB · Backend/Auth · Frontend/Runtimes |
| [057](057-engine-control-v0-autostart-flag.md) | Engine Control v0: Autostart-Flag via SSH — `runtimes.autostart_supported`/`autostart_flag_path`, `services/runtime_autostart.py` (touch/rm + Rücklese-Verifikation über den bestehenden `_ssh_run`/Host-Registry-Pfad, ADR-048), on-demand GET/POST `/runtimes/db/{slug}/autostart`, `AutostartToggle` auf `/runtimes` (an/aus/unbekannt, kein optimistisches UI); erster Baustein von Cockpit v2 | Accepted | 2026-07-05 | Backend/Runtime · Backend/DB · Frontend/Runtimes · Infra/Runtime |
| [058](058-cli-tool-updates.md) | CLI-Tool-Updates aus User-Sicht — `docker/cli-versions.json`-Manifest als Single Source of Truth, Build läuft auf dem Host über die CLI-Bridge (`POST /agent-images/build`, Docker-Socket-Proxy-Regel `BUILD: 0`), periodischer Update-Check (npm/GitHub, 6h) ohne Auto-Update, Update-Orchestrierung mit Manifest-Rollback bei Build-Fehlschlag + Rolling Recreate (`agents.pending_recreate`, Migration `0147`, ADR-054-Propagationsmechanik wiederverwendet), neue Sektion "CLI-Tools" auf `/runtimes`; GHCR-Publish + Auto-Update-Policy sind v2 | Accepted | 2026-07-05 | Infra/Runtime · Backend/DB · Backend/Services · Frontend/Runtimes |

## Neue ADRs schreiben

1. Nächste Nummer wählen (chronologisch, keine Lücken)
2. Aus `_template.md` kopieren (oder ein bestehendes ADR)
3. In dieser README zur Tabelle hinzufügen
4. Wenn ein altes ADR ersetzt wird: dessen Status auf `Superseded by ADR-XXX` setzen
5. Im Commit: `docs(adr): add ADR-XXX — {kurzer Titel}`
- [ADR-041](041-compose-renderer-emits-new-agent-services.md) — Compose-Renderer emittiert Service-Blöcke für neue cli-bridge-Agenten
- [ADR-045](045-omp-runtime.md) — `omp` Runtime-Typ — Clean-Stream Headless Agent (omp + Qwen, `mc-omp-agent` Image, `bridge.py --serve`)
- [ADR-049](049-omp-native-tui-session.md) — omp Native-TUI Session (echte scrollbare omp-CLI auf Sessions, turn-end-Hook + `@file`-Inject + Watchdog; ersetzt das ADR-045-Headless-Drive-Modell)
