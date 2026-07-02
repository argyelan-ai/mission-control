#!/usr/bin/env bash
# Memory-Sampler für Mission Control Container-Leak-Analyse.
#
# Läuft alle 30min via launchd (~/Library/LaunchAgents/com.mc.memory-sampler.plist)
# und appended docker stats in ~/.mc/memory-samples.csv.
#
# Auswerten:
#   awk -F, '$2 ~ /mc-agent-rex/' ~/.mc/memory-samples.csv
#   → zeigt RSS-Verlauf für Rex über Zeit
#
# Stoppen:
#   launchctl unload ~/Library/LaunchAgents/com.mc.memory-sampler.plist
#
# Angelegt 2026-04-23 für Container-Memory-Leak-Investigation.

set -euo pipefail

SAMPLES_CSV="$HOME/.mc/memory-samples.csv"
TS=$(date '+%Y-%m-%dT%H:%M:%S')

# Header schreiben wenn File neu
if [ ! -f "$SAMPLES_CSV" ]; then
  echo "timestamp,container,mem_used,mem_limit,mem_pct,cpu_pct" > "$SAMPLES_CSV"
fi

# docker stats --no-stream gibt einen Snapshot
# Format: "NAME  MEM_USAGE/LIMIT  MEM%  CPU%"
# Wir parsen das in CSV-Spalten. Nur mc-agent-* und mission-control-* Container.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

/usr/local/bin/docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}' 2>/dev/null \
  | grep -E '^(mc-agent-|mission-control-|festive_)' \
  | while IFS='|' read -r name memusage mempct cpupct; do
      # memusage Format: "178.6MiB / 6.769GiB" → split an " / "
      mem_used="${memusage% / *}"
      mem_limit="${memusage##* / }"
      # Kommas in Zahlen (falls lokal) auf Punkte normalisieren
      mem_used="${mem_used// /}"
      mem_limit="${mem_limit// /}"
      mempct="${mempct// /}"
      cpupct="${cpupct// /}"
      echo "$TS,$name,$mem_used,$mem_limit,$mempct,$cpupct" >> "$SAMPLES_CSV"
    done
