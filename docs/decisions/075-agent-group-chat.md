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
