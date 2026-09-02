#!/bin/bash
# latency-tune.sh — Latenz-Abstimmung für den TP-Verbund über Netz:
# PCIe-ASPM auf "performance" und CPU-Tiefschlaf (tiefe C-States) sperren.
#
# Der zweite Teil braucht einen HALTE-PROZESS: solange jemand
# /dev/cpu_dma_latency mit dem Wert 0 offen hält, bleibt die CPU wach. Stirbt
# der Prozess (oder die Box startet neu), fällt die Einstellung STILL zurück —
# deshalb prüft der Agent den Prozess (Kennung "cpu_dma_holder" in der
# Kommandozeile) und nicht nur die ASPM-Datei.
#
# Idempotent: ist beides schon aktiv, wird NICHTS neu gestartet (Exit 0).
# Sonst würde jeder Aufruf einen weiteren Halter erzeugen. Scheitert einer
# der beiden Schritte, endet das Skript mit Exit 1 und einer Meldung — der
# Agent zeigt sie als last_error.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Kein Rückweg-Skript: ein Neustart
# räumt beides weg.
set -u
ASPM=/sys/module/pcie_aspm/parameters/policy
DMA=/dev/cpu_dma_latency
HOLDER_TAG=cpu_dma_holder

if [ "$#" -ne 0 ]; then
  echo "abgelehnt: keine Argumente erwartet" >&2
  exit 2
fi

holder_running() {
  # Muster mit Klammer, damit der pgrep-Aufruf selbst nie auf sich passt.
  pgrep -f "cpu_dma_holde[r]" >/dev/null 2>&1
}

aspm_ok=0
if [ -w "$ASPM" ]; then
  if grep -q '\[performance\]' "$ASPM" 2>/dev/null; then
    aspm_ok=1
  elif echo performance > "$ASPM" 2>/dev/null; then
    aspm_ok=1
  fi
fi
if [ "$aspm_ok" -ne 1 ]; then
  echo "ASPM-Richtlinie konnte nicht auf performance gesetzt werden ($ASPM)" >&2
  exit 1
fi

if ! holder_running; then
  if [ ! -w "$DMA" ]; then
    echo "$DMA nicht beschreibbar — C-State-Sperre unmöglich" >&2
    exit 1
  fi
  # Halter: eigene Sitzung (setsid), kein Terminal, überlebt das Ende dieses
  # Skripts. Die Zeile "# cpu_dma_holder" im Python-Code ist die Kennung,
  # nach der Agent und pgrep suchen.
  setsid nohup python3 -c "
import struct, time
f = open('$DMA', 'wb', buffering=0)
f.write(struct.pack('i', 0))
# $HOLDER_TAG
while True: time.sleep(3600)
" > /dev/null 2>&1 < /dev/null &
  sleep 1
  if ! holder_running; then
    echo "Halte-Prozess ($HOLDER_TAG) ist nicht gestartet" >&2
    exit 1
  fi
fi

echo "ASPM: $(cat "$ASPM") | Halter läuft"
