# ADR-072 — ChatAdapter: ein Kontrakt für den Team-Chat, Absender-Identität als erstklassiges Konzept

**Status:** Accepted
**Datum:** 2026-07-28
**Scope:** Backend/Services · Backend/Config · Backend/Tests

## Kontext

Der Team-Chat (Interaktionsmodell 2.0, P2.3/P2.4/P3.1) ist auf Telegram
entstanden, und seine Regeln sind dort auch gewachsen — *in* den
Telegram-Modulen:

- `telegram_outbound.py` (182 Zeilen) enthielt fast ausschliesslich
  **kanal-neutrale** Politik: Schleifenschutz, Skip-Gründe (internes
  Dispatch-Briefing, Migrations-Seed), die Ping-Regel (@Mark / Frage / Approval
  / Review = laut), die Nachtruhe 23–07 in Operator-Zeitzone. Kanal-spezifisch
  waren genau zwei Zeilen: das Absender-Präfix und der `send_message`-Aufruf.
- `telegram_inbound.py` enthielt die Routing-Entscheidung (bekannter Raum → sein
  Thread, unbekannter Raum → **nicht raten**, sondern nachfragen, kein Raum →
  Allgemein-Chat = DM mit Boss) plus den Schleifenschutz beim Schreiben.
- `messaging.post_message`, `task_lifecycle`, `routers/tasks`,
  `routers/agent_task_status` und der Purge-Loop in `main.py` riefen Telegram
  **namentlich** auf.

Der Operator führt Slack als zweiten Kanal ein — perspektivisch als Hauptkanal,
Telegram soll per Schalter ruhiggestellt liegenbleiben. Ohne Schnitt hätte der
Slack-Adapter diese Regeln kopieren müssen: sechs Fan-out-Stellen im neutralen
Code, zwei Sätze Schleifenschutz, zwei Implementierungen der Nachtruhe. Jede
Regeländerung wäre danach an zwei Orten fällig gewesen — und beim ersten
Vergessen driftet der Chat auseinander (der Kanal, dessen einziger Zweck es ist,
dass Mark *nichts verpasst*).

Anforderungen: (a) ein zweiter Kanal darf **keine** kanal-neutrale Datei
anfassen müssen; (b) Telegram-Verhalten bleibt exakt gleich (Refactoring, kein
Funktionsumbau); (c) mehrere Kanäle gleichzeitig, einzeln abschaltbar,
sauberer Aus-Zustand; (d) ein Netz, das einen falsch gebauten Kanal *vor* dem
Rollout rot macht.

## Entscheidung

Ein **`ChatAdapter`**-Kontrakt nach exakt dem Muster der bestehenden
Host-Harness-Registry (ADR-064): `Protocol` + Registry-Dict + Lookup/Catalog —
plus ein parametrisierter TCK nach dem Muster von ADR-071. Bewusst dieselbe
Mechanik, keine zweite.

### 1. Der Kontrakt (`services/chat_adapter.py`)

Aufgenommen wurde nur, was Telegram heute leistet oder Slack absehbar braucht:

| Operation | Warum sie im Kontrakt ist |
|---|---|
| `is_enabled()` / `is_configured()` | Zwei Schalter, weil der Bestand zwei hatte: die Raum-Pflege lief am Feature-Flag allein, der Versand zusätzlich an Token+Chat-ID. Ein einziges Flag hätte eines der beiden Verhalten geändert. |
| `ensure_room(session, thread)` | Ein Gesprächsraum je Thread/Projekt (Telegram-Thema, künftig Slack-Channel). Idempotent, `None` = Kanal nicht bereit (degradieren, nie werfen). |
| `resolve_thread_for_room(session, room)` | Die Gegenrichtung. Kanal-spezifisch, weil die Raum-ID an einer kanal-eigenen Spalte hängt (`threads.telegram_topic_id`). |
| `handle_task_done(session, task)` | „Erledigt“ am Raum sichtbar machen (Telegram: `✓ …` + Thread schliessen). |
| `purge_rooms(older_than_days)` | Marks 30-Tage-Regel, pro Kanal ausgeführt. |
| `send(room, OutboundChatMessage)` | Der einzige wirklich kanal-spezifische Schritt der Ausgabe. |
| `mirror_message(session, message)` | Kanal-Einstieg in die neutrale Pipeline; `BaseChatAdapter` implementiert ihn fertig, ein Kanal muss ihn nicht anfassen. |
| `capabilities` | `sender_identity` + `rooms` — **nur** Fähigkeiten, auf die heute eine Kontrakt-Operation verzweigt. Knöpfe und Dateien wurden bewusst **nicht** aufgenommen: keine Chat-Operation nutzt sie (Approval-Buttons laufen ausserhalb, über `telegram_bot.send_approval_telegram`). Kein Kontrakt auf Vorrat. |

Nicht im Kontrakt: das Auspacken eingehender Payloads (Telegram-Voice/STT,
chat_id-Gate, künftig Slack-Signaturprüfung). Das ist pro Kanal so verschieden,
dass eine gemeinsame Signatur nur ein `dict` durchreichen würde — der Kanal ruft
stattdessen die neutrale Routing-Entscheidung `chat_inbound.route_inbound()`
auf, sobald er Raum + Text hat.

### 2. Absender-Identität ist ein eigenes Konzept, kein Formatierungsdetail

Der Kern der alten Schwäche stand in einer Zeile:
`text = f"{prefix}: {message.body}"`. Telegram sendet alles vom **selben Bot**,
also *konnte* der Agentenname dort nur Text sein. Slack kann pro Nachricht
Absendername und Avatar setzen.

Hätte der Kontrakt den fertigen String transportiert, wäre Telegrams technische
Grenze zur Gewohnheit aller Kanäle geworden — Slack hätte „Rex: fertig“ vom
MC-Bot gezeigt, statt eine Nachricht *von Rex*. Der Kontrakt führt Identität
deshalb als Wert (`ChatSender(kind, display_name, agent_id)`); die neutrale
Pipeline löst sie **einmal** auf, der Kanal rendert sie. Ein Kanal ohne
Identitäts-Fähigkeit **degradiert sie sichtbar** (Telegram: Präfix) statt sie zu
verlieren — dieses Gesetz prüft der TCK für jeden Adapter. Der Unterschied ist
operativ, nicht ästhetisch: Mark muss immer wissen, *wer* etwas gesagt hat.

Ein `ChatSender is None` heisst „der Kanal-Bot spricht selbst“ (Rückfrage bei
unbekanntem Raum) — genau das Verhalten, das der alte `_reply`-Pfad hatte.

### 3. Kanal-Schalter (`CHAT_CHANNELS`)

`settings.chat_channels` ist eine Komma-Liste (`"telegram"`, `"slack"`,
`"telegram,slack"`). Leer = keine explizite Auswahl → jeder registrierte Kanal
darf laufen. Darüber liegen die zwei Adapter-Flags. Zwei Selektoren:
`enabled_chat_adapters()` (Raum-Pflege) und `sendable_chat_adapters()`
(Versand). Damit ist der heutige Default **byte-gleich zum Bestand**: Telegram
hängt weiter an `TELEGRAM_TEAM_CHAT_ENABLED`. Telegram ruhigstellen =
`CHAT_CHANNELS=slack`; kein Code verschwindet. Kein aktiver Kanal ist ein
first-class Zustand: alle neutralen Einstiegspunkte werden zu No-ops — kein
Wurf, kein Fehler-Log (im TCK gegen `caplog` asserted).

### 4. Wo was jetzt wohnt

```
chat_adapter.py   Kontrakt + Registry + Schalter + Katalog
chat_outbound.py  neutral: Skip-Regeln, Identität, Ping-Regel, Nachtruhe, Fan-out
chat_inbound.py   neutral: Routing-Entscheidung, Allgemein-Chat, Schleifenschutz-Kwargs
chat_rooms.py     neutral: Fan-out für task-done + Purge-Tick
chat_telegram.py  Telegram: Schalter, Transporte, Präfix-Degradation, Raum-Mapping
telegram_*.py     unverändert in ihrer Aufgabe — jetzt hinter dem Adapter
```

Die Telegram-Module behalten ihre öffentlichen Funktionen:
`mirror_message_to_telegram` bleibt der dokumentierte Injektionspunkt (Fakes
ohne Netz), `telegram_topics.handle_task_done` bleibt der einzige Ort von Marks
Regel, welcher Themen-Besitzer ein `✓` bekommt.

### 5. TCK (`tests/test_chat_adapter_tck.py` + `tests/chat_harnesses.py`)

Parametrisiert über die **registrierten** Adapter. Jeder Adapter braucht eine
Harness (Fake-Transport, Raum-Bindung, „Transport kaputtmachen“) — ein
registrierter Adapter ohne Harness lässt den TCK **rot** werden, damit ein
Kanal nicht ungeprüft mitlaufen kann. Geprüfte Gesetze: Identität wird getragen
oder sichtbar degradiert · die neutralen Regeln werden benutzt statt nachgebaut
(Schleifenschutz + Ping/Nachtruhe werden *durch* `adapter.mirror_message`
geprüft) · nichts wirft bei totem Transport · unbekannter Raum wird nie geraten
· der Schalter trägt mehrere Kanäle und einen sauberen Aus-Zustand.

## Alternativen

- **Pro Kanal duplizieren** (Slack-Modul neben Telegram-Modul, beide von
  `post_message` aufgerufen): billigster erster Schritt, aber die Regeln lägen
  doppelt — Nachtruhe, Ping-Regel und Schleifenschutz sind genau die Stellen,
  an denen Drift nicht auffällt (ein stummer Ping meldet sich nicht). Zusätzlich
  hätte jede der sechs neutralen Fan-out-Stellen einen zweiten `if`-Zweig
  bekommen. Verworfen.
- **Telegram ersetzen statt abstrahieren** (Slack rein, Telegram raus): der
  Operator will Telegram ausdrücklich als ruhiggestellten Zweitkanal behalten
  (Mobil-Fallback, Sprachnachrichten-Ingest). Ausserdem hätte der Umbau die
  laufende Zustellung angefasst statt sie nur umzuhängen — Risiko ohne Not.
  Verworfen.
- **Nur den Sende-Aufruf abstrahieren** (ein `send_chat_message(text)`-Helfer):
  hätte den Präfix-String im Kontrakt zementiert und damit genau die Schwäche
  konserviert, die diese ADR beseitigt. Verworfen.
- **Ein einziges Aktiv-Flag statt `is_enabled`/`is_configured`**: hätte eines
  der beiden bestehenden Gates geändert (Raum-Pflege lief ohne Credential-Check)
  — also eine Verhaltensänderung im Refactoring. Verworfen.
- **Inbound-Payload-Parsing in den Kontrakt** (`ingest(payload)`): Telegram
  bringt Voice+STT und ein chat_id-Gate mit, Slack Signaturprüfung und Events —
  eine gemeinsame Signatur wäre ein durchgereichtes `dict` ohne Aussage.
  Verworfen zugunsten „Kanal parst, neutral routet“.

## Konsequenzen

### Positiv
- Ein zweiter Kanal = eine Adapter-Datei + ein Registry-Eintrag + eine
  Test-Harness. Keine neutrale Datei wird angefasst.
- Absender-Identität ist Daten; ein Kanal, der mehr kann als Telegram, darf mehr
  zeigen, ohne dass irgendwo ein String umgebaut wird.
- Mehrere Kanäle gleichzeitig sind der Normalfall des Fan-outs, nicht ein
  Sonderfall (ein ausfallender Kanal reisst die anderen nicht mit — getestet).
- Die Regeln (Schleifenschutz, Ping, Nachtruhe, „nie raten“) existieren genau
  einmal und werden für jeden Kanal *durch den TCK* eingefordert.
- Telegram-Verhalten unverändert: alle bestehenden Telegram-Tests bleiben grün,
  **ohne** dass eine davon angefasst wurde.

### Negativ
- Fünf neue Module statt zwei — mehr Dateien, mehr Sprünge beim Lesen. Der
  Modulkopf jeder Datei sagt darum explizit, was neutral und was kanal-eigen ist.
- `post_message(..., mirror_to_telegram=...)` heisst weiter so, obwohl der
  Parameter kanal-neutral gemeint ist. Umbenennen hätte Tests und Aufrufer
  angefasst — bewusste Kompatibilitätsschuld, im Docstring markiert. Gleiches
  gilt für `main._telegram_topic_purge_loop` und seine Konstanten.
- `TelegramChatAdapter.mirror_message` überschreibt die neutrale Default-
  Implementierung, nur um durch `telegram_outbound.mirror_message_to_telegram`
  zu laufen (Injektionspunkt der Bestandstests). Ein neuer Kanal soll das
  **nicht** nachahmen — die Basisklasse ist der Normalfall.
- Der TCK hängt an handgeschriebenen Fake-Transporten, nicht an aufgezeichneten
  Live-Antworten wie der Pane-TCK (ADR-071). Eine Slack-API-Änderung fängt er
  nicht; er fängt Kontraktbruch. Bewusster Unterschied: eine HTTP-API bricht
  anders als ein TUI-Pane.

### Wo künftig aufpassen
- Wer eine neutrale Regel ändert, ändert sie in `chat_outbound`/`chat_inbound` —
  **nicht** in einem Adapter. Ein Adapter, der Regeln nachbaut, fällt im TCK auf
  (die Sabotage-Probe hat genau das verifiziert).
- Wer einen Kanal registriert, muss ihm eine Harness geben, sonst ist der TCK rot.

## Referenzen

- Betroffene Dateien: `backend/app/services/chat_adapter.py`,
  `chat_outbound.py`, `chat_inbound.py`, `chat_rooms.py`, `chat_telegram.py`,
  `telegram_outbound.py`, `telegram_inbound.py`, `messaging.py`,
  `task_lifecycle.py`, `routers/tasks.py`, `routers/agent_task_status.py`,
  `main.py`, `config.py`, `backend/tests/test_chat_adapter_tck.py`,
  `backend/tests/chat_harnesses.py`
- Verwandte ADRs: ADR-064 (HostHarnessAdapter — dasselbe Registry-Muster),
  ADR-071 (Adapter-Kontrakt + TCK — dasselbe Test-Muster), ADR-061
  (Jarvis-Telegram-Inbound — teilt sich den einen getUpdates-Poller)
- Kontext: Interaktionsmodell 2.0 (P2.3 Outbound-Spiegel, P2.4 Inbound-Ingest,
  P3.1 Themen-Lebenszyklus)
