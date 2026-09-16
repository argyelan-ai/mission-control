# Harness Development Ring — was ein Harness können muss

Marks Vorgabe (16.09.2026, bindend): *„alle agenten, egal welche art, welche
harness oder host oder container sollten nach einem switch genau so
funktionieren wie vorher. das muessen wir beweisen nach dem umbau"* und
*„wir muessen natuerlich unseren MC Entwicklungsring dementsprechend
erweitern, damit wir wissen, was zu tun ist wenn wir einen neuen Harness
integrieren moechten."*

Diese Datei ist die Handreichung dazu; der Wächter ist
`backend/tests/test_harness_dev_ring.py` (zwei Richtungen, siehe dort).

## Die Grundgesamtheit — gezählt, nicht geschätzt

**Layer A — mechanische Dispatch-Ebene** (5). Was das System einem Agenten
antut, bevor er ein Wort sagen kann:

| # | Fähigkeit | Beweis-Anker |
|---|-----------|--------------|
| 1 | dispatch-prompt | Prompt erreicht die Turn-Grenze (poll.sh paste/nudge; omp: bridge wrap_prompt, bridge.py:1140) |
| 2 | ack-transition | Backend sieht in_progress (agent_heartbeat, agent_scoped.py:326) |
| 3 | turn-signal | Turn-Signal-Datei wird an der Turn-Grenze geehrt (poll.sh:180; settings_extras je Harness, harness_compat.py:296) |
| 4 | term-int-survival | TERM/INT-Traps räumen auf (poll.sh:1615-1616) |
| 5 | heartbeat-context | Heartbeat trägt context_pct (bridge `_build_heartbeat_payload`; #606/#609) |

**Layer B — Task-Vertrag** (17). IDs: `cli-ack`, `cli-comment`,
`cli-checklist`, `cli-deliverable`, `cli-finish`, `cli-ask`, `cli-inbox`,
`cli-msg-thread`, `cli-memory`, `cli-vault`, `cli-review`, `cli-park`,
`cli-worker-restart`, `cli-task-state`, `cli-delegate`, `cli-docs`,
`deliverable-get` (als Teil von cli-deliverable gezählt, eigene ID für den
Lese-Weg). Layer-C-IDs: `ssh-push`, `container-place`.

Details: Die `mc`-Verben, ohne die ein Task-Zyklus
nicht durchläuft — gezählt als die 45 CommandSpecs in
`scripts/mc-cli/mc_cli/commands.py`, hier verdichtet zu den 17
aufgaben-kritischen Gruppen (ack, comment, checklist, deliverable,
deliverable-get, finish+Reflexion, ask/question, inbox, msg/thread, memory,
vault×2, review-Verben, park/hold, worker-restart, Status-Verben, delegate,
docs). Der Backend-Gegenwächter ist `test_mc_cli_endpoints.py`
(MUST_HAVE_CLI_ENDPOINTS ↔ SKIP_CLI mit Grund).

**Layer C — Ort** (2). ssh-push (Arbeitsregel 15: lokal committen, SSH
pushen lassen — die workflow-Scope-Sperre gilt nur für HTTPS) und
container-place (mc-<harness>-agent-Image bzw. Host-Adapter in
HOST_ADAPTERS, host_harness_adapter.py:446-454).

**Summe: 24 Fähigkeiten je (Harness, Ort)-Zelle.** Sechs Harnesses
(HARNESS_CAPABILITIES, harness_compat.py:296-337) × zwei Orte = 12 Zellen,
288 Aussagen im Vollausbau. Der Wächter zählt die Spalten nach — „gefällt
mir" ist dort kein Argument, nur `da`/`fehlt-bewusst`/`offen` + Beweis.

## Matrix (Ist-Stand, Seed: claude/container)

| Fähigkeit | claude/container | sonstige Zellen |
|-----------|------------------|-----------------|
| dispatch-prompt … heartbeat-context | `da` (start-claude.sh; settings_extras=True harness_compat.py:303; poll.sh:180/:1615; statusLine) | **Wächter rot bis deklariert** — das ist gewollt: jede Zeile wird mit Fundstelle nachgezogen, nicht gebulldozed |
| ssh-push | `fehlt-bewusst` (seit 2026-09-15, Recheck: credential-scope; Grund: agent credential ohne workflow scope, Fork-Gegenprobe identisch) | je Harness |
| container-place | `da` (docker/mc-claude-agent/) | je Harness |

`fehlt` vs `unbelegt`: `fehlt-bewusst` trägt `since` + `recheck` und wird in
Richtung 2 des Wächters **jederzeit neu abgeleitet** (Container-Claim gegen
`docker/mc-<h>-agent/`, Scope-Claim gegen die Credential-Wirklichkeit). Ein
`offen` ist eine offene Lücke mit Zähler — sichtbar, aber kein Mock-Beweis.

## Neuer Harness — die Schritte, jeder mit Beweis

0. **Pflicht-Reads:** Skill `mc-cli-onboarding` (Adapter-Kontrakt,
   Signal-Hierarchie, §7a Antwort-Beweis) — diese Datei ergänzt, sie
   ersetzt nichts.
1. **Registry-Einträge:** HARNESS_CAPABILITIES + (host) HOST_ADAPTERS bzw.
   (container) Image. → Der Wächter wird **sofort rot** (Richtung 1) und
   listet die fehlenden Aussagen. Das ist die Arbeitsliste.
2. **Layer A mechanisch:** Turn-Grenze + Signale + Heartbeat (Adapter bzw.
   poll.sh-Verdrahtung). Beweis je Stufe: Signal feuert im Container,
   detect_turn_state schaltet, ACK-Kette (§7a: Dispatch→ACK→Antwort, kein
   Config-Vertrauen).
3. **Layer B Task-Zyklus:** `mc`-CLI im Image/Host-Pfad installieren
   (mc-agent-base/mc-cli), dann **Messlauf**: Task `inbox` → Marker-Message →
   wörtliches Quotieren → Cursor/Ack disk-verifiziert → deliverable inline
   + Datei → checklist done → finish mit Reflexion → status done. Erst wenn
   alle 17 Layer-B-Verben am echten Agenten gelaufen sind, ist die Zelle `da`.
4. **Layer C Ort:** Container: Image-Bau + Bind-Mounts; Host:
   entrypoint + Plist + Env-Dir. ssh-push-Fähigkeit je Ort prüfen und als
   `da`/`fehlt-bewusst`+since+recheck deklarieren.
5. **Fleet-Beweis (Marks Satz 1):** nach dem Umbau derselbe Task-Zyklus am
   umgezogenen Agenten — „funktioniert wie vorher" ist der 7a-Lauf gegen
   die volle Layer-B-Liste, nicht nur „antwortet".

## Warum ein Wächter statt einer Doku

Die Doku hier erklärt; der Wächter (`test_harness_dev_ring.py`) zählt.
Richtung 1 verhindert stilles Dazunehmen, Richtung 2 verhindert verrottende
Ausnahmen (der Claude-Auto-Memory-Befund, Task 67792587: 113/113 Notizen
nur harness-seitig, ist genau die Klasse von Lücke, die eine nur
vorwärts prüfende Liste nie gesehen hätte). Der Registry-Test
`test_host_harness_catalog.py` (HARNESSES ⊆ HOST_ADAPTERS) bleibt bestehen —
dieser Wächter prüft die **Aussagen**, nicht nur die Existenz.
