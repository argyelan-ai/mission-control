# ADR-078 — Slot-Runtime: Agenten hängen an der Box-URL, nicht am Rezept

**Status:** Accepted
**Datum:** 2026-09-05
**Scope:** Backend/Runtime · Backend/DB · Backend/API · Backend/Dispatch · Docker/Agent-Images · Frontend/Runtimes
**Baut auf:** ADR-054 (Runtime-Wächter, „Engine führt, MC folgt"), ADR-077 (Ein Rezept-Modell), ADR-048 (Host-Registry), ADR-049 (omp-Bridge)

## Kontext

Auf einer GPU-Box läuft immer genau EIN grosses Modell, und alle Rezepte dieser
Box hören auf **derselben URL** (Konvention: Port 8000). Der Rezept-Umschalter
(ADR-077) tauscht also das Modell hinter einer unveränderten Adresse.

Ein Agent hängt heute an genau EINER Runtime-Zeile (`agents.runtime_id`). Aus
ihr rendert der Bootstrap `OPENAI_BASE_URL` und `OPENAI_MODEL`
(`routers/internal.py`). Jeder Rezept-Start legt aber eine EIGENE Runtime-Zeile
an (`recipe_switcher.build_runtime_from_recipe`). Daraus folgt der Fehler:

> Nach einem Rezeptwechsel fragt der Agent weiter den Modellnamen des ALTEN
> Rezepts an. Die Engine kennt ihn nicht mehr und antwortet 404.

Live belegt am 05.09.2026: zehn Runtime-Zeilen zeigten auf dieselbe Box-URL,
nur zwei davon waren eingeschaltet — und mehrere Agenten hingen an
stillgelegten Zeilen. Der einzige bisherige Ausweg war ein
Agenten-Runtime-Switch (`agent_runtime_switch`), also ein Container-Neustart
mit Health-Frist, Rollback-Pfad und Trust-Dialog — teuer und fragil, und
eigentlich für etwas anderes gebaut (Agent bewusst auf eine ANDERE Box oder in
die Cloud umhängen).

Zwei Bausteine waren schon da und wurden nur nicht zusammengesteckt:

* Der Drift-Wächter folgt einer **ankerlosen** Runtime-Zeile bewusst: hat eine
  Zeile weder Container- noch Prozessnamen, schreibt er das servierte Modell
  (und das Kontextfenster) in die Zeile. Im Code steht wörtlich
  *„drift IS the feature"* (`runtime_watcher._served_answer_is_own`).
* Der omp-Container liest den Modellnamen bei JEDEM Task neu aus einer Datei
  (`launch-omp.sh` liest `omp.env`, die Bridge respawnt Window 0 pro Task) —
  ein Container-Neustart ist für einen Modellwechsel gar nicht nötig.

## Entscheidung

**Je Head-Box gibt es EINE „Slot"-Runtime-Zeile. Agenten hängen dort, nicht am
Rezept.** Die Zeile ist der Platzhalter für das, was die Box gerade serviert;
der Wächter und der Umschalter schreiben hinein, WAS das ist.

Konkret:

1. **Explizites Kennzeichen `runtimes.is_slot`** (Migration 0194, BOOLEAN NOT
   NULL DEFAULT false). Alle Sonderregeln hängen an diesem Feld, nie an einem
   Runtime-Typ und nie an einer Konvention.
2. **Vertrag einer Slot-Zeile:** `runtime_type = "openai_compatible"`, KEIN
   `container_name`/`process_name`/`launch_command`/`stop_command`,
   `exclusive_memory = false`, `autostart_supported = false` — und sie
   **behält ihre `host_id`** (die Head-Box).
3. **Ausschlüsse überall, wo `is_slot`:** nie Instanz eines Rezepts
   (`recipe_matches_runtime`), nie Belegung einer Box
   (`recipe_switcher._load_fleet`), nie Verdrängungsopfer
   (`runtime_manager._ensure_exclusive_host`), nie Autostart- oder
   Auto-Recovery-Ziel (`runtime_watcher._autostart_target`,
   `_maybe_auto_recover`); der Drift-Wächter folgt ihr dagegen IMMER
   (`_served_answer_is_own` → `True`), Modell wie Kontextfenster.
4. **Übergangs-Marker + Sofort-Schreiben:** `start_recipe_on_host` setzt den
   Grace-Marker (`runtime_grace.mark_switching`) zusätzlich auf die Slot-Zeile
   der Head-Box und schreibt nach einem erfolgreichen Start Modell und Fenster
   sofort hinein.
5. **Bereitschafts-Tor:** `dispatch_delivery._check_runtime_readiness` verschiebt
   die Zustellung, solange die Slot-Zeile in Grace ist oder die letzte Probe
   `reachable = false` sah — mit Ereignis `dispatch.deferred_runtime_loading`
   und einer Warnung nach 45 Minuten. Das Alter kommt aus einem **eigenen
   Wartezähler je Aufgabe** (`mc:dispatch:defer:<task_id>`), nicht aus dem
   Grace-Marker: der lebt nur 20 Minuten, die Warnung hätte also nie gefeuert.
   Derselbe Zähler drosselt das Ereignis auf höchstens eines alle 5 Minuten.
6. **Reload statt Neustart:** `runtime_propagation._sync_one` rendert die
   omp-Konfiguration IM Container neu (`docker exec … render-omp-config.sh`);
   `docker restart` bleibt der Rückfallweg.
7. **Kein Crash-Loop ohne Modell:** die Agenten-Entrypoints brechen nicht mehr
   sofort ab, wenn `OPENAI_MODEL` fehlt, sondern fragen den Bootstrap bis zu
   30 Minuten lang erneut.
8. **Daten sind Instanz-Sache:** die Migration legt KEINE Zeilen an. Der
   Backend-Start führt `slot_runtimes.ensure_slot_runtimes()` aus (idempotent,
   wie `repair_legacy_sparkrun_rows`) und hängt die cli-bridge-Agenten der Box
   um. Rückweg: `backend/scripts/slot_rollback.py` **plus** der Schalter
   `SLOT_RUNTIMES_ENABLED=false` — ohne ihn legte der nächste Backend-Start
   alles wieder an, der Rückweg hätte also nur bis zum nächsten Deploy gehalten.
9. **Der Wechsel bleibt ein Wechsel, auch wenn er lange dauert:** solange die
   Rezept-Instanz in Grace ist, erneuert der Wächter den Marker der Slot-Zeile
   (`runtime_watcher._refresh_slot_grace`). Beide Marker sterben damit
   gleichzeitig. Ohne das feuerte ein ehrlicher 30-Minuten-Kaltstart ab
   Minute 21 `runtime.unreachable` für die Slot-Zeile.

## Warum `host_id` bleibt (und nicht NULL wird)

Der Architektur-Review schlug `host_id = NULL` vor, weil eine Slot-Zeile dann
automatisch aus `_load_fleet` fällt. Dagegen steht ein härterer Befund:
`routers/internal.py` rendert die langen omp-Zeitgeber
(`OMP_TURN_IDLE_TIMEOUT = 1800`, `OMP_TASK_DEADLINE = 7200`) für
`openai_compatible` **nur bei gesetzter `host_id`** — eine boxlose Slot-Zeile
fiele auf den 300-Sekunden-Watchdog zurück, genau den Killer, der am 03./04.09.
lange lokale Züge abgeschnitten hat. Ausserdem wäre „Gruppe: ohne Box" in der
Oberfläche eine Lüge: die Zeile IST eine Box.

Der Preis dafür ist, dass die Ausschlüsse explizit gebaut werden müssen statt
sich aus einem NULL zu ergeben. Genau dafür gibt es `is_slot`.

## Verworfene Alternativen

**A — Alias-Name (`--served-model-name spark`).** Jedes Rezept serviert
zusätzlich unter einem festen Aliasnamen, der Agent fragt immer den Alias an.
Verworfen: `agent_runtime_switch.select_probed_model` behält ein weiterhin
serviertes Modell bewusst bei — der Alias würde für immer kleben und ein
echter Wechsel nie mehr erkannt. Ausserdem verliert die Datenbank die
Modell-Wahrheit (Katalog, Kosten, Badge sehen nur noch „spark"), und jedes
Rezept müsste einzeln angefasst werden.

**C — MC als `/v1`-Proxy.** Das Backend nimmt die Anfragen an und schreibt den
Modellnamen um. Verworfen: MC würde zum Single Point of Failure für jede
einzelne Token-Ausgabe, mit Streaming-Latenz über einen zusätzlichen
Docker-Hop — und einem `/v1`-Pfad, den es heute gar nicht gibt.

**„Agent an die Box binden" (`agents.host_id`).** Verworfen: `agents` hat heute
kein `host_id`; es bräuchte eine neue Spalte plus eine Auflösungsschicht an
jeder Stelle, die heute `runtime_id` liest. Und die naheliegende Quelle
„aktuelles Rezept der Box" (`hosts.autostart_recipe_slug`) ist bewusst eine
verzögerte Wahrheit (ADR-077/#422: sie folgt erst der bestätigten Antwort) —
also unbrauchbar genau in der Übergangszeit, um die es hier geht.

## Konsequenzen

**Gut**
* Ein Rezeptwechsel braucht keinen Agenten-Runtime-Switch mehr. Der Switch
  bleibt für das, wofür er gebaut ist: andere Box, andere Cloud.
* Stillgelegte Rezept-Zeilen können keinen Agenten mehr mit in den Ruhestand
  nehmen.
* Der Katalog-Badge („läuft") wird ehrlicher: die Slot-Zeile matcht kein
  Rezept mehr und kann darum keine fremde Zeile grün färben.
* Eine Aufgabe, die in ein Ladefenster fällt, wartet sichtbar, statt in ein
  totes Modell zu laufen.

**Preis / Risiken**
* Ein Wechsel flaggt ab sofort ALLE Agenten der Box zum Modell-Abgleich (vorher
  ein bis zwei). Deshalb ist der In-Container-Reload (Punkt 6) keine Kür,
  sondern Voraussetzung — `docker restart` mit bis zu 60 s Health-Wartezeit je
  Agent wäre bei sechs Agenten eine Viertelstunde Stillstand.
* Wird eine Slot-Zeile gelöscht oder abgeschaltet, stehen alle Agenten der Box
  ohne Runtime da. Punkt 7 (kein sofortiger Abbruch im Entrypoint) macht daraus
  ein Warten statt eines Crash-Loops.
* Der Wächter schreibt jetzt in eine Zeile, an der viele Agenten hängen. Der
  Probe-Guard bleibt für alle ANDEREN Zeilen unverändert scharf — nur die
  Slot-Zeile darf folgen.

**Rückweg — zwei Schritte**
1. `SLOT_RUNTIMES_ENABLED=false` in die `.env`, Backend neu starten. Ohne diesen
   Schritt legt `ensure_slot_runtimes()` beim nächsten Start alles wieder an.
2. `docker compose exec backend python -m scripts.slot_rollback` hängt jeden
   Agenten zurück an die Rezept-Zeile seiner Box (bevorzugt die laufende, sonst
   die aus dem Ereignis `agent.slot_rebound`) und löscht die Slot-Zeilen.
   `--dry-run` zeigt den Plan, `--keep-rows` lässt die Zeilen stehen.

Das Skript ist **alles oder nichts**: findet sich für auch nur einen Agenten
kein Ziel, bricht es ab, bevor es irgendetwas schreibt (Exit-Code 2), und eine
Slot-Zeile wird nur gelöscht, wenn nachweislich kein Agent mehr an ihr hängt.
Die Rezept-Zeilen selbst werden nie angefasst, es geht also nichts verloren.
