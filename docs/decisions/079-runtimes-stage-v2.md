# ADR-079 — Runtimes-Seite v2 „Die Bühne" (Fleet-Tab hinter Schalter)

**Status:** Accepted
**Datum:** 2026-09-06
**Scope:** Frontend/State

## Kontext

Die heutige Fleet-Ansicht (`SlotStage.tsx`) zeigt eine Kachel je **Box** (Host-Slot).
Läuft ein Modell als Duo über zwei Boxen, sieht der Operator zwei fast identische
Kacheln (Head zeigt das volle Modell, Worker eine abgespeckte Kopie) statt EINES
zusammenhängenden Bildes „ein Modell läuft über zwei Boxen". Mark hat dazu am
06.09.2026 die Spezifikation `docs/specs/runtimes-buehne-v2.md` abgenommen (Mockup:
`runtimes-a.tpl.html`/`build-a.py`): ein laufendes Modell wird zu **einer Karte**
(„Bühne") mit den beteiligten Boxen als **Mitgliedern**, statt einer Kachel je Box.

Der Umbau ist gross genug (neue Gruppierungslogik auf Modell- statt Box-Ebene, neues
Lauflicht/Wärmestreifen-Vokabular, neue Datenquelle Puls/`GET /hosts/{id}/pulse`) um
ihn hinter einem Schalter zu bauen, statt `SlotStage` in-place zu ersetzen — Mark
braucht eine Klick-Abnahme (Desktop + Handy), bevor v2 zum Standard wird, und drei
Backend-PRs (Puls, Stop↔Autostart, Telemetrie-Verlauf) laufen parallel zu diesem
Frontend-PR.

## Entscheidung

Neue Fleet-Ansicht (`frontend-v2/src/app/runtimes/stage/`) hinter
`NEXT_PUBLIC_RUNTIMES_STAGE` — `v1` (Standard, `SlotStage`) oder `v2` (`FleetStage`).
`page.tsx`'s Fleet-Tab verzweigt auf den Flag; kein anderer Tab ändert sich.

- **`FleetStage.tsx`** gruppiert `stageGroups` auf Modell-Ebene: `buildStages()`
  (reine Funktion, kein Hook) nimmt pro Host-Gruppe die von `pickServing()`
  gewählte laufende Runtime und fasst ihren Host plus `member_hosts` zu EINER
  Bühne zusammen. Boxen ohne laufendes Modell werden `FreeBox`, boxen aus
  `sleepingGroups` werden `AsleepBox`. Läuft nichts und ist mindestens eine Box
  frei (keine schlafende dabei), zeigt die Seite den Leer-Zustand.
- **`Stage.tsx`** rendert die vier Zonen aus der Spec (Lauflicht `FlowEdge`,
  Lebenszeichen + Wärmestreifen `HeatStrip`, Instrumente `KpiRow`, Mitglieder
  `MemberRowContainer`, Aktionen `ActionBar`/`PhaseBar` je nach Zustand).
- **Puls** (`GET /hosts/{id}/pulse`, PR 1, Backend parallel in Arbeit): der
  API-Client (`api.hosts.pulse`) fängt jeden Fehler (inkl. 404, solange der
  Endpoint nicht deployt ist) ab und liefert `available:false` statt eines
  Rejects — die Bühne zeigt dann leere Zellen, nie ein Fehlerbanner.
- **Stop↔Autostart-Dispatch-Gate** (PR 2, Backend parallel in Arbeit): `ActionBar`
  ruft `api.runtimes.stop(id, {force})`; ein 409 mit `{agent, task}` im Body zeigt
  eine inline Bestätigungszeile („Stop anyway") statt `window.confirm`, ein Klick
  wiederholt mit `force:true`. Die genaue Form des 409-Bodys ist eine Annahme aus
  der Spec, solange der Backend-PR nicht gemergt ist.
- **HONESTY RULE** (aus `SlotStage.tsx` übernommen): `Runtime` trägt keine
  Startzeit — die Karte zeigt darum keine erfundene Laufzeit („up 2 h 41" bleibt
  Mockup-Referenz, keine Feld-Vorgabe).
- Bestehende Bauteile bleiben unverändert nutzbar: `Meter`-Ersatz mit
  `transform: scaleX` (neu in `MemberRow.tsx`, keine Layout-Eigenschaft
  animiert), `HostRecipeSwitcher` (Switch-model-Auslöser), `PhaseIndicator`
  (jetzt aus `SlotStage.tsx` exportiert, von `PhaseBar.tsx` wiederverwendet),
  `DeviceModeStrip` (kompakter Modus-Vierer, unverändert wiederverwendet),
  `grouping.ts` (`pickServing`/`pickSlot`, unverändert).

### Cockpit (PR 5, `stage/cockpit/`)

Das Zahnrad auf `ActionBar`/`FreeBox`/`AsleepBox` öffnet seit PR 5 `BoxCockpit`
statt des alten `RuntimeDetailPanel` (das bleibt nur noch für Cloud/Unassigned-
Runtimes in `page.tsx`'s Register-Tab). `FleetStage.tsx` hält den Cockpit-
Zustand selbst (`{members, runtime, activeHostId}`) statt ihn nach `page.tsx`
zu heben — die Bühne kennt bereits alle Mitglieder einer Karte, ein zweiter
Lookup dort wäre doppelte Arbeit.

- **Drawer/Sheet-Mechanik**: `SlideOverPanel` bekam einen neuen `hideHeader`-
  Schalter (backward-kompatibel, Default `false`) — das Cockpit braucht einen
  eigenen Kopf (Boxname + Mono-Fakten + Box-Umschalter bei Duo) statt des
  generischen Panel-Titels, aber will Backdrop/Bottom-Sheet/Esc/Motion nicht
  zweimal bauen.
- **Fünf Gruppen** (`TelemetryChart`, `ModeList`, `AutostartGroup`,
  `ConnectionGroup`, `RecipeGroup`) in der Anatomie [Label 92px | Inhalt]
  (`.cockpit-grp`, Container-Query wie `.stage-kpi`).
- **`ModeList`** teilt sich `useDeviceModeControl` (jetzt aus
  `DeviceControl.tsx` exportiert) mit dem Vierer auf der Karte — ein
  Mutations-Pfad, damit Karte und Cockpit nie auseinanderlaufen können.
- **Telemetrie-Verlauf** (`GET /hosts/{id}/metrics/history`, PR 3): der
  Backend-PR ist zum Zeitpunkt von PR 5 noch nicht gemergt.
  `api.hosts.metricsHistory()` fängt jeden Fehler (inkl. 404) ab und liefert
  `{points:[]}` — derselbe Honesty-Fallback wie `api.hosts.pulse()`; das
  Diagramm zeigt dann "collecting…" statt eines Fehlers.
- **HONESTY-Lücken, bewusst offen gelassen** statt erfundener Felder:
  Autostart zeigt die letzten Start-**Ereignisse** nicht als Zeitleiste
  (`ActivityEvent` trägt kein `host_id` — keine Korrelation ohne Raten
  möglich), sondern fällt auf `HostAutostartStatus.last_attempt_at`/
  `last_result` zurück (eine Zeile). Der Log-Pfad aus dem Mockup entfällt
  ganz (kein Backend-Feld trägt ihn). Die Fan-Prozentzahl in Mode/Telemetry
  entfällt ebenso (`DeviceState` trägt keine).

## Alternativen

- **`SlotStage.tsx` in-place umbauen:** verworfen — kein Rückweg ohne Redeploy,
  und drei Backend-Verträge (Puls/Stop-Gate/Telemetrie-Verlauf) sind zum
  Zeitpunkt dieses PRs noch nicht gemergt. Ein Schalter erlaubt, das Frontend
  vor den Backend-PRs zu liefern und erst nach Marks Klick-Abnahme umzuschalten.
- **Grouping auf Box-Ebene belassen, nur die Optik ändern:** verworfen — die
  Spec verlangt explizit „ein Modell = eine Karte", das ist eine
  Daten-/Gruppierungs-Änderung, keine reine CSS-Änderung.

## Konsequenzen

### Positiv
- Ein Duo-Modell ist als EIN zusammenhängendes Bild lesbar statt zwei Kacheln.
- Rückweg ist ein Flag-Flip (Rebuild), kein Rollback von Datenmodell-Änderungen —
  `FleetStage`/`Stage`/… fassen nichts an, was `SlotStage` liest oder schreibt.
- Puls-Endpoint-Abwesenheit (Backend-PR 1 noch nicht deployt) bricht die Seite
  nicht — geprüft über `api.hosts.pulse`'s eigenen Catch-all.

### Negativ
- Zwei parallele Fleet-Implementierungen (`SlotStage` + `stage/`) bis Welle 6
  (Schliff) `SlotStage` entfernt — doppelte Wartung für die Übergangszeit.
- Der Stop-Dispatch-Gate-409-Vertrag ist eine Annahme; ändert der gemergte
  Backend-PR die Form (z.B. `detail` als String statt `{agent,task}`-Objekt),
  muss `ActionBar.tsx`'s `parseStopConflict()` nachgezogen werden.
- „Speed solo" (Zone 2) hat noch keine Datenquelle (Katalog-/Bench-Feld fehlt im
  Frontend-Vertrag) — die Karte zeigt „–", bis das nachkommt.

## Referenzen

- Spec: `docs/specs/runtimes-buehne-v2.md` (Worktree `buehne-spec`)
- Betroffene Dateien: `frontend-v2/src/app/runtimes/stage/*`,
  `frontend-v2/src/app/runtimes/page.tsx` (Schalter + Fleet-Tab-Zweig),
  `frontend-v2/src/app/runtimes/SlotStage.tsx` (nur `PhaseIndicator` exportiert),
  `frontend-v2/src/lib/api.ts` (`hosts.pulse`, `runtimes.stop({force})`),
  `frontend-v2/src/lib/types.ts` (`HostPulse`, `RuntimeStopConflict`,
  `RuntimeActionResult.autostart_disabled`)
- Verwandte ADRs: ADR-076 (Round-Shape-Sprache), ADR-078 (Slot-Runtime/Box-URL)
