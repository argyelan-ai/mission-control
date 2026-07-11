# ADR-069 — Benchmark Studio als Vertical + Kern-Bausteine

**Status:** Accepted
**Datum:** 2026-07-11
**Scope:** Backend/Verticals · Backend/DB · Backend/Content · Frontend/Verticals

**Numbering note:** hoechstes ADR bei Branch-Zeit war 065. ADR-066 (grok harness) und
ADR-068 (grok bridge v2) wurden zwischenzeitlich auf Main gemergt; ADR-067 blieb frei
(Luecke). Diese Entscheidung traegt daher 069 statt der im Plan urspruenglich
geschaetzten 066. Bei Merge-Kollision mit einem weiteren parallelen Branch umnummerieren
— Vorbild ADR-064/065.

## Kontext

Mark will auf X visuelle One-Shot-Capability-Demos posten: lokale Spark-Modelle gegen
Frontier-Modelle (Claude, Grok), gleicher Prompt, Video-Grid. Der einzigartige Winkel:
kein Speed-Benchmark, sondern aesthetische Qualitaets-Demos — HTML-Animationen, 3D,
Mini-Games, Websites. MC orchestriert die gesamte Kette:

```
Prompt → Modelle generieren index.html
       → mc-playwright /record (HTML → Video)
       → mc-playwright /compose (N Videos → Grid mit Labels)
       → Studio-Review (Operator begutachtet das Grid)
       → Draft → Inbox-Freigabe (Approval)
       → Post via x_publisher.post_media (X API v2)
```

Design-Dokument: `docs/superpowers/specs/2026-07-11-benchmark-studio-design.md`
(von Mark abgesegnet, Interview 11.07.2026).

Drei Architektur-Spannungen mussten aufgeloest werden:

1. **Wohin mit der Orchestrierung?** Challenge-Produktion ist Marks Content-Business,
   nicht generische Infrastruktur. Praezedenz: `news_studio` Vertical (ADR-044).
2. **Wie erfaehrt das Vertical vom Post-Ausgang?** Der Publish-Teil laeuft bewusst ueber
   Approval + ContentPipeline (ADR-065, "kein zweiter Lifecycle") — aber der Kern darf
   ein Vertical nie importieren (ADR-044, Kopplungsrichtung).
3. **Zwei Generierungspfade (Spark-Direkt vs. Fleet-Dispatch) ohne neuen Dispatch-Typ
   einzufuehren.**

## Entscheidung

**5 Bausteine ueber 3 PRs (feat/x-publisher-v2 → feat/prompt-library-inbox →
feat/bench-studio):**

### Baustein 1 — Publisher-Media (Kern, PR 1)

`services/x_publisher.post_media()` (tweepy Media-Upload, erweitert `post_text()`) nimmt
`media_paths: list[str]`; Upload via `tweepy.MediaUpload`, `tweet()` mit `media_ids`.
Laeuft ueber dieselbe Approval-Kette wie ADR-065 — Payload waechst um `media_paths[]`,
kein Schema-Umbau.

### Baustein 2 — Record + Compose (Kern, PR 1, mc-playwright)

Zwei neue Endpunkte im `docker/mc-playwright`-Service:
- `POST /record` — nimmt eine lokale HTML-Datei oder URL, rendert sie headless,
  nimmt ein Video auf (webm); gibt `video_path` zurueck.
- `POST /compose` — nimmt N Video-Pfade + Labels, komponiert ein Grid-Video via ffmpeg.

ffmpeg wird ins mc-playwright-Image installiert. Mount `mc_shared_deliverables` wurde
von `ro` auf `rw` geaendert — der Orchestrator schreibt
`/shared-deliverables/bench-<id>/<label>/index.html`, mc-playwright liest und schreibt
in dasselbe Volume. Praezedenz: mcp-screenshots-Mount ro→rw (05.07.).

### Baustein 3 — Prompt Library (Kern, PR 2)

Tabelle `prompt_templates` (Migration 0152): `id`/`title`/`body`/`tags`/`created_at`/
`updated_at`. Generisch — nicht an Challenges gebunden. CRUD-API unter
`/api/v1/prompt-templates` + Inbox-Preview-Anreicherung fuer `x_post`-Approvals
(Challenge-Info + Media-Preview).

### Baustein 4 — Vertical `bench_studio` (PR 3)

`backend/app/verticals/bench_studio/` + `frontend-v2/src/verticals/bench_studio/` +
Flag `benchStudio` in `frontend-v2/src/lib/verticals.ts` (Default `true`). Strippbar:
Verzeichnisse loeschen + Flag aus = App bootet und baut unveraendert.

**Tabellen im Kern** (ADR-044 §3): `bench_challenges` + `bench_entries` (Migration 0153,
eine Kette). `bench_entries.task_id` mit `ondelete=SET NULL` — Bench-Historie ueberlebt
Task-Loeschung (mc-task-delete-guard). `prompt_text` ist eine **eingefrorene Kopie** —
Template bleibt editierbar, ohne Historie zu verfaelschen; `prompt_template_id` bleibt
als Provenienz (Verwendungs-Historie im Studio).

**Zustands-Maschine** (`bench_challenges.status`):
```
generating → rendering → composing → review → drafted → published
                                        ↓         ↓
                                      failed    (re-draft moeglich)
```
Entry-Status: `pending → generating → generated → rendered | failed`.

**Orchestrator** (`orchestrator.py`): steuert die Kette, schreibt bei jedem Schritt
Status + Fehler in die DB. Teilfehler blockieren nicht: Entry `failed` → Grid aus den
Ueberlebenden; alles `failed` → Challenge `failed`.

**Zwei Generierungspfade, kein neuer Dispatch-Typ:**
- **Spark:** Direkt-Call `spark_client` (OpenAI-kompatibel, `/chat/completions`),
  HTML-Antwort nach `/shared-deliverables/bench-<id>/<label>/index.html`, Usage-Metriken
  (`duration_ms`, `tokens_in`, `tokens_out`, `tok_per_s`) nach `bench_entries.metrics`.
- **Claude/Grok:** normaler Fleet-Task via `auto_dispatch_task` mit striktem One-Shot-
  Brief ("liefere genau eine `index.html` als Deliverable"), Artefakt-Einsammlung ueber
  den `task_done`-Hook.

**Router** `/api/v1/bench/*` (operator-JWT): fuenf Endpunkte (create, list, get, start,
rerender) + Draft-Endpunkt.

**Frontend Studio-Seite** `/bench` mit zwei Tabs:
- *Challenges* — Gallery (Status-Chips, Progress-Polling alle 5 s), Challenge-Detail
  (Entry-Videos/Screenshots, Metriken-Toggle, Draft-Dialog mit Zeichenzaehler).
- *Prompt Library* — Suche/Tags, Editor, Start-Challenge-Button, Verwendungs-Badge.

### Baustein 5 — Inbox-Preview (Kern, PR 2 + PR 3)

`ApprovalCard.tsx` erkennt `action_type="x_post"` und zeigt Tweet-Text, Zeichenzaehler,
abspielbare Videos/Bilder — kein separates Freigabe-UI noetig.

### Architektur-Entscheidung: Hook-Registry statt Kern→Vertical-Import

Damit der Kern den Challenge-Status flippen kann, ohne das Vertical zu importieren
(ADR-044-Kopplungsrichtung), einfuehren wir **`x_post_resolved_hooks`** in
`backend/app/verticals/hooks.py` (Kern, wird nie gestrippt). Das Vertical registriert
beim App-Boot einen Callback; `_handle_x_post_resolution()` in `routers/approvals.py`
ruft die Registry nach jedem Approve/Reject auf und reicht `result_dict | None`
durch. Vorher gab es nur `task_done_hooks` + `tools_md_sections`.

### Architektur-Entscheidung: `task_done_hooks` entgated

Beide Call-Sites (`routers/tasks.py`, `routers/agent_task_status.py`) liefen nur wenn
`task.pipeline_id` gesetzt war — Bench-Agent-Tasks haben keine Pipeline. Gate entfernt;
Hooks self-filtern (Registry loggt + schluckt Fehler).

### Architektur-Entscheidung: Draft-Idempotenz + Pipeline-Reuse

`POST /bench/{id}/draft` ist idempotent: identischer pending Draft liefert dieselbe
`approval_id` (409-Guard). Existiert eine abgelaufene/abgelehnte Approval, wird eine
neue ContentPipeline angelegt und referenziert; ist eine aktive Pipeline vorhanden,
wird sie wiederverwendet.

### Architektur-Entscheidung: Edited-Text-wins bei Templates

Beim Draft: kommt ein `text`-Parameter mit, gewinnt er gegenueber dem
Template-generierten Text (`text` aus dem Template ist der Default, Operator-Korrektur
hat Vorrang). Der `prompt_text` in `bench_challenges` bleibt immer die eingefrorene
Kopie des Generations-Prompts, nie der Tweet-Text.

## Alternativen

- **Bench-Logik in den Kern** (wie `x_publisher`): verworfen — Challenge-Orchestrierung
  ist Marks Content-Business, nicht generische Infrastruktur; ADR-044 existiert genau
  dafuer. Der Kern soll auf jeder MC-Installation funktionieren.
- **Eigene Publish-Tabelle/Status im Vertical** (z. B. `bench_published`-Row): verworfen
  — verletzt "kein zweiter Lifecycle" (ADR-065); Approval + ContentPipeline decken
  Freigabe + Publish-Tracking vollstaendig ab.
- **Kern schreibt Challenge-Status direkt** (Import des Verticals in `approvals.py`):
  verworfen — bricht die ADR-044-Kopplungsrichtung und jede gestrippte Installation.
- **Polling statt Resolved-Hook** (Vertical prueft beim GET, ob die Approval resolved
  ist): verworfen als Primaermechanismus — ohne offenen Studio-Tab bleiebe `drafted`
  ewig stehen; nur der Failed-Task-Reconcile bleibt GET-seitig als Sicherheitsnetz.
- **SSE-Stream fuer Challenge-Fortschritt:** verworfen fuer v1 — es gibt keinen
  generischen SSE-Hook im Frontend; 5-s-Polling reicht fuer einen Ein-Operator-
  Studio-Tab (spaeter nachruestbar ohne Schema-Umbau).
- **Alles im Kern** / **alles privat ohne Vertical-Muster:** beide Extreme verworfen —
  ersteres blaest den OSS-Kern auf, letzteres erzwingt eine dritte parallele Codebasis.

## Konsequenzen

### Positiv

- Komplette Kette (Prompt → Video-Grid → X-Post) in MC, Freigabe wie immer in der Inbox.
  Studio-Review (Operator begutachtet Grid) und Post-Freigabe (Approval) sind bewusst
  getrennte Gates — ein Lauf darf nie ohne Review gepostet werden.
- Metriken historisiert (`bench_entries.metrics` JSON) — Speed-Charts ohne Schema-Umbau
  (Spec §8, "wir duerfen uns nicht verbauen").
- `x_post_resolved_hooks` ist generisch: der naechste Konsument (z. B. ein LinkedIn-
  Publisher-Vertical) haengt sich identisch ein, ohne einen neuen Hook-Typ einzufuehren.
- `task_done_hooks` entgated → auch Agent-Tasks ohne Pipeline koennen Hooks ausloesen;
  aeltere Hooks (die nur bei `pipeline_id` relevant sind) self-filtern.

### Negativ (bekannte Schwaechen)

- **Crash-Recovery-Gap (Design-Gap, dokumentiert):** Ein Backend-Crash waehrend
  `rendering` oder `composing` laesst die Challenge in diesem Zustand haengen — es gibt
  keinen automatischen Re-Entry. Operator-Reset via `POST /bench/{id}/rerender`. Das
  `rerender`-Schreiben setzt `status=failed` und schreibt eine Fehler-Meldung, damit
  der Operator weiss, was passiert ist.
- **Theoretisches Race-Window beim 409-Guard:** der Draft-Idempotenz-Check laeuft
  nicht unter Datenbank-Unique-Constraint — zwei gleichzeitige Requests koennen in einer
  kurzen Zeitspanne zwei Approvals anlegen. Fuer ein Ein-Operator-System akzeptiert
  (Frequenz vernachlaessigbar); loesbar mit DB-Unique-Constraint in v2.
- **Expired-pending-Approval blockiert Re-Draft:** eine abgelaufene, noch offene Approval
  (Status `pending`, TTL ueberschritten) muss der Operator zuerst manuell ablehnen, bevor
  ein neuer Draft angelegt werden kann. Erklaerte Einschraenkung fuer v1.
- **X-Video-Limit-Konstanten noch nicht hinterlegt:** maximale Video-Groesse/-Laenge/-
  Aufloesung fuer die X API sind im Code noch nicht als benannte Konstanten definiert —
  Fehler bei Ueberschreitung kommt erst vom tweepy-Upload. Follow-up-Task.
- **`sharedSubpath`-Duplikation:** das Frontend importiert `sharedSubpath` aus einem
  Core-Helper statt aus `mediaPathToFilesLocation` — eine geringe Doppelung, die in
  einem Cleanup-PR adressiert werden kann.
- Gestrippte Installationen tragen zwei brachliegende Kern-Tabellen (`bench_challenges`,
  `bench_entries`) — bewusst, ADR-044.
- Hook-Indirektion: Debugging braucht das Wissen, dass `register()` beim App-Boot laeuft
  und `_handle_x_post_resolution` die Registry ruft.
- 5-s-Polling im Studio-Tab erzeugt Grundlast (ein Operator — akzeptiert).

## Referenzen

- `backend/app/models/bench.py` — `BenchChallenge`, `BenchEntry` (SQLModel)
- `backend/alembic/versions/0153_bench_studio_tables.py` — Migration
- `backend/app/verticals/hooks.py` — `x_post_resolved_hooks` (neue Registry)
- `backend/app/verticals/bench_studio/orchestrator.py` — Zustands-Maschine + Generierungs-Pfade
- `backend/app/verticals/bench_studio/drafts.py` — Draft-Idempotenz, Pipeline-Reuse
- `backend/app/verticals/bench_studio/routers.py` — `/api/v1/bench/*`
- `frontend-v2/src/verticals/bench_studio/` — BenchStudioPage, ChallengesTab, PromptLibraryTab,
  DraftDialog, NewChallengeDialog, ChallengeDetail, api.ts, types.ts
- `frontend-v2/src/app/bench/page.tsx` — Route-Entry
- `docker/mc-playwright/media.py` — `/record` + `/compose` Endpunkte
- `docker/mc-playwright/service.py` — ffmpeg-Aufruf Grid-Komposition
- `backend/app/services/x_publisher.py` — `post_media()` (Baustein 1, PR 1)
- `backend/alembic/versions/0152_prompt_templates.py` — Prompt Library Migration
- Spec: `docs/superpowers/specs/2026-07-11-benchmark-studio-design.md`
- [ADR-044](044-vertical-modules.md) — Vertical-Module (Praezedenz + Kopplungsrichtung)
- [ADR-065](065-x-post-publisher.md) — X-Publisher ("kein zweiter Lifecycle", Approval-Flow)
- [ADR-033](033-secrets-vs-credentials-boundary.md) — Secrets vs Credentials Boundary
