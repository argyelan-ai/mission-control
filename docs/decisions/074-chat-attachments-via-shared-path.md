# ADR-074 — Chat-Anhänge über einen gemeinsamen Pfad statt über ein neues Protokoll

**Status:** Accepted
**Datum:** 2026-08-19
**Scope:** Backend/Services · Backend/API · Frontend/Pages · Security

## Kontext

Die Sessions-Chat-Ansicht (ADR-073) konnte ausschliesslich Text zustellen: Die
Eingabe tippt in die laufende TUI (tmux `send-keys`, bei Host-Agenten die
WS-Brücke). Der Operator wollte Bilder und Dateien senden können — sein realer
Arbeitsweg ist ein Screenshot mit `Cmd+Shift+4`, dann `Cmd+V`, und auf dem Handy
ein Foto aus der Galerie oder frisch aus der Kamera.

Ein Anhang muss also bei einer CLI ankommen, die nur eine Zeile Text
entgegennimmt. Naheliegend wäre ein Nebenkanal gewesen — ein eigener Endpunkt,
der Bilddaten an den Agenten schiebt. Den kennt aber keine der vier Harnesses:
Wir treiben interaktive CLIs fern, wir ersetzen sie nicht (ADR-072).

Die entscheidende Beobachtung: Alle Harnesses **lesen Dateien über Pfade**. Es
braucht also keinen neuen Transportweg, sondern nur einen Pfad, der auf beiden
Seiten derselbe ist.

Zwei weitere Anforderungen kamen vom Operator und sind bindend:

1. **Alle Agenten, alle Dateitypen.** Ob ein Agent eine Datei versteht, ist nicht
   die Zusage des UI. Keine Harness-Gates, keine MIME-Liste.
2. Der Weg muss auf **Desktop und Handy** funktionieren.

## Entscheidung

Ein Anhang wird als **Agenten-Referenz** unter
`~/.mc/references/agent/<agent-id>/<prüfsumme>-<name>` abgelegt, und sein
**absoluter Pfad** wird der Nachricht als eigene Zeile angehängt
(`[Anhang: /pfad]`). Die CLI liest die Datei selbst.

Abgelegt wird über `services/reference_ingest.py` — dieselbe Ablage, die der
References-Upload und der Slack-Datei-Ingest benutzen. Die Besitz-Art
`reference_files.agent_id` (Migration 0172) beschreibt exakt diesen Fall: eine
Datei, die der Operator top-level im Chat schickt, gehört dem AGENTEN und
keiner Aufgabe. Die zwei Regeln, die für einen laufenden Chat nicht passen,
sind dort Parameter geworden statt ein Grund für eine zweite Ablage:
`allowed_mimes=None` (alle Dateitypen) und `max_files=None` (kein 20er-Deckel).
Die Voreinstellung bleibt streng — References-Upload und Slack-Ingest
verhalten sich unverändert.

Dieser Ordner wurde gewählt, weil er in **jeden** Agenten-Container unter *exakt
demselben absoluten Pfad* gemountet ist (`${HOME}/.mc/references:${HOME}/.mc/references:ro`)
und Host-Agenten ihn ohnehin direkt lesen. Ein Pfad gilt damit überall — es gibt
keine Übersetzung pro Agent und keine Kopie.

Der Verlauf gewinnt die Kacheln aus dem Text zurück (`attachments.ts`). Das
Transkript der CLI bleibt die einzige Quelle: Ein Nebenkanal wäre eine Quelle,
die der Chat nach einem Neuladen nicht mehr anzeigen könnte.

## Alternativen

- **Eigener Anhang-Kanal zur CLI** (Bilddaten direkt an den Agenten) → Verworfen:
  Keine der vier Harnesses hat einen solchen Eingang. Es hätte bedeutet, die CLI
  zu ersetzen statt sie fernzusteuern — die Grenze, die ADR-072 zieht.
- **Ablage unter `~/.mc/workspaces/<slug>/uploads/`** (der erste Entwurf) →
  Verworfen: Der Workspace-Mount heisst im Container `/workspace`, auf dem Host
  aber anders. Das hätte eine Pfad-Übersetzung pro Agent gebraucht — und für
  Host-Agenten wie Boss, die keinen solchen Mount haben, gar nicht funktioniert.
- **Ablage unter dem registrierten `attachments`-Root** → Verworfen: Der Root ist
  in `fs_roots` eingetragen, existiert auf der Platte aber nicht und ist nirgends
  gemountet. Nutzbar erst nach einer Compose-Änderung und einem Neubau aller
  Container — Aufwand ohne Gegenwert, da `references` bereits überall liegt.
- **Eine zweite Ablage neben `reference_ingest`** (`services/chat_attachments.py`,
  der erste Entwurf dieser ADR) → Verworfen und zurückgebaut: Begründet war sie
  mit dem 20-Dateien-Limit und der MIME-Allowlist — beides Hindernisse, die
  Parameter sein mussten und keine Rechtfertigung für einen parallelen
  Speicherpfad. Der Preis war real: Anhänge blieben beim Löschen ihres Agenten
  verwaist liegen (`delete_references_for(agent_id=…)` räumt nur Referenzen ab),
  es gab keine DB-Zeile, und der Traversal-Guard, das Prüfsummen-Präfix und die
  realpath-Gegenprobe existierten doppelt. Umgekehrt hat der Rückbau dem
  gemeinsamen Speicher etwas gebracht, das er noch nicht hatte: **atomares
  Schreiben** (`.part` → `os.replace`) — vorher konnte ein Abbruch mitten im
  Schreiben eine halbe Datei unter dem Zielnamen hinterlassen, die der Agent
  ohne Fehlermeldung liest. Das gilt jetzt für alle drei Wege.

## Konsequenzen

### Positiv

- Kein neues Protokoll, kein Nebenkanal, keine Änderung an den CLIs.
- Ein Pfad für alle Harnesses **und** für Host-Agenten.
- Der Verlauf bleibt vollständig aus dem Transkript rekonstruierbar.
- Live bewiesen vor dem Bau: Testbild abgelegt, der Agent hat den Bildinhalt
  korrekt wiedergegeben.

### Negativ

- **Die Freigabe aller Dateitypen zog eine Sicherheitsänderung nach sich.** Der
  Files-Browser lieferte bisher jede Datei inline mit Endungs-MIME aus; geschützt
  war das allein durch die MIME-Allowlist der References-Uploads (Review-Fund M1,
  Stored XSS im App-Origin). Mit freien Uploads trägt diese Annahme nicht mehr.
  `fs_service.read_stream` liefert aktive Inhalte (HTML/SVG/XML/JS) darum ab
  jetzt **immer** als Download mit neutralem Content-Type aus — für **alle**
  Files-Roots, nicht nur für den Chat. Das ist eine repo-weite
  Verhaltensänderung: Wer bisher eine SVG-Vorschau im Datei-Browser erwartet hat,
  bekommt nun einen Download.
- Der Ordner wächst. Es gibt **kein Alters-Fenster**: Anhänge verschwinden mit
  ihrem Agenten (`delete_references_for(agent_id=…)` beim Löschen des Agenten),
  so wie jede andere Referenz auch. Ein zusätzlicher 30-Tage-Lauf über denselben
  Baum wäre eine zweite Aufräum-Regel für dieselben Dateien gewesen — und er
  hätte Anhänge unter noch existierenden Nachrichten weggeräumt, deren Pfad im
  Transkript stehenbleibt. Bewusst in Kauf genommen: Wer nie einen Agenten
  löscht, sammelt Anhänge an; das ist im Files-Browser sichtbar und von Hand
  löschbar.
- Die Erkennung „ist das ein Bild?" existiert doppelt (Backend für die Antwort,
  Frontend für die Rückgewinnung aus dem Transkripttext). Beide Listen müssen
  zusammen gepflegt werden.
- Ob ein Agent den Anhang wirklich lesen kann, verspricht das UI bewusst nicht.

## Referenzen

- Betroffene Dateien: `backend/app/services/reference_ingest.py`
  (`allowed_mimes` / `max_files` / atomares Schreiben / `is_image_reference`),
  `backend/app/routers/agent_chat.py` (`POST /agents/{id}/chat/attachment`),
  `backend/app/services/fs_service.py` (`read_stream`, aktive Inhalte),
  `frontend-v2/src/components/chat/Composer.tsx`,
  `frontend-v2/src/components/chat/attachments.ts`,
  `frontend-v2/src/components/chat/ChatAttachmentTile.tsx`,
  `frontend-v2/src/hooks/useAuthBlob.ts`
- Verwandte ADRs: ADR-072 (Adapter-Kontrakt), ADR-073 (Transkript-Tailing),
  ADR-053 (Referenz-Dateien), ADR-022 (`~/.mc`-Layout)
