# ADR-075 — Multi-Agent-Gruppenchat auf comm_v2-Threads

**Status:** Accepted
**Datum:** 2026-08-20
**Scope:** Backend/DB + Backend/Delivery + Frontend/Sessions

> Nummer 075 (nicht 074): ADR-074 ist von PR #330 (Chat-Anhänge) belegt —
> gleiche Kollisionsvermeidung wie bei Migration 0180 (offener PR zuerst).

## Kontext

Mark will Gruppen aus beliebigen Agenten mit einem Pflicht-Ziel: autonome
Research-/Diskussions-Runden UND Live-Mitchatten, Ergebnis als Verlauf plus
lebendes Dokument. Drei Systeme boten sich als Unterbau an: der Sessions-Chat
(ADR-073), die Meetings-Altlast (`agent_meetings`, Phase-31-Torso) und das
comm_v2-Thread-System (Interaction Model 2.0).

Live-Befunde (2026-08-20): Der Sessions-Chat ist ein read-only Spiegel EINES
CLI-Transkripts — strukturell nicht gruppenfähig. Die Meetings-Tabellen haben
0 Zeilen, keinen funktionierenden Zustellweg (`_send_and_wait` ist ein
Platzhalter) und keine UI. comm_v2 trägt dagegen N Leser pro Thread schon
heute (AgentThreadCursor mit Composite-PK), inklusive Nudge+Pull, Mentions
und Antwort-Pfad.

## Entscheidung

**Eine Gruppe = ein `Thread(kind="group")` + `agent_groups`-Config +
`group_members` — kein neues Nachrichtensystem.** Teilnahme-Scope läuft über
`thread_scope.message_threads_for_agent` (eine Regel, Zustellung UND
Antwort-Recht). Zustellung ist **mention-gefiltert**: Auf Gruppen-Threads
erhält ein Agent nur Nachrichten, die ihn erwähnen; alles andere schiebt den
Cursor still weiter. Das Wort erteilen ausschliesslich die Runden-Engine
(`group_runner`, PR B), Marks @-Mentions und explizite Agenten-@-Mentions —
**Agenten-Posts ohne Mention wecken strukturell niemanden** (Sturm-Schutz als
Struktur, nicht als Drossel). Die Meetings-Altlast wird beerdigt (Konzepte
Agenda/Runden/Facilitator wandern auf Threads); die Loop-Konzepte
(max_rounds, Budget, Circuit-Breaker, Gates, Runden-Reports) leben mit
identischen Feldnamen in `agent_groups` weiter — Gruppen absorbieren Loops
langfristig (V3), die Loops-Seite bleibt vorerst unangetastet.

Weitere Festlegungen (Mark, 2026-08-20): kein Modus-Schalter (Verhalten folgt
aus `status`: idle = Live, running = Runden), Runden laufen **parallel**
(alle Sprecher gleichzeitig, Lead urteilt zuletzt), das lebende
Ergebnis-Dokument ist V1 (nur der Lead schreibt, Datei unter
`~/.mc/references/groups/<slug>/result.md`, Snapshot je Runde in
`group_rounds.doc_snapshot`).

## Alternativen

- **Meetings wiederbeleben:** Drittes Nachrichten-Schema neben
  `messages`/`task_comments`, toter Kern, keine UI → verworfen
  (no_second_lifecycle).
- **Sessions-Chat erweitern:** Transkript-Tailing ist 1:1-gebunden; ein Raum
  bräuchte einen gemeinsamen Speicher, den das Modell nicht hat → verworfen;
  die Sessions-*Seite* bleibt nur der Verpackungsort.
- **Zustellung ungefiltert (jeder sieht alles):** N Agenten, die einander
  antworten, eskalieren zu Nachrichten-Stürmen (vgl. Port-Vergiftung
  18.08.) → verworfen zugunsten des Mention-Filters.
- **LLM-Facilitator als eigener Callpfad:** neuer API-Kostenpfad,
  Modell-Management, kann hängen → verworfen; Moderations-LOGIK ist Code,
  das URTEIL liefert der Lead als normaler Teilnehmer-Turn.

## Konsequenzen

### Positiv
- Poll/Inbox/Antwort-Pfad und poll.sh bleiben unverändert; ein Scope-Join +
  ein Payload-Filter tragen die ganze Gruppen-Zustellung.
- Sturm-Sicherheit ist beweisbar (Test: Post ohne Mention → niemand geweckt).
- Budget/Gates/Runden sind mit den Loops-Feldern deckungsgleich → spätere
  Absorption ist Umzug, kein Umbau.

### Negativ
- Gruppen sind gemächlich: 5s-Poll + Turn-Grenzen-Gate — „sofort" gibt es
  nicht (Systemeigenschaft, in der UI ehrlich anzeigen).
- Budget bremst nachlaufend (Token-Harvester) — der harte Deckel ist
  max_rounds, nie das Budget.
- Ein später hinzugefügtes Mitglied bekommt alte Erwähnungen seines Namens
  beim ersten Poll nachgeliefert (kein Cursor-Fast-Forward für Gruppen —
  bewusst: sonst verlöre ein frisches Mitglied Marks ersten Auftrag im
  5s-Fenster nach Gruppenerstellung).

## Nachtrag PR B (gleicher Tag)

- **Live-Verhalten vereinfacht (Abweichung vom Plan-Detail):** V1 legt KEINE
  `GroupRound(kind="live_impulse")`-Zeilen an. Live-Mitchatten ist reine
  mention-gefilterte Zustellung; Agent-zu-Agent-Ketten deckelt ein
  Anti-Ping-Pong-Zähler im Antwort-Pfad (`live_max_turns_per_impulse`,
  gezählt seit der letzten User-/System-Nachricht — ab dem Deckel werden die
  Mentions gestrichen, der Post bleibt im Protokoll). Budget erfasst damit
  nur autonome Runden; Live-Plauderei läuft über die globale
  Nutzungs-Telemetrie. Das `kind`-Feld bleibt für eine spätere echte
  Live-Runden-Erfassung bestehen.
- **`approvals.board_id` nullable (Migration 0182):** group_gate-Approvals
  gehören zu einer Gruppe, nicht zu einem Board. Die globale Pending-Liste
  zeigt sie weiterhin; board-gescopte Listen filtern sie schlicht nicht ein.
- **max_rounds zählt PRO LAUF:** `current_round_no` wird bei jedem frischen
  Start genullt (Resume einer offenen Runde behält die Zähler);
  `rounds_completed` bleibt Lebenszeit-Statistik.

## Nachtrag PR C — UI (gleicher Tag)

- **Kein Modus-Schalter in der Oberfläche.** Der Kopf zeigt ▶/⏸/⏹ und den
  Rundenstand; ob die Gruppe gerade „live" oder „autonom" ist, liest man am
  Status, nicht an einem Umschalter (Nutzerentscheid).
- **Streng achromatische Sprecher** (Nutzerentscheid): Unterscheidung nur über
  Avatar + Name. Farbe bleibt Zuständen vorbehalten.
- **Runden-Trenner über `brief_seq`** statt Text-Parsing des Briefs — dafür
  trägt die Runden-Antwort dieses Feld.
- **Wahrhaftige Statuszeile:** Ist der SSE-Strom weg, sagt die Zeile das —
  sie rät nie einen Zustand. `failed` bekam einen eigenen Zweig, weil es sonst
  in den Else-Fall „Bereit" gefallen wäre (im Review gefunden).
- **Zweisprachig:** alle Texte unter `sessions.groups` in `messages/de.json`
  UND `messages/en.json` (76 Schlüssel, Parität geprüft) — MC ist
  deutsch *und* englisch, hartcodierte Strings wären ein Rückschritt.
- **Ein Markdown-Renderer:** `MarkdownContent` wurde aus `ChatMessage.tsx` in
  ein eigenes Modul gezogen, damit Gruppenraum und 1:1-Chat nicht
  auseinanderdriften (Verhalten unverändert, bestehende Tests grün).

## Nachtrag Live-Betrieb — was erst der Echtbetrieb gezeigt hat (2026-08-22)

Vier Fehler überlebten die gesamte Testabdeckung und fielen erst im ersten
echten Lauf auf. Sie stehen hier, weil sie ein Muster bilden: **grüne Tests
prüfen die Mechanik, nicht die Umwelt.**

1. **Der Lead konnte das Ergebnis-Dokument gar nicht schreiben.** Der Plan
   ging davon aus, er editiere es „mit normalen Datei-Tools, kein neuer
   Schreib-Endpoint nötig" — der References-Mount ist in den
   Agenten-Containern aber **read-only**. Lesen ja, schreiben nie. Der
   tragende Teil des Entwurfs war unmöglich. Korrektur:
   `PUT /agent/groups/{id}/document` (nur der Lead, sonst 403) plus das Verb
   `mc group-doc <id> --file <datei>`.
2. **Kein `mc group-doc`.** Der Lead lief live in `mc help | grep group`. Ein
   Backend-Endpunkt allein erreicht die Container nie — das `mc`-CLI ist in
   die Agent-Images eingebacken.
3. **Der Lead war nicht wechselbar.** Ein hängender Lead legte die Gruppe
   dauerhaft still. Korrektur: `lead_agent_id` per `PATCH` umsetzbar.
4. **Falsche Reihenfolge der Lead-Pflichten:** erst Dokument, dann Verdikt.
   Scheiterte Schritt 1, kam nie ein Verdikt — drei Runden verbrannt.

## Nachtrag Kursänderung — der Chat ist das Protokoll, nicht das Produkt (2026-08-22)

Operator-Befund nach dem ersten vollständigen Lauf: „die Agenten schreiben
extrem viel, das überlädt den ganzen Chat." Gemessen: 1600–4900 Zeichen je
Beitrag, 30 000–41 800 Token je Runde.

Es war **kein Agenten-Problem**. Der Runden-Brief verlangte Quellen-Pflicht,
Dissens-Ausweis und Zwangsformat — und sagte kein Wort über Länge. Ein so
beauftragter Agent schreibt einen Aufsatz; er tut genau, was dasteht. Dahinter
lag eine unausgesprochene Vermischung: Hermes' Bot Mode baut ein *Gespräch*,
wir bauen einen *Auftrag mit Ergebnis* — hatten die Tiefe aber im
Gesprächskanal abgeladen.

**Leitsatz:** Der Chat trägt die Meinungsbildung, das Ergebnis-Dokument trägt
die Substanz.

- Längenbudget im Brief (2–4 Sätze); der Lead postet kurz und schreibt lang
  ins Dokument.
- `PASS` wird eine vollwertige Antwort (Hermes-Muster) statt des kleinlauten
  „NICHTS NEUES"; die deutsche Altform bleibt gültig, damit laufende Gruppen
  mit älteren Briefen im Thread nicht brechen.
- Passen **alle** Sprecher, endet der Lauf regulär (`all_passed` →
  `room_settled`), ohne den teuersten Turn zu wecken. Das **ergänzt** die
  Fortschritts-Bremse, ersetzt sie nicht: den Teil-Fall (halbe Runde passt,
  Lead urteilt trotzdem WEITER) deckt weiterhin nur `continue_stale` ab. Wer
  per Timeout übersprungen wurde, zählt nicht als Passer — er ist nicht
  einverstanden, sondern unbekannt.
- Kontext als Delta (Beiträge 2000 → 400 Zeichen).
- Turn-Timeout 600 → **900 s**, bewusst gegen die Hermes-Vorlage (180 s):
  lokale Motoren brauchen bei langem Kontext lange bis zum ersten Token, und
  unsere Turns schliessen Recherche und Werkzeug-Aufrufe ein. Ein zu knapper
  Deckel kostet die Runde einen ganzen Beitrag; Warten kostet nur Zeit.
- UI: lange Beiträge klappen im Raum zu — das Netz für den Fall, dass ein
  Agent die Längenvorgabe ignoriert.

Bewusst **nicht** von Hermes übernommen: „alle antworten, wenn niemand
erwähnt wird" (bricht den Sturm-Schutz) und das reine Gesprächsmodell ohne
Ziel (unser Pflicht-Ziel ist der Grund, warum eine Runde überhaupt endet).

## Referenzen

- Betroffene Dateien: `app/models/group.py`, `alembic/versions/0181_agent_groups.py`,
  `app/services/thread_scope.py`, `app/services/group_service.py`,
  `app/routers/groups.py`, `app/routers/agents.py` (`_group_message_visible_to`),
  `app/routers/agent_scoped.py` (Mention-Auflösung + SSE-Hook),
  `app/comm_constants.py` (THREAD_KINDS+group), `app/redis_client.py`
  (group_events)
- Verwandte ADRs: ADR-051 (Loops), ADR-071 (Nudge+Pull), ADR-072
  (Chat-Adapter), ADR-073 (Sessions-Chat)
- Plan: `~/.claude/plans/wir-haben-ja-vor-enchanted-codd.md` (Design-Runde
  mit 2 Explorern + 3 Planern, 2026-08-20)
