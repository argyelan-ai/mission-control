# Runtimes-Seite v2 „Die Bühne" — Spezifikation (Stand 06.09.2026, von Mark abgenommen)

Mockup-Artefakt (lebende Referenz für Optik): https://claude.ai/code/artifact/4796f97e-2294-41e3-a74f-e2e35e86086b
Gilt für den Tab **Fleet** der Seite `/runtimes`. Tabs Cloud · Models · Infrastructure bleiben unverändert.
UI-Texte sind i18n-Schlüssel (EN + DE), nie hart kodiert.

## 1. Grundidee
Ein laufendes Modell = **eine Karte („Bühne")**. Die Boxen, auf denen es läuft (Head + Worker beim Duo), sind **Mitglieder** dieser Karte. Es gibt keine Kachel je Box mehr. Eine Box ohne Modell ist eine **freie Karte**, eine ausgeschaltete Box eine **Blaupause**.

## 2. Anatomie der Bühne — Zonen in fester Reihenfolge, nie ersetzt
| Zone | Inhalt | Verhalten |
|---|---|---|
| 0 Lauflicht | Heller Kopf mit ausblendendem Schweif (4 Lagen, Deckung 1/.5/.25/.1) zieht um die **Kartenkante**. SVG-Rect-Lagen, Bewegung per rAF-JS (kein CSS `pathLength`, Safari). | 7 s/Runde bei Arbeit, 16 s im Leerlauf (kein Token in 5 min), ocker beim Wechsel, rot gestrichelt + still bei Störung, kein Licht bei frei/aus. `prefers-reduced-motion`: still, gedimmt. Nur animieren, wenn sichtbar (IntersectionObserver). |
| 1 Lebenszeichen | Zustandspunkt (8 px, eckig) + Modellname (Clash Display, 22–30 px) · rechts oben Laufzeit in Mono („up 2 h 41"). Darunter **Wärmestreifen**: 60 Zellen à 15 s = 15 min, Helligkeit = Token/s (Akzent, Opacity .08–1). Darunter EINE Mono-Zeile „38 tok/s · duo · 7 ms". | Tot: Zellen rot; Wechsel: Zellen gedimmt, Zeile „switching to <Modell>", Ecke = verstrichene Zeit. **Keine Sätze, keine Meldungstexte** auf der Karte (gehören in MC-Notifications). |
| 2 Instrumente | Vier gleiche Zellen: **Context** (max_context_len) · **Speed solo** (tok/s aus Bench-Standard je Rezept, „–" wenn unbekannt) · **Agents** (Anzahl gebundener Agenten) · **Endpoint** (`:8000` + Copy-Icon, Untertitel „slot"). | 2 Spalten unter 600 px Kartenbreite, 4 darüber. Desktop: Hairlines zwischen Zellen. |
| 3 Mitglieder | Je Box: Punkt + Name + Rolle (Mono uppercase) · GPU-Balken · RAM-Balken · Modus-Vierer (eco+ · eco · normal · boost, kompakt) · Temperatur rechts. | Nebeneinander ab 600 px, sonst gestapelt. Balken-Animation nur `transform: scaleX`. |
| 4 Aktionen | **Switch model** (primär, Akzentfläche) · Zahnrad (öffnet Cockpit) · **Stop** (Ghost, roter Text). Sonst nichts. | Handy: primär volle Breite, Stop volle Breite darunter. Während Wechsel: Phasen-Leiste evict · launch · load ersetzt die Knöpfe, „Cancel" bleibt. Störung: primär = „Restart now", zweite Reihe „Other model" + Stop. |

Kein „+ Model" (Umschalter kann dasselbe). Kein Autostart-Schalter vorne. Kein Seiten-Untertitel; Kopf der Seite = „Runtimes" + Plus-Icon-Knopf (Add runtime).

## 3. Weitere Karten-Zustände
- **Leer (nichts läuft, beide Boxen frei):** eine Karte, Kopfzeile „No model · 2 boxes ready", Einladung „Nothing is running" + „Start model", Zone 3 mit beiden Boxen (graue Punkte), Zone 4 = „Recipes ▾" + Zahnrad.
- **Solo:** Bühne mit einem Mitglied + darunter **freie Karte** je freier Box.
- **Freie Karte (Box läuft, kein Modell) = Kapazitäts-Ausweis:** gleiche Zonen. Titel = Boxname, grauer Punkt, Ecke „free"; Wärmestreifen = 60 leere Zellen (bg-hover); Mono-Zeile „no model · ready · 3 ms"; Instrumente = **Memory free** · **Recipes fit** (Rezepte, deren Speicherbedarf passt) · **Agents** · **Endpoint · idle**; Zone 3 = die Box (Name/Rolle, Balken, Modus, Temp); Zone 4 = „Start model" (primär) + Zahnrad. Kein Lauflicht.
- **Blaupause (Box aus/schläft):** gestrichelter Rahmen, Ecke „asleep", Zeile „powered off · last seen 22:40", Instrumente „–", Balken leer, Modus gesperrt (Opacity .5), Zone 4 = „Wake" + Zahnrad.

## 4. Cockpit (Zahnrad) — je Box
Desktop: Drawer von rechts, 420 px. Handy: Bottom-Sheet, volle Breite. Kopf: Boxname (Clash 20 px) + Mono-Fakten (Rolle · GB10 · IP · fabric · up) + Schliessen. Gruppen mit Anatomie [Label (Mono, 92 px) | Inhalt], getrennt durch Hairlines, **keine Boxen in Boxen**:
1. **Telemetry:** SVG-Diagramm 1 h: GPU (Akzentlinie + Fläche .08), RAM (gestrichelt, t3), Temp (warning). Legende darunter.
2. **Mode:** Radio-Liste, 4 Zeilen à 48 px, Kopfzeile Mode · Power · Fan; Spalten rechtsbündig, tabular; aktive Zeile accent-subtle. **Keine Speed-Spalte, kein Auto.** Fusszeile „this box · now 62 °C · fan 38 %". Karte (Vierer) und Cockpit zeigen denselben Zustand.
3. **Autostart:** nur Schalter + Modellname (Mono). Darunter Zeitleiste der letzten Starts (Zeit · Punkt ok/err · Text) + Log-Pfad.
4. **Connection:** URL mit Copy-Icon + Chip „slot"; gebundene Agenten als Textlinks mit Zustandspunkt (working/idle).
5. **Recipe:** Fakten-Liste model · source (Repo @ Commit) · path · env · worker (IP, RoCE).
Fusszeile (bg-deep): **Re-probe · Restart · Stop** (Stop rechts, rot, Label „Stop · autostart off").

## 5. Stop ↔ Autostart (verifiziert am Code 06.09.)
Stop existiert (`POST /runtimes/{id}/stop`, `stop_ssh_process` mit Rezept-`stop_command`, Duo via `stop_multi_box_instance`), aber koppelt nicht an `hosts.autostart_enabled` → Wächter (`runtime_watcher._maybe_auto_recover`, `AUTO_RECOVERY_COOLDOWN=900`) holt das Modell zurück. **Neu:** erfolgreicher Stop einer host-gebundenen Runtime setzt `hosts.autostart_enabled=false`, wenn es an war, Event `host.autostart_disabled_by_stop`, Antwort enthält `autostart_disabled: true`. **Dispatch-Gate:** ist ein an diese Runtime/Slot gebundener Agent mit `current_task_id` beschäftigt → 409 `{agent, task}`; `?force=true` übersteuert.

## 6. Datenquellen
- **Puls (tok/s):** neuer Poller `runtime_pulse.py` (Lifespan-Task): alle 5 s `GET <endpoint-base>/metrics` der Head-Box (vLLM Prometheus: `vllm:generation_tokens_total`, Fallback SGLang `sglang:gen_throughput`/`generation_tokens_total`), Differenz/Zeit = tok/s, Ring 180 Punkte in Redis `mc:host:{host_id}:pulse` (List, LTRIM). Endpoint `GET /hosts/{host_id}/pulse` → `{points:[{t,tps}], now_tps, idle_seconds}`. Ohne Metrik: leere Punkte, `available:false`, nie 5xx. Nur pollen, wenn `hosts`-Runtime `reachable`.
- **Telemetrie-Verlauf:** bestehender 5-s-Poll von `GET /hosts/{id}/metrics` (Frontend) bleibt; neu Backend-Ring 1 h (720 Punkte, 5 s) `mc:host:{host_id}:metrics:history` aus dem gleichen Sammler wie `host_metrics`, Endpoint `GET /hosts/{host_id}/metrics/history` → `{points:[{t,gpu,ram_used,ram_total,temp,fan}]}`.
- **„Recipes fit":** Rezepte aus `GET /hosts/{id}/recipes`, deren `capacity`-Vorflug (P4, #428) passt.
- **Gebundene Agenten + „working":** `GET /runtimes/{slug}/agents` + `current_task_id`.
- **Speed solo:** aus Katalog-/Bench-Feld, wenn vorhanden; sonst „–".

## 7. Umsetzung — 3 Wellen, hinter Schalter `NEXT_PUBLIC_RUNTIMES_STAGE=v2` (Default v1 bis Abnahme)
| PR | Inhalt | Live-Gate |
|---|---|---|
| 1 Puls | `runtime_pulse.py`, Redis-Ring, `GET /hosts/{id}/pulse`, Settings `RUNTIME_PULSE_INTERVAL` (0 = aus), Tests mit Fake-Metrik | Bench gegen die Head-Box → Bursts sichtbar; Engine weg → leer, kein 500 |
| 2 Stop+Autostart | Kopplung + Event + Dispatch-Gate, Tests | Stop via API → `autostart_enabled=false`, 20 min kein Wächter-Start; Stop bei arbeitendem Agent → 409 |
| 3 Telemetrie-Verlauf | 1-h-Ring + Endpoint, Tests | nach 10 min 120 Punkte, Werte ≈ nvidia-smi |
| 4 Stage | `Stage`, `FreeBox`, `AsleepBox`, `FlowEdge`, `HeatStrip`, `KpiRow`, `ActionBar`; Zustände Duo/Solo/Leer/Wechsel/Störung; ersetzt SlotStage/WorkerTile im Fleet-Tab hinter dem Schalter; Vitest | Marks Klick-Abnahme Desktop + Handy; Umschalten über neue Karte; Stop → „Leer" |
| 5 Cockpit | `BoxCockpit` (Drawer/Sheet) mit 5 Gruppen + Fusszeile; nutzt PR 2/3 | Modus über Liste → Vierer folgt ≤ 10 s; Autostart über Cockpit → DB |
| 6 Schliff | Duo→Solo-Übergang (transform/opacity, 200 ms), i18n DE, alte Komponenten raus, v2 Standard | beide Sprachen ohne rohe Schlüssel |

Bauteile, die bleiben: `Meter` (auf scaleX umstellen), `HostRecipeSwitcher`, `PhaseIndicator`, `DeviceModeStrip` (kompakt), `api.runtimes.stop`, `grouping.ts` (workerOf), `RuntimeDetailPanel` (nur noch Cloud/Unassigned).
Design-Tokens ausschliesslich aus `lib/colors.ts`; Fonts wie im Repo (Clash Display, General Sans, JetBrains Mono).
