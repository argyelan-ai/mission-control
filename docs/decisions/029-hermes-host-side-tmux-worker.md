# ADR-029 — Hermes als host-side tmux Worker mit eigener Bridge

**Status:** Accepted
**Datum:** 2026-04-30
<!-- Status: Accepted -->
**Scope:** Infra/Runtime, Backend/DB, Backend/Provisioning, Frontend/Runtimes

## Kontext

Mit dem v0.8 Milestone wird Hermes als 12. Mission-Control-Agent eingeführt — ein additiver Pilot, der das bestehende Agent-Team (Boss, Henry, 9 Docker-Agents, Sparky) ergänzt. Hermes ist **kein** weiterer claude- oder openclaude-Agent, sondern bringt sein eigenes CLI-Binary (`~/.local/bin/hermes`) mit eigener State-Schicht (`~/.hermes/`), eigenen Skills und eigener internen Self-Improving-Loop mit. Die Integration in MC darf diese eigenständige Architektur nicht aufweichen, soll aber alle bestehenden MC-Konventionen erben (Workspace-Layout, Sessions-Tab Streaming, Runtime-Binding, Token-Routing).

Drei Kernfragen mussten beantwortet werden:

1. **Wo läuft Hermes?** Docker oder Host?  Hermes-State (`~/.hermes/`) ist nativ auf dem Mac angelegt (SOUL.md, Skills, internal DB). Ein Docker-Container müsste den State entweder spiegeln (zerbrechlich) oder via Bind-Mount durchreichen (funktioniert, bringt aber keinen Mehrwert gegenüber Host-Lifecycle). Boss läuft seit ADR-014 als Host-Worker — derselbe Pattern ist hier die naheliegende Antwort.
2. **Eigene Bridge oder cli-bridge.py erweitern?**  `scripts/cli-bridge.py` ist seit ADR-013 explizit Docker-Agent-Config-Management (settings.json, plugins, claude-config Mirror). Hermes hat weder `settings.json` noch Claude-Code-Plugins — eine Erweiterung von cli-bridge.py um eine Hermes-Sonderbehandlung würde die saubere Trennung kaputt machen. `scripts/free-code-bridge.py` zeigt, dass eigenständige host-side Bridges das etablierte Muster sind.
3. **Wie verhindern wir Mehrfach-Instanzen?**  Hermes hält interne State-DB-Files in `~/.hermes/` über Sessions hinweg. Zwei parallel laufende Hermes-Prozesse gegen dasselbe `~/.hermes/state.db` würden den Zustand korrumpieren — und genau das könnte der bestehende Runtime-Switch (ADR-027) ungewollt auslösen, wenn jemand einen anderen Agent auf die Hermes-Runtime bindet oder Hermes selbst auf eine zweite Runtime-Variante switched. Ohne Hard-Stop ist das ein Foot-Gun.

Drei Optionen für die Mehrfach-Verhinderung standen im Raum:

- **Hardcode-Whitelist** (`if runtime.id == "hermes-vllm": raise ...`) — verworfen: kein generisches Pattern, jeder zukünftige host-side Worker müsste manuell in der Liste landen.
- **Per-Agent Lock-Flag** (`agents.runtime_locked: bool`) — verworfen: koppelt das Lock an den Agent statt an die Runtime, würde bei Hermes-Re-Provisioning verloren gehen.
- **Per-Runtime Single-Instance-Flag** (`runtimes.single_instance: bool`) — gewählt: das Constraint sitzt da, wo es entsteht (die Runtime selbst weiss, dass sie keine zweite Instanz verträgt), und alle zukünftigen host-side Worker erben das Pattern automatisch ohne Code-Change.

## Entscheidung

Hermes wird als 12. Agent integriert nach folgenden sechs Punkten:

1. **Host-side tmux Worker.** Hermes läuft als macOS-Prozess in einer dedizierten tmux-Session `hermes-worker` (analog Boss' `boss-host`). Workspace liegt in `~/.openclaw/agents/hermes/` gemäss ADR-022. Kein Docker-Container, kein Container-Lifecycle.
2. **vLLM-Provider Reuse.** Hermes spricht denselben vLLM-Endpoint an, der heute Sparky bedient: `http://192.0.2.10:8000/v1` mit `Qwen/Qwen3.6-35B-A3B-FP8`. Keine neue Infrastruktur, keine zusätzliche GPU-Last — Sparky und Hermes teilen sich den Provider.
3. **Single-Instance non-switchable** (single-instance Pattern). Neues DB-Feld `runtimes.single_instance: bool` (Default `false`). Hermes-Runtime hat `single_instance: true`. `services/agent_runtime_switch.switch_agent_runtime` prüft vor jeder Bindungs-Mutation, ob die Quelle ODER das Ziel `single_instance=True` ist, und raised in dem Fall `AgentNotSwitchableError` (HTTP 422). Frontend (`RuntimeSwitchModal.tsx` + `RuntimePill.tsx`) zeigt solche Runtimes als disabled mit Lock-Cue. Generisches Pattern: ohne Hardcode-Erwähnung von Hermes — andere künftige host-side Worker erben es.
4. **NICHT in `docker/docker-compose.agents.yml`.** Das File ist seit ADR-027 generator-managed durch `compose_renderer.py` aus dem DB-State und wird bei jedem Cross-Image-Switch neu gerendert. Ein Hermes-Eintrag würde beim nächsten Switch eines anderen Agents verloren gehen — und müsste auch da nicht stehen, weil Hermes host-side läuft, nicht Docker.
5. **Eigene `scripts/hermes-bridge.py`.** Pattern-Vorbild: `scripts/free-code-bridge.py`. Verantwortlichkeiten: `agent.env` laden, `tmux new-session -d -s hermes-worker hermes` mit env starten, Health-Check per tmux-Session-Existenz, optional Re-Provision via MC-API. Auto-Start beim Boot via launchd-Plist `~/Library/LaunchAgents/com.mc.hermes-bridge.plist` (analog Boss' `com.openclaw.boss`). Kein Code-Pfad mit `cli-bridge.py` geteilt — Hermes-Binary unterscheidet sich grundlegend vom claude-Binary, und cli-bridge.py ist Docker-Agent-Config-Management gemäss ADR-013.
6. **Migration 0095** erweitert `runtimes` um `single_instance` (NULL-able BOOL, Default `false`), seedet die Hermes-Runtime via `runtimes.json` + `runtime_seeder.py` (idempotent), und INSERTet die Hermes-Agent-Row idempotent (`ON CONFLICT (slug) DO NOTHING`) mit `agent_runtime='host'`, `runtime_id` auf die neu geseedete Hermes-Runtime, `workspace_path='~/.openclaw/agents/hermes'`, `provision_status='provisioned'`.

`runtime_manager.build_runtime_env(rt)` bekommt einen neuen Branch für `runtime.runtime_type == "hermes"`: rendert `OPENAI_BASE_URL=http://192.0.2.10:8000/v1`, `OPENAI_MODEL=<rt.model_identifier>`, `MC_AGENT_TOKEN`, `MC_BASE_URL` — **kein** `ANTHROPIC_AUTH_TOKEN`. Output landet als `~/.openclaw/agents/hermes/agent.env` mit chmod 600. `hermes-bridge.py` source'd die Datei vor `tmux new-session`.

## Alternativen

- **Hermes als Docker-Agent.** Eigenes `mc-hermes-agent`-Image bauen, in `docker-compose.agents.yml` einreihen. Verworfen — Hermes-State (`~/.hermes/`) ist host-nativ; Container-Lifecycle bringt keinen Vorteil, kostet Build-Pipeline + Image-Storage, und der nächste Cross-Image-Switch eines Docker-Agents würde den hardcoded Hermes-Eintrag wegrendern.
- **cli-bridge.py erweitern.** In `cli-bridge.py` einen `if agent.runtime_type == "hermes":`-Branch einbauen. Verworfen — bricht die saubere Trennung „cli-bridge = Docker-Config-Management" und macht jede zukünftige Hermes-Bridge-Änderung zu einer Docker-Bridge-Änderung mit entsprechendem Blast-Radius.
- **Hardcode-Whitelist für Single-Instance.** In `agent_runtime_switch` eine Liste `NON_SWITCHABLE_RUNTIME_IDS = {"hermes-vllm"}` führen. Verworfen — kein generisches Pattern, jede neue host-side Runtime braucht einen Code-Change.
- **Per-Agent Lock-Flag (`agents.runtime_locked`).** Verworfen — Lock gehört zur Runtime, nicht zum Agent; bei Re-Provisioning des Hermes-Agents würde das Flag verloren gehen.
- **Eigene vLLM-Instanz für Hermes.** Verworfen — Verdopplung der GPU-Last + Operations-Komplexität ohne erkennbaren Benefit; Qwen3.6-35B-A3B-FP8 packt zwei Worker problemlos im Pilot-Volumen.

## Konsequenzen

### Positiv

- **Additiv.** Bestehende 11 Agents (Boss, Henry, 9 Docker-Agents inkl. Sparky) bleiben unverändert. Kein Refactor an `cli-bridge.py`, `compose_renderer.py` oder `docker_agent_sync.py`.
- **Generisches Single-Instance-Pattern.** `runtimes.single_instance` ist nicht Hermes-spezifisch — der nächste host-side Pilot-Worker (z.B. künftige spezialisierte Tools) erbt das Verhalten ohne Code-Change.
- **vLLM-Reuse.** Kein zusätzlicher Inferenz-Server, keine zusätzliche GPU-Allokation. Sparky + Hermes teilen sich den vLLM-Endpoint, passend zur Pilot-Phase.
- **Saubere Trennung.** `cli-bridge.py` (Docker-Config), `free-code-bridge.py` (Free-Code-spezifisch), `hermes-bridge.py` (Hermes-spezifisch) — drei eigenständige Bridges mit klarem Ownership. Der Operator kann Hermes patchen ohne Docker-Agents zu riskieren.
- **Token-Routing isoliert.** Neuer `runtime_type == "hermes"` Branch in `build_runtime_env(rt)` ist Unit-testbar — keine Vermischung mit claude/openclaude Routing.
- **Frontend lernt Lock-State.** `RuntimeSwitchModal` + `RuntimePill` bekommen ein neues, generisches Disabled-Cue für `single_instance` Runtimes — nutzbar für alles, was künftig non-switchable ist.

### Negativ

- **Drei host-side Bridges (Boss-Plist, free-code-bridge, hermes-bridge).** Wenn der gemeinsame Anteil (env-Loading, MC-API-Client, Health-Check, launchd-Plist-Pattern) wächst, müssen wir einen Refactor nach `scripts/_bridge_common.py` ziehen. Phase 24 trägt das pragmatisch self-contained — der Refactor ist explizit als v0.9-Kandidat vermerkt, nicht eingebaut.
- **launchd-Plist Doku-Schuld.** Es gibt heute keinen dokumentierten gemeinsamen Plist-Pattern für host-side Bridges. Hermes-Plist macht den dritten Eintrag (Boss, ttyd, Hermes), und `docs/agent-state.md` muss den Auto-Start-Lifecycle künftig sauber abbilden.
- **vLLM Single-Point-of-Failure.** Sparky **und** Hermes hängen am selben Endpoint. Wenn der vLLM-Server kippt, sind beide Agents weg. Akzeptabel im Pilot — aber bei v0.9-Skalierung muss eine zweite Instanz oder ein Failover dazu.
- **Single-Instance ist Runtime-weit.** Wer künftig eine zweite Hermes-Variante (z.B. mit anderem Modell) als separate Runtime registriert, muss `single_instance` ggf. anders modellieren — z.B. via Tag/Group statt Boolean. Der Boolean reicht für Phase 24 + erkennbaren v0.8-Horizon.
- **Internal LAN IP in ADR.** `192.0.2.10` ist im Klartext dokumentiert. Selber Disclosure-Level wie ADR-028; kein Risiko für Public Repos, aber bei Multi-Tenant-Setups später zu abstrahieren.

## Implementierung

- `backend/alembic/versions/0095_*.py` — Migration: `runtimes.single_instance: BOOL DEFAULT false`, Hermes-Runtime-Seed (via `runtimes.json` Pickup durch `runtime_seeder.py`), Hermes-Agent-Row INSERT idempotent.
- `backend/config/runtimes.json` — neuer Eintrag `hermes-vllm` mit `runtime_type: "hermes"`, `endpoint: "http://192.0.2.10:8000/v1"`, `model_identifier: "Qwen/Qwen3.6-35B-A3B-FP8"`, `single_instance: true`.
- `backend/app/models/runtime.py` — neues Feld `single_instance: bool = False`.
- `backend/app/services/runtime_manager.py` — `build_runtime_env(rt)` neuer Branch für `runtime_type == "hermes"`.
- `backend/app/services/agent_runtime_switch.py` (§150–285) — `switch_agent_runtime` ergänzt Pre-Check auf `single_instance`, raised `AgentNotSwitchableError` bei Verletzung.
- `scripts/hermes-bridge.py` — NEU, Pattern-Vorbild `free-code-bridge.py`.
- `~/Library/LaunchAgents/com.mc.hermes-bridge.plist` — NEU, Auto-Start beim Boot.
- `frontend-v2/src/components/shared/RuntimeSwitchModal.tsx` — Disabled-State + Erklärung für `single_instance`.
- `frontend-v2/src/components/shared/RuntimePill.tsx` — Lock-Cue + neuer `RUNTIME_TYPE_COLOR`-Eintrag für `hermes`.

## Verwandte ADRs

- **ADR-027** (Universal Agent ↔ Runtime Binding) — Switch-Service, in den der `single_instance` Pre-Check eingehängt wird.
- **ADR-028** (Runtime Registry DB-only + Session-Env-Propagation) — `build_runtime_env` Helper, in dem der `runtime_type == "hermes"` Branch sitzt; DB-only Registry, in der `single_instance` als neues Feld lebt.
- **ADR-022** (`~/.mc/` Home + Standardized Workspace Layout) — Hermes erbt die `~/.openclaw/agents/<slug>/` Konvention.
- **ADR-014** (Boss runs as macOS host process) — Vorbild für host-side Worker mit launchd + tmux.
- **ADR-013** (MC V2 Docker-Agents Live-Deployment) — definiert `cli-bridge.py` als Docker-Config-Management; ADR-029 begründet, warum Hermes daran nicht andockt.
- **ADR-003** (Triple-Runtime-Architektur) — Hermes ergänzt das `host`-Runtime-Bucket (Boss + Hermes), `cli-bridge` und `openclaw` bleiben unberührt.
