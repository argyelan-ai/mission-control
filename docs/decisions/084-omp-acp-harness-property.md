# ADR-084 — ACP als Eigenschaft des omp-Harness (löst ADR-081 ab)

**Status:** Accepted (supersedes ADR-081)
**Datum:** 2026-09-15
**Scope:** Infra/Runtime (compose_renderer, harness_compat, config) + Backend/Dispatch (recovery, chat transport)

## Kontext

ADR-081 machte den ACP-Treiber (`OMP_DRIVER=acp`) an eine Namensliste fest:
`OMP_ACP_AGENT_SLUGS=sparky,rex` in der `.env` entschied über
`compose_renderer._agent_env_overrides(slug)` / `config.omp_acp_agents()`,
wer den ACP-Pfad bekommt. Die Folge beobachtet 2026-09-15: ein frisch
angelegter Agent (FreeCodes Wechsel auf omp/ollama) stand nicht auf der
Liste → kein ACP → nativer TUI-Pfad mit Pane-Scrape und
Komposer-Verifikation — mit vier gescheiterten Versuchen als Live-Evidenz.
Das ist dasselbe Muster, das ADR-056 mit `harness_compat.py` bereits
einmal aus dem Code entfernt hatte; über ADR-081 kam es über die
Deployment-Konfiguration zurück.

Der native Bridge-Weg ist dabei nicht defekt, aber strukturell fragil:
docs/dispatch-path-parity.md (Baseline 314c57f) zeigt, dass seine
Mechanismen (TUI-Injektion, `inject_file`-Komposer-Verifikation, Hook-
Signal-Datei, Pane-Scrape für Kontext-% — Zeilen 20/33/43) auf
Bildschirm-Erkennung beruhen; alle dokumentierten Gaps (G2–G7) sind
geschlossen, die Incident-Form bleibt aber das Pane selbst. ACP umgeht
die Klasse statt sie zu reparieren (ADR-081, #210-Umlaute,
Sidebar-Kollaps).

## Entscheidung

**Verhalten leitet sich aus Harness und Runtime-Fähigkeit ab, nie aus der
Identität eines Agenten.**

1. **`omp_driver_for(harness)` in `backend/app/services/harness_compat.py`**
   ist DIE Entscheidungsstelle: Harness `omp` → `"acp"`, jeder andere
   Harness → `"native"`. `compose_renderer._agent_env_overrides(harness)`
   bekommt den Harness (abgeleitet über `_harness_for_image(image)` aus dem
   aufgelösten Image, derselbe Signalweg wie `pick_image_for_harness`) statt
   des Slugs.
2. **`OMP_ACP_AGENT_SLUGS` verschwindet** (Settings-Feld, `.env.example`,
   compose-Passthrough, `config.omp_acp_agents()`). Der Notausstieg ist
   **einer für alle**: `OMP_DRIVER_DEFAULT=native` (Default `acp`) stellt
   den ganzen omp-Kader auf den nativen TUI-Pfad zurück. Der manuelle
   Per-Service-Rollback (`OMP_DRIVER=native` im Service-Block) überlebt
   das Re-Rendering unverändert.
3. **Fähigkeiten-Matrix je Harness** (`HarnessCapabilities` /
   `HARNESS_CAPABILITIES` in `harness_compat.py`), kodifiziert die zuvor
   verstreuten Bedingungen:
   | Harness | settings_extras (hooks+statusLine) | shared-mcp-Mount | Host-Launcher | cli_plugins/cli_skills |
   |---|---|---|---|---|
   | claude | immer | ja | ja | ja |
   | openclaude | je Runtime-Protokoll (`anthropic`) | nein | nein | ja |
   | omp | nie | nein | nein | nein |
   | kimi | nie | nein | nein | nein |
   | hermes/grok | nie | nein | nein | nein |
   Konsolidierte Fundstellen: `runtime_protocol(rt) == "anthropic"` in
   `plugin_manager`-Aufrufen (`agent_scoped.py`, `skills.py`,
   `docker_agent_sync.py` → `settings_extras_for()`), `harness == "claude"`
   in `docker_agent_sync.render_host_launcher_script`, claude-only
   shared-mcp-Mount im Renderer. Operating Card bleibt agents-seitiges
   Opt-in (`use_operating_card`), kein Harness-Merkmal.
4. **Der Wechsel meldet seine Folgen:** `switch_agent_runtime` hängt eine
   sichtbare Warnung an das `SwitchResult`, wenn der Zielharness keine
   CLI-Plugins/Skills unterstützt (`capabilities_for(...).cli_plugins`) und
   der Agent welche zugewiesen hat — kein stilles Wegfallen.
5. **Recovery leitet sich ab:** der implizite Tier-2-Skip (Neustart tötet
   den laufenden ACP-Turn, Messung 2026-09-14, PR #574) prüft
   `omp_driver_for(agent.harness)` statt der Slug-Vereinigung in
   `recovery_tier2_skip_agents()`. Die explizite Liste
   `RECOVERY_TIER2_SKIP_AGENT_SLUGS` bleibt als Operator-Opt-out für
   Host-Agenten, deren Bridge-Treiber das Backend nicht beobachten kann —
   sie ist Notfallventil, kein Verhaltensschalter.
6. **Wächter-Test** (`backend/tests/test_acp_harness_property.py`): die
   ADR-081-Bezeichner dürfen in Code und Deployment-Konfiguration nicht
   wiederauftauchen; neue `*_agent_slugs`-Settings-Felder sind Whitelist-
   pflichtig. Der Test wird rot, sobald wieder eine Namensliste als
   Verhaltensschalter eingeführt wird.

## Alternativen

- **Namensliste behalten (ADR-081-Status quo):** beweisbar pro Agent, aber
  jede neue Fleet-Instanz vergisst den Eintrag → der beobachtete
  FreeCode-Ausfall. Verworfen.
- **ACP pro Runtime statt pro Harness:** Runtime-Zeilen sind Provider-
  Bindungen, nicht Ausführungsmodi; derselbe Provider kann von claude
  (TUI) und omp (ACP) genutzt werden. Verworfen.
- **Pane-Reparatur statt ACP-Standard:** der native Pfad ist geschlossen,
  aber jede Reparatur ist ein neuer Parsing-Pfad mit eigener Fehlerklasse
  (ADR-081-Begründung). Bleibt Rückfallebene hinter `OMP_DRIVER_DEFAULT`,
  nicht Standard.

## Konsequenzen

### Positiv
- Ein frisch angelegter Agent bekommt beim Wechsel auf omp dieselbe
  Behandlung wie der ganze Kader — belegt am Neuzugang, ohne Konfiguration.
- Eine Entscheidungsstelle (`omp_driver_for`), ein globaler Rollback
  (`OMP_DRIVER_DEFAULT`), keine verstreuten Harness-/Slug-Bedingungen.
- Wechsel auf pluginlose Harnesses sind sichtbar warned.

### Negativ
- Der Sabotage-Test „ein einzelner Agent bleibt native“ ist entfallen —
  Rollbacks sind nur noch global oder per manuellem Service-Eintrag.
- `OMP_DRIVER_DEFAULT=acp` macht ACP für die ganze omp-Flotte zum Default;
  Bestandsagenten ohne Listen-Eintrag wechseln beim nächsten Recreate auf
  ACP (Gegenprobe: Veterans-Verhalten im Wächter-Test fixiert).
