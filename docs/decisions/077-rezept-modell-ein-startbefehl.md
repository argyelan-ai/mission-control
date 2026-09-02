# ADR-077 — Ein Rezept-Modell: Engine · Startbefehl · Port · Topologie

**Status:** Accepted
**Datum:** 2026-09-02
**Scope:** Backend/Runtime · Backend/DB · Backend/API · Frontend/Runtimes
**Supersedes:** ADR-059 (Solo-Capability über `sparkrun list`; der Prozess-Check aus ADR-059 bleibt gültig)
**Baut auf:** ADR-036 (`runtimes.launch_command`), ADR-048 (Host-Registry)

## Kontext

Mission Control kann lokale Modelle auf GPU-Boxen starten. Dazu gibt es einen
Rezept-Umschalter (Dropdown auf der Gerätekachel und im Detail-Panel der
Seite `/runtimes`). Ein *Rezept* beschreibt, wie ein Modell gefahren wird.

Der Betreiber meldete: **„Ich sehe meine Rezepte nicht."** Die Karte des Codes
(Stand vor #388) ergab drei belegte Ursachen:

1. **Der Umschalter war stumm, nicht weg.** Beide Umschalter zeigten Rezepte
   nur, wenn die Runtime „sparkrun-verwaltet" war (`routers/runtimes.py`,
   Gate `sparkrun_managed` = Startbefehl enthält `sparkrun run`). sparkrun
   ist ein Kommandozeilen-Wrapper (ein Hilfsprogramm, das vLLM-Container
   startet). Rezepte, die über ein eigenes `start.sh` laufen, galten damit
   als „kein Rezept" und bekamen kein Dropdown. Dazu kam ein zweites Gate:
   `GET /runtimes` überspringt deaktivierte Zeilen, und die Kachel braucht
   mindestens eine „bereite" Runtime, bevor sie überhaupt ein Dropdown zeigt.
2. **Verbund-Tabelle leer und ohne Schreibpfad.** `runtime_hosts` (welche
   Boxen gehören zu einer Runtime) wurde nur gelesen, nie geschrieben.
   `runtimes.topology` wurde durchgereicht, aber nirgends ausgewertet. Eine
   zweite Box hing an keiner Runtime und zeigte „kein eigenes Modell".
3. **Ein Zweibox-Rezept ohne Startbefehl.** Die Runtime des Verbund-Rezepts
   hatte einen leeren `launch_command`. MC konnte sie also nie starten, und
   niemand sagte das.

Dahinter steckte ein Modell-Problem: Rezepte existierten an **drei Stellen**
mit drei Bedeutungen — sparkrun-Registry (per SSH abgefragt), Katalog
`local_recipes`, und Vorlagen in `services/launch_template.py`. Der
sparkrun-Weg hing an einem einzigen Werkzeug und einer einzigen Gerätefamilie.
Für andere MC-Nutzer war er wertlos, und für den Betreiber selbst blockierte er
alles, was nicht sparkrun war.

**Vorgabe des Betreibers (Spezifikation v2, 02.09.2026):** Alle freigegebenen
Rezepte anzeigen und startbar machen, auch Zweibox-Verbünde, als **generische
Kernfunktion** von MC. Keine Hardcodierung auf Geräte oder Modelle. Jeder
MC-Nutzer muss Verbund-Rezepte mit eigenen Boxen nutzen können.

Vertrag: `docs/plans/2026-09-02-rezept-umschalter-vertrag.md` (P0 + P1).

## Entscheidung

Es gibt **ein** Rezept-Modell. Ein Rezept = **Engine · Startbefehl (Pflicht) ·
Port · Topologie (Anzahl Boxen)**.

1. **sparkrun ist kein Konzept mehr.** Kein Gate `sparkrun_managed`, kein
   eigener Umschalter, keine Registry-Abfrage per `uvx sparkrun list`, kein
   `switch_recipe`. Ein sparkrun-Rezept ist ein gewöhnlicher Startbefehl
   (`uvx sparkrun run …`). `services/sparkrun_manager.py` und
   `POST /runtimes/{id}/switch-recipe` sind entfernt. Alte Katalogzeilen mit
   `engine=sparkrun` werden beim Start **umgewandelt, nie gelöscht**
   (`services/local_registry.repair_legacy_sparkrun_rows`, aufgerufen im
   Lifespan `main.py`). Runtime-Zeilen mit `uvx sparkrun run` im Startbefehl
   bleiben unverändert: sie waren schon immer gewöhnliche Docker-Starts.
2. **Topologie am Rezept, nicht Geräte.** `local_recipes.topology` =
   `{"nodes": 1|2}` und `local_recipes.port` (Migration `0191`, beide nullable,
   NULL = 1 Box, wie bisher). Das Rezept sagt nur **wie viele** Boxen es
   braucht. **Welche** Boxen es sind, steht an der Instanz: `runtimes.host_id`
   = Head (die Box, die MC anspricht), `runtime_hosts` = Mitglieder. Beim
   Anlegen einer Instanz wird `topology` aus dem Katalog kopiert und um
   `recipe_slug` als Rückverweis ergänzt.
3. **Startbefehl ist Pflicht** — im Router, nicht als DB-`NOT NULL`
   (`routers/runtimes.py::_require_launch_command`). Die Regel greift für
   aktivierte, host-gebundene Runtimes der befehlsgetriebenen Engines
   (`vllm_docker`, `llamacpp_docker`, `ssh_process`). Cloud-Runtimes und
   LM Studio (startet über `lms load`) bleiben unberührt. Bestehende Zeilen
   ohne Befehl bleiben lesbar, sind aber nicht startbar und sagen das:
   „Startbefehl fehlt".
4. **Eine Schnittstelle für beide Umschalter:**
   `GET /api/v1/hosts/{host_id}/recipes` (`routers/host_recipes.py`,
   Logik in `services/recipe_switcher.py`). Das Backend rechnet **fertig**:
   `fit` (`solo`/`duo`/`none`), `startable`, `running` (Live-Health-Probe am
   Head, nie geraten), `reason` als **Satz** in einfacher Sprache,
   `busy_hosts`, `candidate_workers`. Reihenfolge: laufend, dann startbar,
   dann grau. **Die Oberfläche rechnet nichts nach** — Gerätekachel und
   Detail-Panel zeigen dieselbe Liste aus derselben Quelle
   (`HostRecipeSwitcher.tsx`, ersetzt `SparkRecipeSwitcher.tsx`).
5. **Kein neuer Multi-Host-Startcode.** MC spricht nur mit dem Head.
   `POST /api/v1/hosts/{host_id}/recipes/{slug}/start` komponiert nur
   Bestehendes: Instanz anlegen (dieselben Felder wie der Box-Wizard), dann
   `runtime_manager.start_runtime` (SSH, `nohup bash -lc <launch_command>`,
   Verifikation über das Container-Label `mc.runtime.slug`, Verdrängung über
   `exclusive_memory`). Zweibox-Rezepte orchestrieren ihren Worker **selbst**
   (ihr `start.sh` zieht die zweite Box per SSH dazu, gesteuert über
   `HEAD_IP`/`WORKER_IP`). `runtime_hosts` dient Anzeige und Konfliktprüfung,
   nicht dem Start. In P1 antwortet ein Duo-Start ehrlich mit 409
   „Zweibox-Start kommt in Phase 3".
6. **Geräterolle head/worker** (P2, `hosts.role`) ist nur eine Vorbelegung
   für den Zweibox-Fall. Solo-Rezepte ignorieren sie: ein Ein-Box-Rezept läuft
   auf jeder freien Box.
7. **Generisch.** Keine Gerätedaten im Repo. Die Migration legt keine
   Datenzeilen an; welche Rezepte und Boxen ein Betreiber hat, lebt in seiner
   Datenbank. Testdaten heissen `box-a`/`box-b`/`recipe-x`.

**Bewusste Abweichung vom Vertrag (Regel 6):** Umgewandelte sparkrun-Zeilen
bekommen Engine `vllm_docker`, nicht `ssh_process` — sofern ein Startbefehl
entsteht (eigenes `launch_template` oder aus `recipe_ref` gebaut). Grund:
der Wrapper erzeugt einen Docker-Container mit dem Label `mc.runtime.slug`,
also genau das, was der `vllm_docker`-Lebenszyklus startet, verifiziert und
verdrängt. `ssh_process` hätte keinen Engine-Standard für den Start und keine
Label-Verifikation. Nur eine sparkrun-Zeile **ohne** jeden Befehl wird
`ssh_process`: lesbar, ehrlich „Startbefehl fehlt", nichts Erfundenes.

## Alternativen

- **sparkrun als dritte Quelle behalten** (Katalog + Geschwister-Runtimes +
  `sparkrun list`): Beschreibung — der Umschalter hätte den Katalog nur
  *zusätzlich* gelesen. → Verworfen weil damit zwei Rezept-Begriffe
  nebeneinander blieben, die Oberfläche weiter zwei Datenwege gemischt hätte,
  und ein Gerät ohne sparkrun weiter „kein Rezept" gezeigt hätte. Der
  Betreiber hat sparkrun als Konzept ausdrücklich gestrichen.
- **Eigener MC-Orchestrator für Zweibox-Starts** (MC startet Head und Worker
  selbst, per SSH auf beide Boxen): Beschreibung — neuer Startpfad über
  `runtime_hosts`, Reihenfolge Worker → Head in MC. → Verworfen weil die
  Rezepte das schon selbst können (`start.sh` orchestriert den Worker) und MC
  damit nur nachbauen würde, was jedes Rezept anders macht. Neuer
  Multi-Host-Code ist der riskanteste Teil; wir brauchen ihn nicht.
- **Rezept bindet feste Geräte** (`topology` = Liste konkreter Host-Slugs):
  Beschreibung — ein Rezept wüsste, auf welchen Boxen es läuft. → Verworfen
  weil das Gerätedaten in den Katalog trägt, der als Seed im Repo liegt, und
  weil derselbe Katalogeintrag bei jedem MC-Nutzer andere Boxen meint. Anzahl
  ins Rezept, Geräte an die Instanz.
- **Pflichtfeld als DB-`NOT NULL`** auf `runtimes.launch_command`:
  Beschreibung — die Datenbank erzwingt den Befehl. → Verworfen weil
  Cloud-Runtimes und LM Studio legitim keinen Befehl haben und die Migration
  bestehende Zeilen hätte zerbrechen oder mit Platzhaltern füllen müssen.
  Die Regel gehört dorthin, wo sie erklärt werden kann: in den Router, mit
  einem Satz als Antwort.
- **Startbarkeit im Frontend rechnen** (wie bisher `solo_capable`,
  `sparkrun_managed`): → Verworfen weil Frontend und Backend beim
  Runtime-Wechsel wiederholt uneinig waren. Ein Ort rechnet, einer zeigt.

## Konsequenzen

### Positiv
- **Die Beschwerde ist gelöst:** jede freigegebene Katalogzeile erscheint im
  Umschalter, unabhängig davon, wie sie startet. Grau hat immer einen Satz.
- **Ein Rezept-Begriff** statt drei. Neue Rezepte sind eine Katalogzeile mit
  Startbefehl, Port und Anzahl Boxen; kein Wrapper-Wissen nötig.
- **Generisch und Open-Source-tauglich:** kein Gerät, kein Modell, kein
  Werkzeug ist hart verdrahtet. Ein Nutzer mit zwei beliebigen Boxen kann
  Zweibox-Rezepte hinterlegen.
- **Kein zweiter Lebenszyklus:** der Rezept-Start ruft denselben
  `start_runtime` wie Box-Wizard und Runtimes-Seite. Verdrängung, Label-
  Verifikation und der Prozess-Check aus ADR-059 gelten weiter.
- **Ehrlicher Zustand:** „läuft" nur bei bestandener Health-Probe; ein
  Duo-Start in P1 wird abgelehnt statt still zu scheitern; eine Box ohne SSH
  sagt „MC kann hier nichts starten".
- Weniger Code: Registry-Abfrage per SSH, GPU-Zählung, TP-Override und der
  zweite Umschalter sind weg.

### Negativ
- **Zweibox-Start ist noch nicht da** (P3). Bis dahin ist `fit: "duo"` eine
  Ankündigung: die Liste zeigt es, der Start-Knopf antwortet 409.
- **`hosts.role` fehlt noch** (P2). `candidate_workers[].role` ist heute
  immer `null`; das Feld steht nur schon im Schema, damit die Oberfläche
  später nichts nachrüsten muss.
- **Exklusiv-Heuristik über `min_vram_gb`:** eine aus dem Katalog angelegte
  Instanz bekommt `exclusive_memory = (min_vram_gb is not None)`. Ein Rezept
  ohne VRAM-Angabe verdrängt also nichts und wird nicht verdrängt. Das ist
  eine Annahme, kein Wissen; ein explizites Katalogfeld wäre sauberer.
- **Umwandlung statt Löschung** heisst: alte sparkrun-Zeilen tragen jetzt
  `vllm_docker` mit einem generierten Startbefehl. Wer den Wrapper deinstalliert,
  hat Katalogzeilen, die auf `uvx` zeigen — sie melden dann beim Start einen
  Fehler, nicht vorher.
- **Port-Kollision wird nur auf dem Head geprüft** und nur gegen laufende
  Instanzen. Zwei Rezepte mit gleichem Port auf der Worker-Box sieht P1 nicht.
- **Startbefehl-Pflicht per Router** heisst: eine alte Zeile ohne Befehl kann
  nicht mehr aktiviert oder an eine Box gebunden werden, bis jemand den Befehl
  nachträgt. Das ist gewollt, aber es überrascht beim ersten PATCH.
- Der Startup-Reparaturlauf ist ein weiterer Lifespan-Schritt, der bei jedem
  Backend-Start läuft (idempotent, ein SELECT bei leerem Ergebnis).

### Offen (Phasenplan v2, Konzept 02.09.2026)
| Phase | Inhalt | Stand |
|---|---|---|
| P0 Daten | `topology` + `port` am Katalog (Migration 0191), Katalog nachtragen | ✅ #388 |
| P1 Sichtbar + sparkrun raus | eine Schnittstelle, beide Umschalter, Pflicht-Startbefehl, Umwandlung | ✅ Backend #388 · Frontend #381 |
| P2 Rolle + SSH | `hosts.role` (head/worker), Box-Wizard erfasst Rolle + SSH für jede Box | offen |
| P3 Duo-Start | Worker-Wahl beim Start, MC schreibt `HEAD_IP`/`WORKER_IP` in die `.env` des Rezepts, Schreibpfad `runtime_hosts`, Verdrängung über alle Mitglieder | offen |
| P4 Vorflug | RAM/Disk-Prüfung über alle Ziel-Boxen aus der Telemetrie | offen |

Zu P3, geprüft am 02.09.: Das Start-Skript des Zweibox-Rezepts lädt seine
`.env` **vor** den Standardwerten (`set -a`, dann `HEAD_IP="${HEAD_IP:-…}"`).
Steht der Wert in der `.env`, gewinnt die `.env` über die Umgebung. MC muss
die zwei Schlüssel deshalb **in die `.env` des Rezepts schreiben** (nur diese
zwei, idempotent), nicht als Umgebungsvariable mitgeben.

## Referenzen

- Betroffene Dateien:
  `backend/app/models/local_recipe.py` (`ENGINES`, `LEGACY_ENGINE_SPARKRUN`, `topology`, `port`),
  `backend/alembic/versions/0191_recipe_topology_port.py`,
  `backend/app/services/recipe_switcher.py` (`list_host_recipes`, `start_recipe_on_host`, `build_runtime_from_recipe`, Gründe als Sätze),
  `backend/app/services/local_registry.py` (`normalise_legacy_engine`, `repair_legacy_sparkrun_rows`, `RecipeSpec.normalised`),
  `backend/app/routers/host_recipes.py`,
  `backend/app/routers/runtimes.py` (`_require_launch_command`, `LAUNCH_COMMAND_REQUIRED`),
  `backend/app/main.py` (Router-Registrierung, Startup-Reparatur),
  `frontend-v2/src/components/shared/HostRecipeSwitcher.tsx`, `frontend-v2/src/lib/api.ts` (`hosts/{id}/recipes`)
- Entfernt: `backend/app/services/sparkrun_manager.py`, `POST /runtimes/{id}/switch-recipe`,
  `sparkrun_managed`-Gate, `frontend-v2/src/components/shared/SparkRecipeSwitcher.tsx`
- Commits: `3c839843` — feat(rezepte): ein Rezept-Modell — Katalog mit Topologie/Port, Umschalter-API, sparkrun-Rückbau (P0/P1 Backend) (#388); Frontend #381
- Vertrag: `docs/plans/2026-09-02-rezept-umschalter-vertrag.md`
- Verwandte ADRs: ADR-036 (`launch_command`, wird hier zur Pflicht), ADR-048 (Host-Registry, `resolved_host_from_row`),
  ADR-054 („Engine leads, MC follows" — `running` nur aus der Probe), ADR-059 (superseded; Prozess-Check `verify_spark_vllm_process_started` bleibt), ADR-057 (Engine Control)
- Skill für neue Rezepte: `~/.claude/skills/mc-recipe-integration/SKILL.md`
