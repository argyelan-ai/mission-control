# ADR-042 — unsloth_porsche: power-managed Runtime (PORSCHE) + Wake-on-LAN + Runtime-Readiness Dispatch-Gate

**Status:** Accepted
**Datum:** 2026-06-24
**Scope:** Infra/Runtime · Backend/Runtime · Backend/Dispatch · Backend/DB

## Kontext

PORSCHE ist eine **Windows-Box** (LAN `192.0.2.100`, Tailscale `<tailscale-ip>`,
MAC `00:11:22:33:44:55`), die einen lokalen **unsloth-OpenAI-Server** betreibt.
Sie soll als LLM-Runtime in Mission Control nutzbar werden — sodass bestehende
cli-bridge/host-Agenten sich per `agent.runtime_id` daran binden, **genau wie heute
an die DGX-Spark-Runtimes** (vLLM/LMStudio/unsloth).

Zwei Eigenschaften machen PORSCHE fundamental anders als alle bisherigen Runtimes:

1. **Anderer Control-Plane.** Der bestehende `unsloth`-Type läuft auf dem DGX Spark
   (Linux `192.0.2.10`) und wird über **SSH + tmux** (`_ssh_run`) gesteuert. PORSCHE
   ist Windows und wird über einen **Flask-Server auf `:5555`** (`POST /powershell`,
   `GET /health`) gesteuert — kein SSH, kein tmux. Health läuft über die
   OpenAI-Probe `/v1/models` statt `/api/health`.
2. **Power-managed.** Die Box **schläft im Leerlauf** und muss vor Nutzung per
   **Wake-on-LAN** geweckt werden. Alle anderen Runtimes laufen durch.

Daraus folgt ein praktisches Problem für den Dispatch: ein Agent läuft weiter als
24/7-Prozess auf dem Mac Mini (Container/Host), nur sein **LLM-Hirn (Inferenz)**
sitzt auf PORSCHE. Wird ein Task in seine Session injiziert während die Box schläft,
geht der Dispatch ins Leere. MC ist bereits **pull-basiert** (Agent pollt
`/agent/me/poll`) — das öffnet einen sauberen Weg, den Task zu *parken*, bis das Hirn
online ist.

Zusätzlich kann das Backend (Docker-Container) selbst kein Wake-on-LAN-Magic-Packet
senden: WoL ist ein L2-Broadcast und verlässt den Container-Netzstack i.d.R. nicht.

Vollständige Analyse: `docs/plans/2026-06-24-porsche-unsloth-runtime-design.md`.

## Entscheidung

Ein **neuer `runtime_type` `unsloth_porsche`** mit eigenem Lifecycle-Branch, einem
Host-Helper für Wake-on-LAN und einem **fail-open Runtime-Readiness-Gate** im
Dispatch, das **ausschliesslich power-managed Runtimes** betrifft.

Konkret umgesetzt (bereits committed auf `feat/porsche-unsloth-runtime`):

1. **Eigener Runtime-Type statt DGX-Branch erweitern.** `get_runtime_state`,
   `start_runtime`, `stop_runtime`, `restart_runtime` in
   `services/runtime_manager.py` bekommen je einen eigenen
   `if runtime_type == "unsloth_porsche":`-Block. Der bestehende `unsloth`-Pfad
   (SSH/tmux, DGX) bleibt **unangetastet** → null DGX-Regressionsrisiko.

2. **Control via Flask `:5555` + PowerShell.** Neue Helper
   `_porsche_reachable(control_url)` (TCP/HTTP-Check `GET /health` — Box wach?) und
   `_porsche_powershell(control_url, command, timeout)` (`POST /powershell`, liefert
   `(stdout, stderr, returncode)` analog `_ssh_run`). Start nutzt das
   `runtime.launch_command`-Feld (PowerShell-Befehl, der den unsloth-OpenAI-Server
   startet); Stop killt best-effort den Prozess am OpenAI-Port
   (`_porsche_default_stop_command`, gibt VRAM frei).

3. **Neue, nullable Runtime-Felder.** Auf dem `Runtime`-Model + `RuntimeCreate`/
   `RuntimeUpdate` + `to_registry_dict`: `control_url` (Flask-`:5555`-URL),
   `wol_mac_address` (Ziel-MAC) und `power_managed` (bool, default `false`,
   `server_default text("false")`). Migration **0130** fügt die drei Spalten hinzu;
   alle bestehenden Runtimes behalten NULL/false → unverändertes Verhalten. Der
   `unsloth-porsche`-Seed-Row kommt idempotent aus `backend/config/runtimes.json`
   über `runtime_seeder` und ist **`enabled=false`** bis die echten PORSCHE-Werte
   (OpenAI-Port, Modellname, `launch_command`) eingetragen sind.

4. **Bedarfsgesteuerter Lebenszyklus.** WoL weckt nur die Box (billig, kein Modell).
   Das Modell wird **on demand** via Start in den VRAM geladen (Warmup ~1–3 Min). So
   laufen GPU/VRAM/Strom nur, wenn tatsächlich inferiert wird — kein Autostart.
   State-Mapping (aus `get_runtime_state`, Feld `state` + Debug-Feld
   `container_status`):
   - `:5555` nicht erreichbar → `state="stopped"`, `container_status="asleep"`
   - `:5555` da, `/v1/models` ≠ 200 → `state="stopped"`, `container_status="booted_no_model"`
   - `:5555` da, `/v1/models` = 200 → `state="ready"`, `container_status="serving"`

5. **Wake-on-LAN über Host-Helper.** Neuer Endpoint
   `POST /api/v1/runtimes/{id}/wake` → `runtime_manager.wake_runtime(rt)`. Da das
   Backend kein L2-Broadcast senden kann, **schreibt es eine Trigger-Datei** nach
   `settings.wake_request_dir` (= `~/.mc/wake-requests/<slug>.request.json`, unter
   dem bestehenden `~/.mc`-Bind-Mount, in Docker via `HOME_HOST` derselbe absolute
   Pfad). Shape: `{slug, mac, ip, broadcast, requested_at}`. Ein host-seitiger
   launchd-Watcher auf dem Mac liest die Datei und ruft das vorhandene
   `~/.claude/skills/wake-porsche/wake_porsche.py`
   (`--mac --ip --broadcast --wait`) auf, das das Magic-Packet schickt.
   `wake_runtime` ist hart auf `power_managed` gegated (400 sonst), 404 wenn die
   Runtime fehlt.

6. **Runtime-Readiness Dispatch-Gate (fail-open, nur power-managed).** Neuer Service
   `services/runtime_readiness.py` mit `runtime_ready_for_agent(agent, session)
   → (allowed, reason)`. Konsultiert an **beiden** Dispatch-Einstiegen:
   - `operations.check_dispatch_allowed` (Schritt „3.5", neuer optionaler
     `session`-Parameter; alle 6 Push-Dispatch-Aufrufstellen — 5 Dateien,
     `task_lifecycle.py` mit zweien — übergeben `session`).
   - `routers/agents.py::agent_poll` (Poll-Pull-Claim-Pfad, nur der frische
     Inbox-Task wird gegated; Recovery- und phase_approval-Claims bleiben
     unberührt).

   **Eiserne Leitplanke:** Das Gate gibt **sofort `(True, None)`** zurück, wenn
   (a) der Kill-Switch `settings.enable_runtime_readiness_gate` aus ist, (b)
   `agent.runtime_id` NULL ist, oder (c) die gebundene Runtime nicht `power_managed`
   ist. Damit läuft **jeder andere Agent** (24/7 cli-bridge, host Boss/Hermes/Jarvis,
   DGX-vLLM/LMStudio/unsloth, cloud) den **unveränderten** Pfad. Readiness wird in
   Redis kurz gecacht (`settings.runtime_readiness_cache_ttl`, 15 s), damit der
   5 s-Poll-Loop `:5555` nicht hämmert. **Jeder unerwartete Fehler fällt OPEN**
   (erlaubt Dispatch) — ein Gate-Bug kann die Fleet nie stalled lassen.

7. **Default manuelles Wecken.** Auto-Wake-on-dispatch ist bewusst **nicht** gebaut
   (siehe Alternativen) — Default ist: Mark klickt „Wecken", die Box bootet, Start
   lädt das Modell, bei `ready` zieht der bereits geparkte Task beim nächsten Poll
   automatisch in die Session.

8. **Runtime-Writes sind admin-only.** `launch_command` und `control_url` fliessen in
   eine PowerShell-Ausführung bzw. einen ausgehenden Backend-POST. Die schreibenden
   DB-CRUD-Endpoints (`POST/PATCH/DELETE /runtimes/db…`) sind daher auf
   `require_role(Role.ADMIN)` gehoben (vorher `require_user`, das auch Viewer zuliess)
   — analog zur Secrets-Tabelle (ADR-033). `control_url` wird zusätzlich validiert
   (muss `http(s)://`). Das schliesst den RCE-/SSRF-Vektor „Viewer setzt
   `launch_command` → Start führt es auf PORSCHE aus".

## Alternativen

- **Den bestehenden DGX-`unsloth`-Branch erweitern (statt neuer Type).** → Verworfen.
  Der `unsloth`-Pfad ist fest auf `_ssh_run`/tmux/`/api/health` verdrahtet; ihn auf
  PORSCHEs Flask-`:5555`/PowerShell/`/v1/models` umzubiegen würde den DGX-Pfad
  brechen oder mit Sonderfällen durchlöchern. Eigener Type = minimaler Eingriff,
  null Regressionsrisiko.

- **Backend sendet WoL direkt (host-network / privilegierter Container).** →
  Verworfen. L2-Broadcast aus dem Docker-Netzstack ist unzuverlässig und würde einen
  host-network- oder privileged-Hack erfordern. Der Trigger-File-Weg nutzt den
  bestehenden `~/.mc`-Bind-Mount (über den Agenten heute schon laufen), braucht
  keinen neuen Dienst und ist sauber testbar.

- **Auto-Wake-on-dispatch (Zuweisung weckt die Box automatisch).** → Vorerst
  zurückgestellt (deferred), als spätere Opt-in-Stufe (Flag `auto_wake` pro Runtime,
  Default aus). Default ist manuelles Wecken — Strom/Geschmack bleiben in Marks Hand,
  und ein versehentlich zugewiesener Task soll nicht ungefragt eine schlafende Box
  hochfahren.

- **Periodisches Background-Readiness-Probing.** → Verworfen (konsistent mit D-22 /
  ADR-028). Es würde `:5555` dauerhaft belasten und die Box potenziell wach halten.
  Stattdessen wird nur geprobt, wenn für den Agent tatsächlich ein Task wartet, und
  das Ergebnis kurz in Redis gecacht.

## Konsequenzen

### Positiv
- PORSCHE wird eine vollwertige LLM-Runtime — Agenten binden sich per `runtime_id`
  wie an den DGX, ohne dass der Agent selbst umzieht.
- **Null DGX-Regressionsrisiko**: eigener Type/Branch, bestehende `unsloth`-Pfade
  unangetastet; alle neuen DB-Felder nullable/default-off.
- **Strom/VRAM nur bei Bedarf**: WoL ist billig, das Modell lädt erst on demand.
- **Die 24/7-Fleet ist strukturell geschützt**: das Gate greift nur für
  power-managed-gebundene Agenten, mit frühem Return + Kill-Switch + fail-open.
- Tasks gehen nie ins Leere: ein Task für eine schlafende PORSCHE bleibt geparkt
  (inbox, `dispatched_at` ungesetzt), bis das Hirn `ready` ist.

### Negativ / Aufpassen
- **Dispatch-Eingriff bleibt High-Risk.** `check_dispatch_allowed` und `agent_poll`
  sind heisse Pfade (Impact-Tabelle in CLAUDE.md). Mitigation ist eingebaut (früher
  Return, fail-open, Kill-Switch, Cache) + Regressions-Tests
  (`tests/test_runtime_readiness_gate.py`), aber jede künftige Änderung am Gate muss
  beweisen, dass Nicht-power-managed-Agenten verhaltensidentisch bleiben.
- **Neuer beweglicher Teil: der host-seitige launchd-Watcher.** Backend und Watcher
  kommunizieren nur über Trigger-Dateien; fällt der Watcher aus, passiert beim
  „Wecken" nichts (Backend meldet trotzdem `ok`, weil es nur die Datei schreibt).
  Watcher-Gesundheit ist Betriebsverantwortung.
- **Platzhalter-Werte.** Der Seed ist `enabled=false`; `endpoint`, `model_identifier`
  und `launch_command` sind TODO-Platzhalter, bis die echten PORSCHE-Werte feststehen.
  `start_runtime` verweigert den Start, solange `launch_command` mit `TODO` beginnt.
- **DHCP-Risiko.** `192.0.2.100` kann wechseln → DHCP-Reservation oder mDNS-Fallback
  empfohlen (offener Betriebspunkt).
- Der Modell-Warmup (1–3 Min nach Start) liest kurz als `booted_no_model`, bis
  `/v1/models` antwortet — für v1 akzeptiert; die Start-Meldung weist darauf hin.

### Sicherheit

- **Command-Ausführung ist gewollt** (`launch_command` → PowerShell auf PORSCHE). Die
  Kontrolle liegt daher bei der Autorisierung: Runtime-Writes sind admin-only
  (Punkt 8), `control_url` ist auf `http(s)://` validiert. Damit kann kein
  niedrig-privilegierter MC-Account einen RCE-/SSRF-Vektor öffnen.
- **Offener Betriebspunkt (PORSCHE-seitig, nicht in diesem Repo):** Der Flask-`:5555`-
  Server hat laut Runbook **keine eigene Authentifizierung** und ist im LAN
  erreichbar. Jeder im `192.0.2.0/24` könnte dort PowerShell ausführen — unabhängig
  von MC. **Empfehlung:** `:5555` per Firewall auf den Mac Mini beschränken **oder**
  ein Shared-Secret/Token zwischen Backend und `:5555` einführen. Bis dahin ist die
  LAN-/Tailscale-Beschränkung (Single-Operator-Setup) die einzige Schranke.
- **Beobachtbarkeit:** Schreibfehler beim Wake-Trigger und ein down-er Watcher sind
  heute nur als 400/Log sichtbar (siehe Negativ) — als Follow-up ein Ops-Event +
  Deploy-Assertion (`wake_request_dir` == Watcher-`WatchPaths`).

## Referenzen

- Betroffene Dateien:
  - `backend/app/models/runtime.py` — `control_url`, `wol_mac_address`, `power_managed` + `to_registry_dict`
  - `backend/alembic/versions/0130_runtime_power_managed.py` — Migration (3 Spalten)
  - `backend/app/services/runtime_manager.py` — `_porsche_reachable`, `_porsche_powershell`, `_porsche_default_stop_command`, `unsloth_porsche`-Branches in `get_runtime_state`/`start_runtime`/`stop_runtime`/`restart_runtime`, `wake_runtime`
  - `backend/app/services/runtime_readiness.py` — `runtime_ready_for_agent` / `is_runtime_ready` / `_probe_ready`
  - `backend/app/services/operations.py` — `check_dispatch_allowed` Schritt 3.5 (`session`-Param)
  - `backend/app/routers/agents.py` — `agent_poll` Readiness-Gate (frischer Inbox-Task)
  - `backend/app/routers/runtimes.py` — `POST /{runtime_id}/wake` + `RuntimeCreate`/`RuntimeUpdate`-Felder
  - `backend/app/services/runtime_seeder.py` — seedet `control_url`/`wol_mac_address`/`power_managed`/`launch_command`
  - `backend/config/runtimes.json` — `unsloth-porsche` Seed (`enabled=false`)
  - `backend/app/config.py` — `porsche_lan_ip`/`porsche_mac`/`porsche_broadcast`/`porsche_control_url`/`wake_request_dir`/`enable_runtime_readiness_gate`/`runtime_readiness_cache_ttl`
  - `~/.claude/skills/wake-porsche/wake_porsche.py` — host-seitiges WoL-Skript
- Tests: `backend/tests/test_runtime_manager_porsche.py`, `backend/tests/test_runtime_readiness_gate.py`
- Design-Doc: `docs/plans/2026-06-24-porsche-unsloth-runtime-design.md`
- Verwandte ADRs: ADR-017 (Runtime Registry in DB), ADR-028 (Runtime Registry DB-only + Background-Probing verworfen), ADR-027 (Agent↔Runtime Binding), ADR-029 (Hermes — single_instance Non-Switchable-Pattern), ADR-036 (Runtime `launch_command`)
