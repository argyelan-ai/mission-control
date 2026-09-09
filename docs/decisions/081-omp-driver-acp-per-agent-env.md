# ADR-081 — Live auf ACP: OMP_DRIVER per Agent-Env im Renderer statt Pane

**Status:** Accepted
**Datum:** 2026-09-09
**Scope:** Infra/Runtime (compose_renderer + omp-bridge)

## Kontext

Der ACP-Pfad im omp-bridge (Subtask 2/4, #464) ist fertig und getestet:
`OMP_DRIVER=acp` schaltet `bridge.py` (`_omp_driver()`, Default `native`)
auf den stdio JSON-RPC-Treiber (`omp acp`, `acp_client.py`). Es fehlte der
Mechanismus, der die Variable pro Container setzt — und die Entscheidung,
ob ACP statt eines Dialog-Panes zum Live-Gate wird.

Ein Dialog-Pane (interaktives tmux-Pane, aus dem die Bridge TUI-Ausgabe
parst) war die Alternative: sichtbar, aber ein zweiter Parsing-Pfad mit
eigener Fehlerklasse. Der Pane war 2026 mehrfach Quelle von Encoder- und
Kollaps-Bugs (#210-Umlaute, Sidebar-Zeilen-Kollaps); ACP eliminiert die
Klasse, statt sie zu repieren. Gleichzeitig soll die Live-Gate-Umschaltung
beweisbar NUR den konfigurierten Agenten treffen: ein Fleet-weites
Env-Flag haette jeden omp-Agenten (Boss-Proben, Experimente) mit
umgestellt und den Sabotage-Test ("anderer Agent bleibt native")
unmoeglich gemacht.

## Entscheidung

1. **ACP statt Pane** fuer den Live-Gate-Verkehr: der im Deployment
   konfigurierte Agent faehrt ab dem naechsten Recreate auf
   `OMP_DRIVER=acp`.
2. **Rollout ueber den Renderer, per Agent:** neue Funktion
   `_agent_env_overrides(slug)` in
   `backend/app/services/compose_renderer.py` — sie gibt
   `{"OMP_DRIVER": "acp"}` genau dann zurueck, wenn der Slug in der
   Deployment-Config steht (`OMP_ACP_AGENT_SLUGS` in `.env`, Komma-Liste,
   gelesen via `app_config.omp_acp_agents()`); sonst `{}`. Injiziert via
   `_ensure_agent_env_overrides()` in `_rewrite_compose`
   (Bestandsservices) und `_build_new_agent_block` (Anhaenge neuer
   cli-bridge-Agenten). Default-Config ist leer: ohne Eintrag laeuft die
   ganze Fleet auf dem Bridge-Default `native` — der Agentenname steht
   in der Deployment-Config, nie im Code.
3. **Rollback = Env entfernen + recreate.** Den Slug aus
   `OMP_ACP_AGENT_SLUGS` streichen (oder dem Service manuell
   `OMP_DRIVER=native` setzen — bestehende Eintraege ueberlebt jedes
   Re-Rendering unveraendert), dann `docker compose up -d --force-recreate
   <agent-service>`. Die Bridge faellt ohne Env auf `native` zurueck; der
   native TUI-Pfad ist und bleibt der dokumentierte Rueckweg.

## Alternativen

- **Fleet-weites `OMP_DRIVER=acp` fuer alle mc-omp-agent-Services**
  (alter Patch bbe3b6c, nie auf main): eine Zeile Mechanismus weniger,
  aber der Sabotage-Test waere leer — "per-Agent" ist beweisbar nur,
  wenn ein zweiter omp-Agent ohne die Variable gerendert wird. →
  Verworfen.
- **Dialog-Pane statt ACP:** sichtbarer Fortschritt im UI, aber zweiter
  Parsing-Pfad mit eigener Fehlerklasse (Encoder, Zeilen-Kollaps,
  Pane-Lesen mitten im Zug kollidiert mit dem Interrupt-Ladder-Kontext,
  ADR-080). ACP ersetzt Parsen durch Protokoll-Events. → Verworfen.
- **Env direkt in docker-compose.agents.yml hart codieren:** verliert
  sich beim naechsten Render (Renderer schreibt die Datei aus DB-State)
  und driftet vom Code. → Verworfen.
- **Agent-Slugs als Dict im Renderer-Code:** der erste Entwurf hatte den
 Slug fest im Code (`_AGENT_ENV_OVERRIDES()`); der Review hat
  zurueckgebaut — Flottennamen gehoeren in die Deployment-Config, nicht
  in Code, Tests oder ADRs. → Verworfen (diese Fassung).

## Konsequenzen

### Positiv
- Beweisbarer Rollout: Sabotage-Test (zweiter omp-Agent ohne
  OMP_DRIVER) ist Teil der Testsuite
  (`backend/tests/test_compose_renderer_acp_env_overrides.py`, 15
  Faelle).
- Rollback ist ein Einzeiler ohne Code-Deploy (Config-Eintrag entfernen
  + recreate).
- Der Renderer-Mechanismus ist generisch: kuenftige per-Agent-Env-
  Umschaltungen (andere Treiber, Feature-Flags) sind eine Config-Zeile.

### Negativ
- Eine Aenderung der aktiven Agenten ist ein Config-Edit (+ Recreate),
  kein Klick. Bewusst gewaehlt — Env-Knobs pro Agent sind
  Infrastruktur, kein Agent-Setting; ein DB-Feld wuerde einen
  Rendering-Durchlauf pro Aenderung ohnehin erfordern.
- Solange ein Eintrag drin steht, rendert JEDE Fleet-Datei den
  konfigurierten Agenten auf ACP — ein "nur mal kurz native" braucht
  den manuellen service-level Override (der genau dafuer ueberlebt).

## Verwandt

- ADR-045 (omp headless harness), ADR-080 (Heartbeat-Steuerkanal/
  Interrupt-Ladder — funktioniert treiberunabhaengig).
- #464/#471/#473: ACP-Treiber, Nacharbeit, Vorschau-Kanal.
