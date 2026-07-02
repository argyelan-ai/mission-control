# ADR-019 — Claude Fleet (Hybrid)

**Status:** Accepted
**Datum:** 2026-04-20
**Scope:** Infra/Runtime · LLM Auth
**Superseded:** —
**Supersedes partially:** ADR-003 (Triple-Runtime-Architektur) — aktualisiert die Runtime-Zuordnung nach Binary und Modell

## Kontext

Nach PR #29-#41 (umfassendes Workspace-Hardening) war der nächste logische Schritt die Umstellung von 10 Docker-Container-Agents und Boss (Host) von der openclaude-Welt (Ollama Cloud + OpenAI-Shim) auf das offizielle Anthropic Claude Code CLI mit der Max-Subscription des Operators als Auth-Basis.

Zwei Designs standen zur Wahl:

- **Option A: Host-tmux für alle (wie Boss)** — 10 launchd-Services pro Agent (ttyd + pty-bridge + entrypoint), neue Port-Registry (7682+), neue Routing-Tabelle im Backend-Terminal-Proxy, eigenhändig generierte plist-Files.
- **Option B: Docker-Container bleiben, nur Binary + Auth wechseln** — bestehende Infra (`mc-agent-base`, `poll.sh`, `entrypoint.sh`, Sessions-UI, pre-push-hook) weiter nutzen, neues Image mit `claude` statt `openclaude` + `CLAUDE_CODE_OAUTH_TOKEN` env.

Ausschlaggebende Recherche-Erkenntnisse (3 parallele Explore-Agents):

1. **Host-Runtime skaliert nicht trivial**: `cli_terminal.py:763` hardcoded `ws://host.docker.internal:7682/` → nur Boss erreichbar via Sessions-Page. 9 weitere Agents hätten Port-Management + Routing-Tabelle + 27 plist-Files gebraucht (Estimate: 12h initial + 30min pro Agent).
2. **OAuth-File-Mount bricht bekanntermassen** (Anthropic Issue #22066): Bearer-Token-Fehler nach Container-Restart. Aber: env-based `CLAUDE_CODE_OAUTH_TOKEN` (via `claude setup-token`, 1-Jahres-Token) ist stabil — genau dafür gemacht.
3. **Max-Subscription deckt 9 parallele Sessions** (Max 20x) — Rate-Limit kein Blocker mehr.
4. **Frontend ist runtime-agnostisch**: Sessions-Page, SkillMatrix, PluginMatrix nutzen `agent.agent_runtime === 'cli-bridge'` weiter; solange das Feld erhalten bleibt, sind keine Frontend-Änderungen zwingend.

## Entscheidung

**Option B — Hybrid-Fleet.** Docker-Container bleiben, Binary + Auth wechseln für 9 Agents:

| Agent | Runtime | Binary | Modell | Auth |
|---|---|---|---|---|
| Boss | `host` (launchd) | `~/.local/bin/claude` | claude-opus-4-7 | macOS Keychain OAuth |
| Rex, Davinci, Shakespeare, FreeCode, Neo, Tester, Deployer, Researcher, Planner | `cli-bridge` (Docker) | `claude` (native installer in `mc-claude-agent:latest`) | claude-sonnet-4-6 | `CLAUDE_CODE_OAUTH_TOKEN` env (shared 1-Jahres-Token, aus Vault via Bootstrap) |
| Sparky | `cli-bridge` (Docker) | `openclaude` (fixierte Version 0.1.8 für Security, in `mc-agent-base:latest`) | qwen3-coder-next | OpenAI-Shim → LM Studio / Ollama Cloud |
| Henry | `openclaw` (Gateway) | — | glm-5.1 (Gateway-managed) | openclaw-Gateway-Auth |

## Implementation

### Neues Docker-Image `mc-claude-agent`
- Base: `node:22.11.0-bookworm-slim` (claude-native-binary braucht glibc, nicht musl; Node für Plugin-Subprozesse)
- Install: `curl -fsSL https://claude.ai/install.sh | bash` → `~/.local/bin/claude` (native Binary, 80MB)
- Kein `CLAUDE_CODE_USE_OPENAI`, kein `OPENAI_BASE_URL`, kein `@ollama/openclaw-web-search`
- `entrypoint.sh` lädt `CLAUDE_CODE_OAUTH_TOKEN` aus Bootstrap-Response, fail-loud wenn fehlt
- `start-claude.sh` ruft `claude --dangerously-skip-permissions --append-system-prompt "$(cat SOUL.md)"`
- `poll.sh` unverändert (runtime-agnostisch, `/clear` funktioniert in claude-code gleich)

### Bootstrap-Endpoint (`/api/v1/internal/bootstrap`)
- Für Agent mit `runtime.slug` beginnend mit `anthropic-claude-` → liefert `CLAUDE_CODE_OAUTH_TOKEN` aus Vault (Key: `claude_code_oauth_token`), KEIN `OPENAI_*`
- Für andere cli-bridge Runtimes (ollama-cloud, qwen-coder-lms) → liefert weiter `OPENAI_BASE_URL` + `OPENAI_MODEL` + `OPENAI_API_KEY`

### DB-Migrationen
- `0080` — Seed `anthropic-claude-opus` + `anthropic-claude-sonnet` Runtime-Rows
- `0081` — Bind 9 Agents + Boss an neue Runtimes (setzt `runtime_id` + `model` konsistent, lässt `agent_runtime` unverändert)

### docker-compose.agents.yml Split
- 2 Anchors: `&claude-agent-base` (neues Image) und `&openclaude-agent-base` (für Sparky)
- 9 Services mit `<<: *claude-agent-base`, Sparky mit `<<: *openclaude-agent-base`

## Alternativen (und warum verworfen)

- **Host-tmux für alle** (ursprünglicher Plan 2026-04-20): zu aufwendig; Boss ist der einzige Agent der von Host-Pattern profitiert (OAuth im Keychain), die 9 Worker können genauso env-based OAuth nutzen.
- **Claude-native Binary auf Alpine**: Alpine hat musl-libc, Anthropic-Installer produziert glibc-Binary. `node:22-bookworm-slim` als Ersatz akzeptiert.
- **Shared OAuth-Token über File-Mount**: bekannter Bug (#22066) — env-Token ist der Anthropic-empfohlene Weg.
- **Per-Agent individuelle OAuth-Tokens**: Max-Subscription ist Account-weit, macht keinen Sinn 10 Tokens zu verwalten. Zukünftig möglich wenn Rate-Limits granularer werden.
- **Sparky auch auf Claude umstellen**: der Operator will lokale Workforce (qwen) für Cost-Savings + bei Anthropic-Outage behalten.

## Konsequenzen

### Positiv
- **Zero Host-Infrastructure-Change**: keine neuen launchd-Services, keine Port-Registry, keine Backend-Terminal-Routing-Tabelle
- **Sessions-UX unverändert**: Scrollen, Markieren, Kopieren, Eingeben funktioniert identisch wie bei openclaude (xterm.js + tmux sind runtime-agnostisch)
- **SkillMatrix + PluginMatrix**: kein Filter-Fix nötig (agent_runtime bleibt `cli-bridge`)
- **Image-Separation für Security**: openclaude 0.1.8 bleibt fixiert im `mc-agent-base`, Claude-Fleet hat ein sauberes Image ohne npm-Dependencies
- **Rate-Limit-Fairness**: Max 20x Pooling ist designed für ~2-3 parallele Opus-Sessions; bei 9 Sonnet-Agents + 1 Boss-Opus steht das deutlich über dem effektiven Limit

### Negativ / Risiken
- **OAuth-Token ist shared**: wenn der Token kompromittiert ist, sind alle Container gleichzeitig betroffen. Mitigation: Token ist 1 Jahr gültig, Rotation via `claude setup-token` + sync-config möglich.
- **Anthropic API als SPOF**: bei Anthropic-Outage sind 9 Agents down. Mitigation: Sparky (lokal) bleibt als Fallback-Worker verfügbar.
- **Container-Memory-Leak** (bekannt, siehe project_container_memory_leak.md) bleibt bestehen — orthogonal zur Binary-Auswahl.
- **`CLAUDE_CODE_OAUTH_TOKEN` ist nicht im Repo**: muss manuell in `.env` + Vault eingetragen werden nach `claude setup-token`. Cutover-Memo in `docs/plans/2026-04-20-claude-fleet-cutover.md`.

## Cutover-Prozedur

Siehe `docs/plans/2026-04-20-claude-fleet-cutover.md` für die interaktiven Host-Steps (`claude setup-token`, Vault-Eintrag, Alembic upgrade, docker compose up --build, sync-config Loop).

## Relevante Files

- `docker/mc-claude-agent/` — neues Image (Dockerfile, entrypoint.sh, start-claude.sh, poll.sh, lib/)
- `docker/docker-compose.agents.yml` — Fleet-Split (2 Anchors)
- `backend/alembic/versions/0080_seed_anthropic_claude_runtimes.py`
- `backend/alembic/versions/0081_claude_fleet_runtime_binding.py`
- `backend/app/routers/internal.py` — Bootstrap-Endpoint mit runtime-slug Logik
- `backend/config/runtimes.json` — Seed-File für Open-Source-Deploys

## Historie

- 2026-04-20 — initial Plan (`docs/superpowers/plans/2026-04-20-anthropic-claude-fleet-migration.md`)
- 2026-04-20 — Pivot zu Hybrid nach Boss-Setup-Audit (Host-Skalierung nicht trivial)
- 2026-04-20 — Implementation Phase 1-6 auf `feature/claude-fleet-migration`
