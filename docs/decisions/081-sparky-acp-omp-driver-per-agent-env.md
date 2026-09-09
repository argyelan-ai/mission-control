# ADR-081 — Sparky live auf ACP: OMP_DRIVER per Agent-Env im Renderer statt Pane

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
beweisbar NUR einen Agenten treffen: ein Fleet-weites Env-Flag haette
jeden omp-Agenten (Boss-Proben, Experimente) mit umgestellt und den
Sabotage-Test ("anderer Agent bleibt native") unmoeglich gemacht.

## Entscheidung

1. **ACP statt Pane** fuer den Live-Gate-Verkehr: `sparky` faehrt ab dem
   naechsten Recreate auf `OMP_DRIVER=acp`.
2. **Rollout ueber den Renderer, per Agent:** neuer Mechanismus
   `_AGENT_ENV_OVERRIDES()` in `backend/app/services/compose_renderer.py`
   (slug → {VAR: value}, aktuell nur `sparky → OMP_DRIVER=acp`),
   injiziert via `_ensure_agent_env_overrides()` in `_rewrite_compose`
   (Bestandsservices) und `_build_new_agent_block` (Anhaenge neuer
   cli-bridge-Agenten). Jeder andere Agent — auch die uebrige
   omp-Fleet — bekommt keinen Eintrag und laeuft weiter auf dem
   Bridge-Default `native`.
3. **Rollback = Env entfernen + recreate.** Den Eintrag aus
   `_AGENT_ENV_OVERRIDES()` streichen (oder dem Service manuell
   `OMP_DRIVER=native` setzen — bestehende Eintraege ueberlebt jedes
   Re-Rendering unveraendert), dann `docker compose up -d --force-recreate
   mc-agent-sparky`. Die Bridge faellt ohne Env auf `native` zurueck; die
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

## Konsequenzen

### Positiv
- Beweisbarer Rollout: Sabotage-Test (zweiter omp-Agent ohne
  OMP_DRIVER) ist Teil der Testsuite
  (`backend/tests/test_compose_renderer_acp_driver.py`, 13 Faelle).
- Rollback ist ein Einzeiler ohne Code-Deploy (Env-Eintrag entfernen +
  recreate).
- Der Renderer-Mechanismus ist generisch: kuenftige per-Agent-Env-Umschal
  tungen (andere Treiber, Feature-Flags) sind eine Dict-Zeile.

### Negativ
- Agent-Slugs sind im Renderer-Code hart codiert (kein DB-Feld): eine
  Aenderung ist ein Commit, kein Klick. Bewusst gewaehlt — Env-Knobs
  pro Agent sind Infrastruktur, kein Agent-Setting; ein DB-Feld wuerde
  einen Rendering-Durchlauf pro Aenderung ohnehin erfordern.
- Solange der Eintrag drin steht, rendert JEDE Fleet-Datei sparky auf
  ACP — ein "nur mal kurz native" braucht den manuellen service-level
  Override (der genau dafuer ueberlebt).

## Verwandt

- ADR-045 (omp headless harness), ADR-080 (Heartbeat-Steuerkanal/
  Interrupt-Ladder — funktioniert treiberunabhaengig).
- #464/#471/#473: ACP-Treiber, Nacharbeit, Vorschau-Kanal.
