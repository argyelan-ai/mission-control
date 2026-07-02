# ADR-040 — Portable File Access (HTTP streaming primary, native open optional)

**Status:** Accepted
**Datum:** 2026-06-18
**Scope:** Backend/Files · Backend/DB · Frontend/Pages · Infra/Reusability

## Kontext

MC hatte keine globale Files-Ansicht. Dateien existierten nur als task-gebundene *Deliverables* im Deliverables-Tab einer Task. Drei Probleme trieben die Entscheidung:

1. **Mobile-Bug:** Der „Ordner öffnen"-Knopf rief eine Backend-Aktion, die `open -R <pfad>` **auf dem Host-Rechner** ausführte (`tasks.py:open_deliverable` → Host-Helper `:8765`). Server-seitiges `open` ist per Definition handy-blind: es agiert auf dem Schreibtisch-Finder des Operators, nie auf dem Handy, und schluckte Fehler still.
2. **Keine Übersicht:** Der Operator wollte die realen Ordnerstrukturen + Dateien über ganz MC browsen (Deliverables, Workspaces, Vault, Screenshots, „alles").
3. **Nicht reusable:** Die Datei-Schicht war an die Maschine des Operators genagelt — macOS-only `open`, `HOME_HOST` mit Literal-Default `/Users/YOUR_USER`, hartkodierte Tailscale-IP `100.x.x.x` im Produktcode, doppelt gepflegte Pfad-Resolver, ein any-absolute-path-Read-Exposure-Fallback, und `~/.mc/secrets` direkt neben browsebaren Ordnern.

Zusätzliche Einschränkung: Die Flotte ist **multi-runtime** (`agent.agent_runtime ∈ {cli-bridge, host, claude-code, manual}`, ADR-029). Nur cli-bridge braucht Container↔Host-Pfad-Rewrite (`dispatch.py`); Host-Worker schreiben Deliverables ohne Slug-Subfolder. Diese Asymmetrie war über drei Stellen verstreut + der Slug wurde bei jedem Read aus `agent.name` neu berechnet (bricht bei Rename).

## Entscheidung

**Datei-Bytes streamen immer portabel über HTTP** (`fs_service.read_stream` → `FileResponse`), und das ist der primäre Zugriff (handy-korrekt). **Native macOS „Im Finder öffnen" ist ein optionaler, capability-detected Bonus** — pro Eintrag nur verfügbar, wenn ein echter Host-Pfad existiert UND der Helper (`:8765`) erreichbar ist; sonst still versteckt statt zu scheitern.

Strukturell:
- **`fs_roots`** — eine Registry der browsebaren `~/.mc`-Subtrees (Single Source of Truth), die das verstreute `HOME_HOST + "/.mc/..."`-String-Bauen + die doppelte Write/Read-Prefix-Liste ersetzt. `secrets`, token-tragende `agents/*/claude-config`, `browser-profiles`, `logs`, `backups` sind **hart ausgeschlossen** (nie als Root registriert).
- **`fs_service`** — der EINE sandboxed Zugriff mit **einem** Containment-Guard (`safe_join`: realpath muss im Root bleiben, kein `..`/Symlink-Escape/NUL). Der runtime-aware `resolve_deliverable` konsolidiert die zwei Resolver-Kopien (`deliverable_fs_resolver` + `tasks.py`-Inline) und droppt die `.mc-deliverables`-Hyphen-Landmine.
- **`file_index`** — ein DB-Beschleuniger für Listing/Suche (capture-at-write beim Deliverable-Registrieren + periodischer Background-Walk). **Nur Accelerator** — Bytes kommen nie aus dem Index, nur Listings; Inhalte sind also nie stale.
- **stabile `agents.slug`-Spalte** (before_insert, rename-fest) statt Recompute aus `agent.name`.
- **`/api/v1/files`-Router** + `api.files`-Frontend-Namespace + globale `/files`-Seite.
- Portabilität: `HOME_HOST → settings.home_host` (Default `Path.home()`, fail-loud-Warnung), `PUBLIC_HOST`/`EXTRA_CORS_ORIGINS` statt Literal-IPs.

## Alternativen

- **Thin (HTTP-only, Deliverables-aggregiert, native open ganz raus):** Maximal portabel, kleinste Fläche. → Verworfen weil der Operator den Finder-Reveal auf seinem eigenen Mac behalten wollte UND ein echter FS-Browser über die ~8 Wurzeln (nicht nur Deliverables) gefordert war.
- **Physische Layout-Normalisierung jetzt** (Host-Worker physisch nach `~/.mc/deliverables/<slug>/<task_id>/` schreiben lassen): Würde die On-Disk-Asymmetrie dauerhaft beseitigen. → **Aufgeschoben** weil es den High-Risk-Dispatch-Pfad (`dispatch_message_builder`), das `mc deliverable` CLI und die task-scoped Pfad-Validierung (Security) berührt + eine Datei-Migration bestehender Deliverables bräuchte. Der konsolidierte Resolver behandelt **beide** Layouts bereits uniform + getestet — der eigentliche Bug ist damit weg; die physische Relokation ist ein separater, fokussierter Task mit Dry-Run.

## Konsequenzen

### Positiv
- Mobile-korrekt by default: In-Browser-Preview + Download funktionieren ohne jede macOS-Abhängigkeit (Handy, Linux, headless, multi-user).
- Native Finder-Reveal bleibt als gracefuller macOS-Bonus erhalten (capability-detected, versteckt statt still scheitert).
- Reusability-Lecks beseitigt: eine FS-SSoT + ein Containment-Guard, kein Literal-`/Users/YOUR_USER`, keine hartkodierte Tailscale-IP, `secrets` nie browsebar.
- Rename-feste Deliverable-Pfade (stabiler Slug); ein Resolver statt drei Kopien.

### Negativ
- Ein generalisierter FS-Browser ist eine echte Security-Fläche — der Root-Allowlist + Containment-Guard muss rigoros bleiben (der any-absolute-path-Fallback wurde aus dem Browse-Pfad entfernt; im Legacy-Deliverable-Resolver bleibt er, da dort write-validierte DB-Pfade bedient werden).
- Der `file_index` kann zwischen zwei Walks kurz von der Disk abweichen (nur Listings; Bytes nie). Versöhnt durch capture-at-write + Re-Walk.
- CORS/Phone-Links brauchen jetzt `PUBLIC_HOST` in `.env` — der bestehende Zugriff des Operators bricht, bis `PUBLIC_HOST=<tailscale-ip>` gesetzt ist (bewusster Trade für Portabilität).
- Die On-Disk-Deliverable-Asymmetrie (Slug vs. kein-Slug) bleibt physisch bestehen (vom Resolver überbrückt) bis zur separaten Migration.

## Referenzen

- Betroffene Dateien: `backend/app/services/fs_roots.py`, `backend/app/services/fs_service.py`, `backend/app/services/file_indexer.py`, `backend/app/models/file_index.py`, `backend/app/models/agent.py` (slug), `backend/app/routers/files.py`, `backend/app/routers/tasks.py` (resolver delegate), `backend/app/config.py` (home_host/public_host), `frontend-v2/src/app/files/page.tsx`, `frontend-v2/src/lib/api.ts` (api.files)
- Supersedes (teilweise): ADR-022 (mc-home-workspace-layout) — dessen Single-macOS-Host-Annahme für Datei-Zugriff (Finder-Reveal als einziger Weg) wird durch HTTP-streaming-primary ersetzt. ADR-022 bleibt gültig für das ~/.mc-Layout selbst.
- Verwandte ADRs: ADR-029/031 (Hermes host-worker + deliverable dual-path), ADR-033 (secrets-Boundary — secrets nie browsebar), ADR-034 (vault-as-source-of-truth)
- Spec: `docs/superpowers/specs/2026-06-18-mc-files-system-design.md` · Plan: `docs/superpowers/plans/2026-06-18-mc-files-system.md`
