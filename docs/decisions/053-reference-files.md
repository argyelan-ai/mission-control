# ADR-053: Referenz-Dateien für Tasks & Projekte

**Status:** Accepted (2026-07-04)

## Kontext

Tasks hatten Referenzen nur als URLs + Freitext (`reference_urls`,
`reference_notes`, Migration 0046). Mark will Beispiel-/Asset-**Dateien**
mitgeben (Layout-Screenshot, Beispiel-CSV, Spez-PDF), die Agenten direkt
nutzen. Vorhandene Bausteine: Knowledge-Attachment-Upload als Muster
(`memory.upload_attachment`), Files-Root-System, und der 1:1-`~/.mc`-Mount
(Backend- und Agent-Container sehen identische absolute Pfade).

## Entscheidung

- **Tabelle `reference_files`** (Migration 0140): genau eines von
  `task_id`/`project_id` gesetzt; `rel_path` relativ zum neuen Files-Root
  **`references`** (`~/.mc/references/{task|project}/{id}/{sha16}-{name}`).
  Root ist browsable, aber NICHT im Files-Browser löschbar — Löschen läuft
  ausschliesslich über die References-API (Row + Datei atomar).
- **API `/api/v1/references`**: Upload (multipart, MIME-Allowlist 14 Typen,
  25 MB, max 20/Entity, Traversal-Guard auf dem rohen Multipart-Namen wie
  beim Knowledge-Muster), List (Task-Liste enthält geerbte
  Projekt-Referenzen mit `inherited`-Flag), Download (auth-gated,
  `Content-Disposition: attachment`), Delete.
- **Dispatch-Injektion:** `task_context_builder` lädt eigene + geerbte
  Referenzen → `ctx.reference_files_context`; `dispatch_message_builder`
  rendert die Sektion „Reference files (uploaded by the operator)" mit
  **absoluten `~/.mc`-Pfaden** — Agenten lesen die Dateien direkt vom
  gemounteten Filesystem, kein Agent-API-Endpoint nötig. Cap 15 Dateien im
  Brief (Rest via /files).
- **Kaskaden:** beide Task-Delete-Endpoints + Projekt-Delete (inkl. der per
  Bulk-SQL gelöschten Projekt-Tasks) räumen Rows + Dateien ab
  (`services/reference_cleanup`).
- **UI:** Task-Maske staged Dateien und lädt nach dem Create hoch;
  Task-Detail mit References-Sektion (geerbte markiert); Projekt-Referenzen
  über einen Dialog am Projekt-Gruppen-Header der Tasks-Seite.

## Alternativen

- **Wiederverwendung des `attachments`-Roots** (Knowledge): verworfen —
  vermischte Semantik, Knowledge-Attachments hängen an BoardMemory-JSON,
  Referenzen brauchen eigene Entity-Bindung + Vererbung.
- **Dateien als Deliverables:** verworfen — Deliverables sind Agent-Output,
  Referenzen sind Operator-Input; getrennte Lebenszyklen.
- **Agent-API zum Abruf:** unnötig — der 1:1-Mount macht die Pfade direkt
  lesbar; weniger Angriffsfläche als ein Download-Scope.

## Konsequenzen

- Referenzen erscheinen automatisch im Files-Browser (Root `references`,
  capture-at-write in den file_index + periodischer Walker).
- Schedule-Job-Templates unterstützen (noch) keine Dateien — bewusst.
- Host-Agents ausserhalb des Mounts (keine bekannt) sähen die Pfade nicht.
