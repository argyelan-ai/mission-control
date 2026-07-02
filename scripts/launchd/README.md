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
ln -sf "$(pwd)/scripts/memory-sampler.sh"     ~/.mc/memory-sampler.sh
ln -sf "$(pwd)/scripts/poll-health-check.sh"  ~/.mc/poll-health-check.sh

# 3. launchd Agents laden
launchctl load ~/Library/LaunchAgents/com.mc.memory-sampler.plist
launchctl load ~/Library/LaunchAgents/com.mc.poll-health.plist

# 4. (Nur falls Boss-Host noch nicht installiert) Boss-Runtime ueber separaten Installer
scripts/install-boss-host.sh
```

Verifizieren:

```bash
launchctl list | grep 'com.mc\.'
# Sollte zeigen: com.mc.memory-sampler und com.mc.poll-health
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

## Stoppen / Deinstallieren

```bash
launchctl unload ~/Library/LaunchAgents/com.mc.memory-sampler.plist
launchctl unload ~/Library/LaunchAgents/com.mc.poll-health.plist
rm ~/Library/LaunchAgents/com.mc.memory-sampler.plist
rm ~/Library/LaunchAgents/com.mc.poll-health.plist
```

Die Scripts selbst (`scripts/memory-sampler.sh`, `scripts/poll-health-check.sh`)
bleiben im Repo und können bei Bedarf manuell aufgerufen werden.

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
