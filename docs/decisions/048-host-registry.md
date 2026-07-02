# ADR-048 — Host-Registry statt neuer runtime_type pro Box

**Status:** Accepted
**Datum:** 2026-07-02
**Scope:** Backend/Runtime · Backend/DB · Frontend/Runtimes · Infra/Runtime

## Kontext

MC ist seit v0.1.0 **public** (github.com/argyelan-ai/mission-control) — OSS-User haben
*andere* GPU-Boxen als wir (oder gar keine). Die Control-Plane nahm aber überall
„genau eine GPU-Box" an:

1. **`_ssh_run` hart auf `settings.dgx_ssh_host`** — jede SSH-Lifecycle-Operation
   (docker start/stop, lms load/unload, tmux, nvidia-smi, Eviction) landete implizit
   auf dem DGX Spark. `get_spark_metrics()` kannte genau einen Spark, die IP `.154`
   stand in ~40 Dateien.
2. **Jede neue Box = neuer `runtime_type` + Copy-Paste-Control-Code.** PORSCHE bekam
   `unsloth_porsche` (ADR-042), Hermes einen eigenen Typ (ADR-029), omp ebenso
   (ADR-045). Das Muster skaliert nicht auf N Boxen: der *Lifecycle-Mechanismus*
   (SSH vs. Flask-WoL vs. lokal) wurde mit dem *Host* verwechselt.

Gleichzeitig muss der Kern-Flow für OSS-User intakt bleiben: **Fresh-Install ohne
GPU-Host** (nur Anthropic-Cloud-Runtimes) muss vollständig funktionieren — 0 Hosts
ist kein Fehlerzustand.

Design-Spec: `docs/plans/2026-07-02-host-registry-design.md` (approved, Option B).

## Entscheidung

Eine **generische Host-Registry**: neue Tabelle `hosts` (Migration `0133_host_registry`,
Model `backend/app/models/host.py`) mit `kind` = `ssh` | `flask_wol` | `local` plus
Verbindungsdaten (`ssh_host`/`ssh_user`/`ssh_key_path`, `control_url`,
`wol_mac_address`, `power_managed`). `runtimes` bekommt `host_id` (FK, nullable,
`ondelete=SET NULL`).

Die Auflösung läuft über `backend/app/services/host_resolver.py`:

```
resolve_host_for_runtime(session, runtime) -> ResolvedHost | None
  1. runtime.host_id        → Host-Row (disabled → Warnung, kein Silent-Fallback)
  2. runtime.host (Legacy)  → ad-hoc ResolvedHost + settings.dgx_ssh_*
  3. settings.dgx_ssh_host  → ad-hoc ResolvedHost (heutiges Verhalten)
  4. sonst                  → None (Lifecycle-Ops: klarer Fehler; HTTP-only-Probes laufen weiter)
```

`runtime_manager` arbeitet nur noch mit `ResolvedHost` — nie mehr direkt mit
`settings.dgx_ssh_*`. `_ssh_run` bekommt den Host als Parameter, `get_spark_metrics()`
wird zu `get_host_metrics(host)`, Eviction ist host-scoped. Bootstrap-Seed im Lifespan
(analog Runtime-Seed ADR-028) seeded `dgx-spark`/`porsche` idempotent aus Env/Bestand
und verlinkt bestehende Runtimes per endpoint-IP. API: `routers/hosts.py`
(CRUD admin-only, Metrics, Delete-Guard bei gebundenen Runtimes) +
Back-Compat-Alias `GET /runtimes/spark/metrics`.

**ADR-042 bleibt gültig:** die `flask_wol`-Mechanik (Flask `:5555`, Wake-on-LAN,
Readiness-Gate) ist unverändert — sie wird nur vom Runtime-Type auf den Host
verschoben. Ersetzt wird ausschliesslich das Muster „neue Box = neuer runtime_type".

## Alternativen

- **Quick-Fix only** (`_ssh_run` bekommt nur einen optionalen Host-Parameter, keine
  Tabelle): Beschreibung — minimale Entkopplung ohne Datenmodell. → Verworfen weil
  die Host-Daten dann weiter über Settings/Runtime-Felder/Hardcodes verstreut blieben;
  OSS-User könnten keine eigenen Boxen deklarieren, und die nächste Box hätte wieder
  Copy-Paste-Control-Code erzeugt.
- **Voll-Fleet + Scheduler** (Agent-Container-Placement auf Remote-Hosts,
  VRAM-Scheduler, Kubernetes-artiges): Beschreibung — Hosts nicht nur als
  LLM-Control-Plane, sondern als generelle Compute-Fleet. → Verworfen weil **YAGNI**:
  kein aktueller Bedarf, grosser Komplexitätssprung. Bewusst als Welle 3 geparkt;
  Agent-Container bleiben auf dem MC-Host.

## Konsequenzen

### Positiv
- Neue Box = **eine DB-Row** statt neuer runtime_type + Copy-Paste-Lifecycle-Branch.
- OSS-tauglich: Fresh-Install ohne GPU-Host läuft mit 0 Hosts fehlerfrei
  (Cloud-Runtimes brauchen keinen Host); eigene Boxen via API/UI deklarierbar.
- `runtime_manager` ist von `settings.dgx_ssh_*` entkoppelt; Metrics + Eviction
  generisch pro Host statt hart auf den Spark.
- Back-Compat-Kette: bestehende Installationen verhalten sich byte-identisch,
  solange nur der Settings-Fallback greift.

### Negativ
- Legacy-Runtime-Felder (`host`, `control_url`, `wol_mac_address`, `power_managed`)
  bleiben als **deprecated** Fallback erhalten — zwei Wahrheitsquellen bis zur
  Bereinigung, Resolver-Kette muss dokumentiert bleiben.
- Vier-stufige Resolution ist mehr Indirektion als der alte Hardcode; Fehlkonfiguration
  (z.B. disabled Host) muss über Warnungen sichtbar bleiben.
- Welle 3 (Placement/Scheduler) ist bewusst geparkt — wer sie braucht, muss die
  Registry dann erweitern statt hier vorgebaute Komplexität vorzufinden.

## Referenzen

- Betroffene Dateien: `backend/app/models/host.py`, `backend/app/services/host_resolver.py`,
  `backend/app/services/runtime_manager.py`, `backend/app/routers/hosts.py`,
  `backend/alembic/versions/0133_host_registry.py`, `frontend-v2/src/lib/types.ts`,
  `frontend-v2/src/lib/api.ts` (Hosts-Sektion `/runtimes`-Seite)
- Design-Spec: `docs/plans/2026-07-02-host-registry-design.md`
- Verwandte ADRs: ADR-042 (bleibt gültig — flask_wol-Mechanik), ADR-028 (Seed-Muster),
  ADR-029, ADR-045 (Beispiele des abgelösten Musters), ADR-017 (Registry in DB)
