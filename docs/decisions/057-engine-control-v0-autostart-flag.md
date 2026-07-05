# ADR-057 — Engine Control v0: Autostart-Flag via SSH

**Status:** Accepted
**Datum:** 2026-07-05
**Scope:** Backend/Runtime | Backend/DB | Frontend/Runtimes | Infra/Runtime

## Kontext

Runtime Management v1 (ADR-054) macht MC zum reinen Beobachter der Inference-
Engine ("Engine führt, MC folgt"): der Watcher probt das aktive Modell alle
90s, aber MC kann selbst nichts am Boot-/Lifecycle-Verhalten der Engine
steuern. Einzige Ausnahme bislang: Wake-on-LAN für `power_managed`-Hosts
(ADR-042), das nur die Box weckt, nicht die Engine selbst konfiguriert.

Ein konkreter Bedarf: auf dem DGX Spark entscheidet ein systemd-Unit beim
Boot anhand einer Flag-Datei (`~/scripts/vllm-autostart.enabled`), ob vLLM
automatisch hochfährt oder nicht — z.B. um nach einem Neustart bewusst
manuell zu starten (Recipe-Wechsel, Debugging) statt dass der alte Stand
sofort wieder hochkommt. Bisher war das nur per manuellem SSH steuerbar,
ohne Sichtbarkeit im Cockpit.

Der Host-Registry-Umbau (ADR-048) hat bereits eine generische `hosts`-Tabelle
mit SSH-Zugangsdaten (`ssh_host`/`ssh_user`/`ssh_key_path`) + Resolver
(`host_resolver.resolve_host_for_runtime()`) etabliert — jede Runtime kennt
über `host_id` bereits ihren Host, ohne dass Zugangsdaten dupliziert werden
müssten.

## Entscheidung

Zwei neue Spalten auf `runtimes` (Migration `0146_runtime_autostart`):
`autostart_supported` (bool, default false) und `autostart_flag_path`
(nullable string) — der absolute Pfad der Flag-Datei auf dem gebundenen Host.
Beide werden vom Operator zur Laufzeit gesetzt (`PATCH /runtimes/db/{slug}`
oder die UI), nie geseeded — der Public-Repo-Grundsatz "keine echten Hosts/
Pfade im Code" bleibt gewahrt.

Neuer Service `services/runtime_autostart.py` führt `test -f` (Status) bzw.
`touch`/`rm -f` (Toggle) über den bestehenden `runtime_manager._ssh_run()`
aus — **keine zweite SSH-Implementierung**, sondern derselbe asyncssh-Pfad
wie jeder andere Lifecycle-Befehl (docker start/stop, lms load, tmux). Der
Host kommt über den bestehenden Resolver (`host_id` → `hosts`-Registry), es
gibt kein zusätzliches `engine_control`-JSON mit eigenem host/user — das
wäre eine zweite, konkurrierende Quelle für Zugangsdaten gewesen.

API: `GET /api/v1/runtimes/db/{slug}/autostart` (on-demand Live-Probe, nicht
Teil des 90s-Watcher-Takts — SSH bei jedem Tick fleet-weit wäre unnötige
Last) und `POST .../autostart {"enabled": bool}` (touch/rm + Rücklese-
Verifikation + `runtime.autostart_changed` Activity-Event). Ein
unerreichbarer Host liefert `enabled: null, reachable: false` bzw. bei POST
einen 502 mit klarer deutscher Fehlermeldung — nie einen Stacktrace.

Frontend: `AutostartToggle`-Komponente auf der `/runtimes`-Karte (nur wenn
`autostart_supported=true`), 3 Zustände (an/aus/unbekannt), disabled bei
unbekanntem Host, kein optimistisches UI — der Toggle zeigt erst nach
Backend-Verifikation den neuen Zustand.

## Alternativen

- **Neues `engine_control`-JSON-Feld mit eigenem host/user/flag_path**
  (ursprünglicher Auftrag): hätte die Host-Registry (ADR-048) dupliziert —
  zwei Quellen für "wo läuft das", eine über `host_id`, eine embedded im
  JSON. → Verworfen zugunsten der bestehenden Registry; nur `flag_path`
  bleibt runtime-spezifisch (der Host selbst ist längst modelliert).
- **Periodisches Polling im Runtime Watcher** (analog Modell-Probing,
  ADR-054): hätte SSH-Last auf jeden Tick draufgesetzt, auch wenn niemand
  die Seite ansieht. → Verworfen (D-22-Präzedenzfall: periodisches Probing
  bewusst vermieden wo On-Demand reicht).
- **Eigene SSH-Bibliothek/-Verbindung für Engine-Control**: hätte die
  Timeout-/Fehlerbehandlungs-Semantik von `_ssh_run` dupliziert. → Verworfen,
  `_ssh_run` wiederverwendet.

## Konsequenzen

### Positiv
- Erster konkreter Baustein von Cockpit v2 (Engine-Steuerung statt nur
  Beobachtung) — reine Additive-Migration, kein bestehendes Verhalten
  betroffen (`autostart_supported` default false).
- Keine neue Zugangsdaten-Quelle: Host-Auflösung, SSH-Timeout- und
  Fehlersemantik sind 1:1 die der bestehenden Lifecycle-Ops.
- Ehrlicher UI-Zustand: "unbekannt" statt eines falschen "aus" bei totem
  Host.

### Negativ
- Der Live-Status ist ein separater On-Demand-Call (kein Redis-Cache wie der
  90s-Watcher) — bei sehr vielen gleichzeitigen `/runtimes`-Betrachtern
  entstehen mehrere parallele SSH-Verbindungen pro Karte. Aktuell kein
  Problem (Single-Operator-Deployment), müsste bei Multi-User-Skalierung
  gecacht werden.
- `autostart_flag_path` ist auf Zeichensatz + absoluten Pfad beschränkt
  (Regex) und wird zusätzlich `shlex.quote`-escaped — wer eine Datei mit
  exotischeren Zeichen im Pfad autostart-steuern will, muss sie umbenennen.
- Cockpit v2 (volle Engine-Steuerung: Start/Stop-Policies, Recipe-Presets)
  bleibt bewusst geparkt — dieser ADR deckt nur den ersten, engen Fall.

## Referenzen

- Betroffene Dateien: `backend/app/models/runtime.py`,
  `backend/app/services/runtime_autostart.py`,
  `backend/app/routers/runtimes.py`,
  `backend/alembic/versions/0146_runtime_autostart.py`,
  `frontend-v2/src/app/runtimes/AutostartToggle.tsx`,
  `frontend-v2/src/lib/types.ts`, `frontend-v2/src/lib/api.ts`
- Verwandte ADRs: ADR-048 (Host-Registry — Quelle der SSH-Zugangsdaten),
  ADR-054 (Runtime Watcher — On-Demand-vs-Takt-Präzedenzfall),
  ADR-042 (flask_wol/Wake-on-LAN — der bisher einzige Engine-Control-Fall)
