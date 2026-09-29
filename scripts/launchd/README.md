# Host-Side launchd Agents

Diese plists laufen auf dem macOS Host des Operators (Mac Mini M4) via `launchd` und
starten periodisch Mission-Control-eigene Monitor-Scripts. Sie sind NICHT
Teil der Docker-Stack — das sind Host-native Schedules für Observability.

## Installation auf einem neuen Host

Einmalig nach Clone des Repos:

```bash
# 1. launchd plists nach ~/Library/LaunchAgents/ kopieren (nicht symlinken —
#    launchd akzeptiert keine Symlinks in diesem Pfad)
cp scripts/launchd/*.plist ~/Library/LaunchAgents/

# 2. Host-Pfad fuer Helper-Scripts via Symlink an Repo binden
mkdir -p ~/.mc
ln -sf "$(pwd)/scripts/memory-sampler.sh"         ~/.mc/memory-sampler.sh
ln -sf "$(pwd)/scripts/poll-health-check.sh"      ~/.mc/poll-health-check.sh
ln -sf "$(pwd)/scripts/docker-health-restart.sh"  ~/.mc/docker-health-restart.sh

# 3. launchd Agents laden
launchctl load ~/Library/LaunchAgents/com.mc.memory-sampler.plist
launchctl load ~/Library/LaunchAgents/com.mc.poll-health.plist
launchctl load ~/Library/LaunchAgents/com.mc.docker-health-restart.plist

# 4. (Nur falls Boss-Host noch nicht installiert) Boss-Runtime ueber separaten Installer
scripts/install-boss-host.sh
```

Verifizieren:

```bash
launchctl list | grep 'com.mc\.'
# Sollte zeigen: com.mc.memory-sampler, com.mc.poll-health und com.mc.docker-health-restart
```

## Was jeder Agent macht

### `com.mc.memory-sampler`
- **Script:** `scripts/memory-sampler.sh`
- **Interval:** 30 min
- **Zweck:** Container-RAM-Snapshot als CSV für Memory-Leak-Investigation
- **Output:** `~/.mc/memory-samples.csv`
- **Auswerten:** siehe Memory-Notiz `project_memory_sampler_check.md`

### `com.mc.poll-health`
- **Script:** `scripts/poll-health-check.sh`
- **Interval:** 5 min
- **Zweck:** Alert-System für silent-failures in Boss-Host `poll.sh` — wenn
  Shell-Escape-Fehler, `mc command not found` oder DB-Integrity-Errors
  im poll.log auftauchen, Telegram-Alert an den Operator via Reports-Bot.
- **State:** `~/.mc/poll-health-state` (Cooldown 1h pro Error-Pattern)
- **Log:** `~/.mc/poll-health.log`
- **Context:** angelegt 2026-04-23 nach Bug C Incident — 2 Wochen Drift unbemerkt

### `com.mc.docker-health-restart`
- **Script:** `scripts/docker-health-restart.sh`
- **Interval:** 60s
- **Zweck:** Positivlisten-basierter Host-Waechter fuer unhealthy Docker-
  Services (`cdp-browser`, `playwright-mcp`). Entprellt in beide Richtungen:
  ein einzelner unhealthy-Tick loest noch keinen Versuch aus (erst 2 in
  Folge), und ein einzelner gesunder Tick raeumt einen laufenden Incident
  noch nicht weg (erst 3 in Folge gilt er als vorbei) — sonst faengt ein
  flatternder Service nach jedem kurzen Erholen wieder bei Versuch 1 an. Bei
  unhealthy: `docker restart` mit Exponential-Backoff (120s → 240s), max. 3
  Versuche, danach Stopp + genau eine Telegram-Meldung — die aber erst als
  zugestellt gilt, wenn sie nachweislich ankam (bis zu 5 Retries bei
  Telegram-Ausfall, danach verstummt sie geloggt statt endlos nachzubohren).
  **mc-agent-\* Container werden per Hard-Guard nie angefasst**, egal was in
  der Allowlist steht.
- **State:** `~/.mc/docker-health-restart-state/<service>.state` (Attempts,
  letzter Versuch, Notified-Flag, Given-up-Flag, Fehlversuche der
  Aufgeben-Meldung, aufeinanderfolgende unhealthy-/gesunde-Ticks — pro
  Service, reset erst nach 3 aufeinanderfolgenden gesunden Ticks)
- **Log:** `~/.mc/docker-health-restart.log`
- **Context:** angelegt 2026-09-13, nachdem der cdp-browser-Container 5 Tage
  `healthy` meldete waehrend die WebSocket-Verbindung haengen geblieben war
  (PR #544 behebt die Erkennung selbst — CDP/WS-Healthcheck statt nur HTTP;
  dieser Job schliesst die verbleibende Luecke: Erkennen allein heilt nicht,
  ohne diesen Job wuerde der Container weiter haengen bis ein Mensch
  eingreift). Bewusst GETRENNT von `com.mc.poll-health` (andere Concern:
  Docker-Container-Health statt Boss-Host-Log-Patterns; anderes Intervall:
  60s statt 5min). Ein autoheal-Sidecar mit `docker.sock`-Mount wurde
  verworfen — Vollzugriff auf den Docker-Host fuer einen haengenden Browser
  ist unverhaeltnismaessig. Der Host-seitige Weg via launchd braucht dagegen
  keine zusaetzlichen Container-Rechte (die Docker-Rechte liegen auf dem Host
  ohnehin) und ist deshalb normale Infrastrukturarbeit.
- **Zahlen-Begruendung:** 60s Intervall wie vorgegeben. 3 Versuche ueberleben
  transiente Ausreisser ohne einen wirklich kaputten Service endlos zu
  bearbeiten. 120s Basis-Backoff (verdoppelnd) gibt dem Healthcheck der
  Kandidaten (bis zu ~90s Settle-Zeit: interval 30s × retries 3) eine faire
  Chance vor dem naechsten Urteil. Genau 2 moegliche Meldungen pro Incident
  (Start + Give-up) statt einer pro Tick — Anlass war ein anderer Vorfall mit
  13 Wiederholungs-Meldungen in 2h. 2 aufeinanderfolgende unhealthy-Ticks vor
  dem ersten Versuch filtern einen einzelnen verpassten Healthcheck heraus.
  3 aufeinanderfolgende gesunde Ticks (> 120s Basis-Backoff) vor dem
  vollstaendigen Zuruecksetzen verhindern, dass ein flatternder Service nach
  jedem kurzen Erholen eine komplette neue Runde beginnt (Rex-Review PR #546,
  Runde 2 — ohne diese Entprellung waeren es bei einem alle 3 Ticks
  flatternden Service ~30 Meldungen/h statt maximal 2 pro Incident).
- **Bekannte Luecke (bewusst nicht Teil dieser Karte):** ohne diesen Job
  reagiert HEUTE niemand automatisch auf `unhealthy` — `restart:
  unless-stopped` in docker-compose.yml greift nur bei Prozess-Exit, nicht
  bei einem Healthcheck-Fail. Dieser Job schliesst genau diese Luecke fuer
  die beiden gelisteten Services.

## Stoppen / Deinstallieren

```bash
launchctl unload ~/Library/LaunchAgents/com.mc.memory-sampler.plist
launchctl unload ~/Library/LaunchAgents/com.mc.poll-health.plist
launchctl unload ~/Library/LaunchAgents/com.mc.docker-health-restart.plist
rm ~/Library/LaunchAgents/com.mc.memory-sampler.plist
rm ~/Library/LaunchAgents/com.mc.poll-health.plist
rm ~/Library/LaunchAgents/com.mc.docker-health-restart.plist
```

Die Scripts selbst (`scripts/memory-sampler.sh`, `scripts/poll-health-check.sh`,
`scripts/docker-health-restart.sh`) bleiben im Repo und können bei Bedarf
manuell aufgerufen werden.

## Docker-Health-Restart: Service zur Allowlist hinzufuegen

`ALLOWLIST=(...)` am Kopf von `scripts/docker-health-restart.sh` ist eine
**Positivliste** — nur was dort explizit steht, wird je automatisch
neugestartet. Vor dem Hinzufuegen eines Service pruefen:

1. Ist der Service **stateless** (kein Datenverlust bei Neustart)? Wenn nein
   (z.B. `db`, `redis`, `qdrant`) → NICHT hinzufuegen, blind-restart riskiert
   Datenverlust/Korruption.
2. Hat der Service einen eigenen `healthcheck:` in `docker-compose.yml`?
   Ohne Healthcheck liefert `docker inspect` `no-healthcheck` und der
   Watcher tut nichts (kein falscher Alarm, aber auch kein Schutz).
3. Ist es ein `mc-agent-*` Container? → Wird vom Hard-Guard im Script
   ohnehin ignoriert, unabhaengig von der Allowlist. Niemals versuchen zu
   umgehen — ein Agent mit laufendem Task darf nie automatisch neu gestartet
   werden.
4. `backend`/`frontend`/`caddy`/`mc-worker` laufen ueber den bestehenden
   Deploy-Workflow mit Backup + Record (siehe TOOLS.md "Deploy the Docker
   environment") — nicht hier duplizieren.

## launchd Refresh nach Script-Updates

Wenn `scripts/memory-sampler.sh` oder `scripts/poll-health-check.sh` im Repo
via `git pull` aktualisiert werden, ist **kein launchd reload nötig** — die
Symlinks in `~/.mc/` zeigen auf die Repo-Version, launchd führt beim nächsten
Interval den aktuellen Code aus.

Nur wenn die plist-Datei selbst sich ändert (z.B. `StartInterval` angepasst)
muss der Agent neu geladen werden:

```bash
cp scripts/launchd/com.mc.poll-health.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.mc.poll-health.plist
launchctl load ~/Library/LaunchAgents/com.mc.poll-health.plist
```
