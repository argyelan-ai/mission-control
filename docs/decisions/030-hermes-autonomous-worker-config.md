# ADR-030: Hermes Autonomous-Worker Configuration

**Status:** Accepted
**Date:** 2026-05-01
**Related:** ADR-029 (Hermes Worker Foundation), Phase 25 Plans 25-01..25-07

## Context

ADR-029 etablierte Hermes als host-side tmux Worker. Phase-25-Smoke (Task `8d5cce68`) deckte 5 Konfigurations-Lücken auf, die einen End-to-End-autonomen Worker-Lifecycle blockierten:

1. `agent.board_id = NULL` → 403 bei Board-scoped APIs (`PATCH /agent/boards/{id}/tasks/{id}`)
2. `security.allow_private_urls: false` → Tailscale-IP triggert 60s-Approval-Prompt
3. `terminal.env_passthrough: []` → MC_BASE_URL/MC_AGENT_TOKEN verschwinden in Subshells
4. `approvals.mode: manual` + `timeout: 60` → unbeaufsichtigt = auto-deny nach 60s
5. `mcp_servers.mc.command/args` falsch zusammengesetzt (`command=python3, args=[venv-python, script]`) → MCP-Server bootet nicht

Diese Lücken sind keine Phase-25-spezifischen Bugs, sondern eine Klasse: **jeder host-runtime autonomous Worker** braucht dieselben Settings.

## Decision

Definition "autonomous host worker" als Konfigurations-Bündel:

| Dimension | Setting | Wert |
|-----------|---------|------|
| Backend Provisioning | `agent.board_id` | Auto-assign zu Standard-Board falls null (`bootstrap_hermes_agent` → `_default_host_agent_board_id`) |
| Hermes Binary Launch | `--yolo` Flag | bypass dangerous-cmd approvals |
| Hermes Config | `security.allow_private_urls` | true |
| Hermes Config | `terminal.env_passthrough` | `[MC_BASE_URL, MC_AGENT_TOKEN, MC_TASK_ID, MC_BOARD_ID, MC_AGENT_ID]` |
| Hermes Config | `approvals.timeout` | 0 (wait indefinitely; entwertet durch --yolo) |
| Hermes Config | `mcp_servers.mc` | command=venv-python (absolute), args=[script-path] (single arg) |
| Worker SKILL.md | Tool-Preference | MCP-first, curl-fallback |
| Bridge Dispatcher | Prompt-Builder | exportiert MC_TASK_ID + MC_BOARD_ID; referenziert `mc_patch_task` als ACK-Mechanismus |

Single Source of Truth für Patches: `scripts/hermes-config-patch.py` (idempotent).

## Consequences

**Positive:**
- Re-smoke nach Plan 25-07 läuft ohne menschliche Eingriffe ACK→Comment→review
- Pattern wiederverwendbar für künftige host-Worker (HERM-FUTURE-01)
- Backend-Provisioning bleibt minimal — Board-Default ist DB-driven, nicht hardcoded
- MCP-first Pattern reduziert Shell-Quoting-Bugs und Approval-Overhead
- Idempotenter Patcher kann nach jedem Hermes-Update ohne Schaden ausgeführt werden

**Negative:**
- `--yolo` umgeht Hermes' eigene Sicherheitslogik. Akzeptiert: Worker hat begrenzten Scope (Workspace), Token kann revoked werden, MC ist controlled environment.
- Config-Patcher kann bei Hermes-Major-Updates brechen falls `_config_version` schema migriert. Mitigation: `pre_update_backup: true` (Hermes-eigenes Backup) + `config.yaml.bak-pre-25-07`.

**Security Threat-Refresh (relativ zu Phase 24 ADR-029):**

- **T-30-01:** `--yolo` lässt Hermes auch destruktive Commands ohne Approval ausführen. Mitigation: Hermes' Workspace ist isoliert (`~/.openclaw/agents/hermes/`); MC_AGENT_TOKEN-Scopes sind Developer-restricted (kein admin); `rm -rf` außerhalb Workspace führt höchstens zu Hermes-Crash, kein Datenverlust.
- **T-30-02:** env_passthrough enthält Token. Mitigation: Hermes' Logs scrubben Token nicht (siehe `redact_secrets: false`). Akzeptiert weil Logs nur lokal sichtbar; Phase 26 Soak könnte `redact_secrets: true` aktivieren als Härtung.
- **T-30-03:** Config-Patcher überschreibt User-Mods. Mitigation: Backup nach `config.yaml.bak-pre-25-07` vor erstem Patch; Idempotenz-Check verhindert ungewollte Diffs; Test `does_not_clobber_unrelated_keys` schützt unrelated Keys.

## Alternatives Considered

- **Per-command-allowlist statt --yolo:** würde Approval-Latenz nur für unbekannte Cmds bewahren, aber Hermes' allowlist-Mechanismus erfordert exakte command-strings (kein glob); MC_BASE_URL ändert sich pro Tailscale-Reconnect → dauerhafte Wartung. Verworfen.
- **MCP-only ohne Curl-Fallback:** minimalistischer, aber Phase 26 Soak könnte mc-mcp.py-Crash erleben → Curl-Fallback bleibt Sicherheitsnetz.
- **Backend pusht via Bridge HTTP statt Bridge pollt MC:** spiegelverkehrt aber komplexer (Backend braucht Reverse-Tunnel zu Host). Plan 25-06 hat Polling gewählt. ADR-030 bestätigt.
- **Hardcoded board_id im bootstrap:** schneller aber bricht bei Board-Renames/Migrations. Resolve-by-name ist robuster und stirbt loud (warn-log) wenn Board fehlt.
