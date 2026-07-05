# ADR-058 — CLI-Tool-Updates aus User-Sicht (Manifest + Host-Bridge-Build + Rolling Recreate)

**Status:** Accepted
**Datum:** 2026-07-05
**Scope:** Infra/Runtime · Backend/DB · Backend/Services · Frontend/Runtimes

## Kontext

Die drei Agent-CLI-Tools (`openclaude`, `claude`, `omp`) sind in den Docker-Images
festgebacken, mit uneinheitlichem Pinning: `openclaude` als Literal in der
Dockerfile (`@gitlawb/openclaude@0.7.0`), `claude` komplett ungepinnt
(`install.sh` zieht bei jedem Rebuild latest), `omp` bereits mit `ARG
OMP_VERSION` + sha256-Pin (ADR-045). Ein Update verlangte bisher
Dockerfile-Edit + manuellen Rebuild + manuelles Container-Recreate — für
Downstream-User (Open-Source-Release seit ADR-043) unzumutbar, und das Backend
wusste nichts über installierte oder verfügbare Versionen.

Alternativen für den Update-Weg selbst:

- **GHCR-Publish der fertig gebauten Agent-Images**, Backend zieht per `docker
  pull`. Näher an "one-click", aber ein neuer CI-Release-Zweig (Image-Matrix ×
  CLI-Version) und eine Release-Kadenz, die von der CLI-Release-Kadenz
  entkoppelt werden müsste.
- **Lokaler Rebuild auf dem Host**, angestossen vom Backend. Downstream baut
  die Agent-Images heute bereits lokal (`docs/setup/first-agent.md`,
  `scripts/build-agent-images.sh`) — kein neuer Baustein, nur eine
  Fernsteuerung des bestehenden Skripts.

Und für die Frage, wo die Soll-Version herkommt:

- **DB-Tabelle** für Ist-/Soll-Versionen. Verlangt neue Migration + Model nur
  für drei Zeilen Konfiguration, und der Wert müsste bei jedem Build ohnehin
  wieder als Datei (Dockerfile-`ARG`) vorliegen — zwei Quellen für denselben
  Fakt.
- **`latest` zur Build-Zeit** (wie `claude` bisher). Nicht reproduzierbar,
  keine Kontrolle über den Zeitpunkt des Updates, kein Rollback-Anker.
- **Datei-Manifest** (`docker/cli-versions.json`), das sowohl vom Build-Skript
  als auch vom Backend gelesen (und vom Backend beschrieben) wird.

## Entscheidung

1. **Update-Weg v1 = lokaler Rebuild, kein GHCR-Publish.** Der Build läuft
   weiterhin auf dem Host über `scripts/build-agent-images.sh`, jetzt
   fernausgelöst durch das Backend statt manuell. GHCR-Publish bleibt
   dokumentiertes v2 (siehe unten).
2. **`docker/cli-versions.json` ist die Single Source of Truth** für
   Soll-Versionen (+ `omp`-sha256). Committed, kein Secret. Gelesen von
   `scripts/build-agent-images.sh` (reicht Werte als `--build-arg` durch,
   CLI-Override `--version X` bleibt möglich) und von
   `backend/app/services/cli_versions.py`; geschrieben (atomar: tmp + rename)
   vom Update-Orchestrator. Jedes gebaute Image trägt zusätzlich die
   OCI-Labels `mc.cli.name` / `mc.cli.version` / `mc.image.built-at`, gelesen
   per `docker image inspect` für den Ist-Stand — kein separates
   "was ist installiert"-Tracking nötig.
3. **Build läuft auf dem Host via CLI-Bridge, nie im Backend-Container.**
   Der Docker-Socket-Proxy hat bewusst `BUILD: 0` (ADR-047 — Image-Builds
   laufen grundsätzlich auf dem Host). `scripts/cli-bridge.py` bekommt dafür
   `POST /agent-images/build` (startet `build-agent-images.sh <tool>` als
   Hintergrund-Subprozess, Log nach `~/.mc/logs/agent-image-build-<tool>.log`,
   409 bei laufendem Build) + `GET /agent-images/build/status` (Polling) +
   `POST /agent-images/omp-sha256` (TOFU-Digest-Berechnung, wenn die
   GitHub-Release keinen Asset-Digest liefert). Präzedenzfall: Plugin-Installs
   laufen bereits über dieselbe Bridge (Port 18792).
4. **UI auf `/runtimes`, neue Sektion "CLI-Tools"** statt einer eigenen Seite —
   CLI-Versionen sind konzeptionell Teil desselben "was treibt meine Agents an"
   -Cockpits wie die LLM-Runtime-Registry.
5. **Update-Check periodisch, kein Auto-Update.**
   `services/cli_update_check.py` läuft als Singleton-Loop (Muster wie
   `runtime_watcher.py`/`intelligence.py`: asyncio-Task, Redis-Lock gegen
   Multi-Worker, `settings.cli_update_check_interval` Default 6h, `0` = aus)
   und cached `{installed, target, latest, update_available}` pro Tool in
   Redis (`mc:cli:versions`). Quellen: npm-Registry `GET
   registry.npmjs.org/<pkg>/latest` für `openclaude`/`claude`, GitHub Releases
   `api.github.com/repos/can1357/oh-my-pi/releases/latest` für `omp`.
   `target=None` (Manifest fehlt Version) zählt bewusst **nicht** als
   Update-Signal. Ein Update wird nur einmal pro neuer Version als
   `cli.update_available`-Event gemeldet (Dedup gegen Redis-Cache), nie
   automatisch ausgeführt.
6. **Update-Orchestrierung** (`services/cli_update_runner.py`), ein Klick =
   ein Lauf, Redis-Lock `mc:cli:update-lock` (TTL 1800s, Owner-geprüfte
   Freigabe, TTL-Renewal während des Laufs):
   1. Manifest bumpen.
   2. Bridge-Build triggern, `GET /agent-images/build/status` pollen,
      Fortschritt (Phase + Log-Tail) nach `mc:cli:update-progress` (TTL 1800s)
      fürs UI-Polling.
   3. **Build-Fehlschlag:** alter Image-Tag bleibt unberührt (Docker ersetzt
      den Tag erst bei Erfolg) → Manifest-Rollback auf die alte Version,
      Event `cli.update_failed`, Lock frei. Rollback des Manifests passiert
      **nur**, solange kein Build-Erfolg vorliegt — nach einem erfolgreichen
      Build ist die neue Version die Wahrheit, auch wenn ein späterer Schritt
      scheitert.
   4. **Erfolg:** Agents des betroffenen Harness (`agents.harness`) werden
      markiert — idle sofort `force_recreate` (`docker compose up -d
      --force-recreate --no-deps`, bestehender Pfad aus
      `agent_runtime_switch`), busy → `agents.pending_recreate = true`
      (Migration `0147`). Ein neuer Watcher-Tick nach
      `runtime_propagation.sync_pending_agents()` (`mark_agents_for_recreate`
      / `recreate_pending_agents`, ADR-054-Mechanik wiederverwendet, aber
      `force_recreate` statt `docker restart`, weil sich das Image geändert
      hat, nicht nur die Runtime-Config) arbeitet Pending-Agents ab.
      Circuit-Breaker nach 3 Fehlversuchen (analog ADR-054).
   5. Event `cli.updated` + Hinweis in der UI: die Manifest-Änderung im Repo
      ist uncommitted — Commit ist bewusst Sache des Users, kein Auto-Commit
      ins Repo aus dem Backend heraus.
7. **API** `routers/cli_tools.py` unter `/api/v1/cli-tools`: `GET ""` (Liste,
   `require_user`), `POST /check` (Check erzwingen), `GET /update-status`
   (Fortschritt fürs Polling), `POST /{tool}/update` → 202 (nur `operator`-Rolle,
   409 wenn ein Update läuft).

## Alternativen

- **GHCR-Pull statt lokalem Build** → verworfen für v1: neue Release-Matrix
  nötig (Image × CLI-Version), CLI-Release-Kadenz ≠ MC-Release-Kadenz,
  Downstream baut heute schon lokal. Bleibt gültiges v2 (siehe unten).
- **Soll-Versionen in der DB statt im Datei-Manifest** → verworfen: der Wert
  müsste beim Build ohnehin als Datei (Dockerfile-`ARG`) vorliegen; ein
  Manifest im `docker/`-Verzeichnis ist die einzige Quelle, die sowohl
  Build-Skript als auch Backend ohne Duplikation lesen können, und bleibt
  reviewbar im Git-Diff.
- **`npm install -g` im Container-Entrypoint bei jedem Start** (statt Version
  im Image zu backen) → verworfen: macht jeden Container-Start
  netzwerkabhängig und nicht-reproduzierbar (genau das Problem, das bei
  `claude` heute schon besteht), und unterläuft das Ziel "Versionen sind
  explizit und geprüft, bevor sie live gehen".

## Konsequenzen

### Positiv
- Alle drei CLIs sind jetzt einheitlich versions-gepinnt und im Backend
  sichtbar (Ist/Soll/Latest), statt implizit im Dockerfile vergraben zu sein.
- Ein Update ist ein UI-Klick statt Dockerfile-Edit + manueller Rebuild +
  manuelles Recreate — deutliche Downstream-Zumutbarkeits-Verbesserung.
- Build-Fehler sind folgenlos für laufende Agents (alter Tag bleibt aktiv,
  Manifest-Rollback), kein Halb-Zustand.
- Busy-Agents werden nicht mitten in einem Task hart recreatet — sie holen den
  Recreate beim nächsten Watcher-Tick nach (wiederverwendet den
  ADR-054-Propagations-Mechanismus statt eine zweite Pending-Logik zu bauen).
- Kein neuer Netzwerk-Pfad: Build läuft konsequent auf dem Host über die
  bestehende CLI-Bridge, respektiert die Docker-Socket-Proxy-Regel `BUILD: 0`
  aus ADR-047.

### Negativ
- Ein Update dauert (Build-Zeit, typischerweise 1–5 Min je nach Image) — kein
  Instant-Swap, das Polling-UI muss das transparent machen.
- Nur ein Build gleichzeitig systemweit (Bridge-Lock) — bei drei
  gleichzeitigen Update-Wünschen wird sequenziert, nicht parallelisiert. Für
  v1 akzeptabel (seltene Aktion, kein Hochfrequenz-Pfad).
- Die Manifest-Änderung bleibt nach einem Update uncommitted im
  Arbeitsverzeichnis — der User muss selbst committen, sonst geht der neue
  Versionsstand beim nächsten `git pull` / Redeploy wieder verloren. Bewusst
  kein Auto-Commit aus dem Backend (kein Git-Identitäts-Ballast im
  Backend-Container).
- `cli_update_runner` und `runtime_propagation` teilen sich jetzt das gleiche
  Pending-Feld-Muster (`pending_runtime_sync` vs. `pending_recreate`) für zwei
  leicht unterschiedliche Zwecke (Config-Refresh vs. Image-Wechsel) — bei
  künftigen Änderungen an einem der beiden Pfade sorgfältig prüfen, dass der
  andere nicht implizit mitbetroffen ist.

## Referenzen

- Betroffene Dateien: `docker/cli-versions.json` (neu, Manifest),
  `scripts/build-agent-images.sh` (liest Manifest, JSON via `python3 -c`, kein
  neuer `jq`-Dep), `scripts/cli-bridge.py` (`POST /agent-images/build`,
  `GET /agent-images/build/status`, `POST /agent-images/omp-sha256`),
  `backend/app/services/cli_versions.py` (Ist-Stand via
  `docker image inspect` Labels), `backend/app/services/cli_update_check.py`
  (periodischer Check-Loop), `backend/app/services/cli_update_runner.py`
  (Update-Orchestrierung, Lock, Rollback), `backend/app/routers/cli_tools.py`
  (`/api/v1/cli-tools`), `backend/app/models/agent.py`
  (`pending_recreate` Spalte), `backend/app/services/runtime_propagation.py`
  (`mark_agents_for_recreate`/`recreate_pending_agents`),
  `backend/alembic/versions/0147_agent_pending_recreate.py`,
  `backend/app/config.py` (`cli_update_check_interval`), Frontend:
  `frontend-v2/src/components/shared/CliToolsSection.tsx`,
  `frontend-v2/src/app/runtimes/page.tsx`.
- Verwandte ADRs: ADR-043 (Open-Source-Release-Contract — der Grund, warum
  Downstream-Zumutbarkeit hier überhaupt zählt), ADR-045 (omp-Versions-Pin als
  Vorbild), ADR-047 (Docker-Socket-Proxy `BUILD: 0` — Grund für den
  Host-Bridge-Umweg), ADR-054 (Runtime Watcher — Pending-Agent-Propagations-
  Mechanik, hier für `pending_recreate` wiederverwendet), ADR-056
  (Harness/Provider-Decoupling — `agents.harness` ist die Achse, über die
  betroffene Agents nach einem CLI-Update identifiziert werden).
- v2 (bewusst nicht in v1): GHCR-Publish der Agent-Images (Release-CI-Matrix
  erweitern), Auto-Update-Policy (z. B. automatische Patch-Updates),
  Changelog-Anzeige im Update-Dialog (npm/GitHub Release Notes).
