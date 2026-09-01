#!/bin/bash
# mc-gpu-mode.sh {boost|normal|eco|eco+|restore|status} — GPU-Takt-Deckel (GB10).
#
# Warum es das gibt: GB10 schaltet unter Dauerlast HART ab — kein Kernel-
# Panic, kein Log. Der Embedded-Controller kappt den Strom, bevor das System
# etwas schreiben kann. Ein Takt-Deckel behebt das. Die Erzeugung hängt an
# der Speicherbandbreite, NICHT am Takt — Drosseln kostet deshalb fast
# nichts und spart sehr viel Strom (eigener Sweep 16.08.2026, 27B-Modell,
# EINE Box):
#
#   Stufe    Takt     Erzeugung   Einlesen   Watt ⌀   °C max
#   boost    frei     20,3 tok/s   36,5 s     59,5      87    (Drosselung tritt auf)
#   normal   2200     19,6         37,8       39,9      81
#   eco      2000     20,4         39,2       32,5      74    <- bester Arbeitspunkt
#   eco+     1800     19,8         40,7       27,1      69
#
# Die Stufe steht in /etc/mc-gpu-mode; gb10-clock-cap.service ruft beim
# Systemstart `restore` auf, damit der Deckel einen Neustart überlebt.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Die sudoers-Regel erlaubt dem Agenten
# NUR die vier Stufen — `restore` und `status` ruft nur root/systemd.
set -u
STATE=/etc/mc-gpu-mode
MODE="${1:-status}"
NVSMI=/usr/bin/nvidia-smi

if [ "$#" -gt 1 ]; then
  echo "abgelehnt: höchstens ein Argument erwartet" >&2
  exit 2
fi
# Erst das Argument prüfen, dann die Umgebung: ein unbekannter Modus ist
# immer Exit 2, egal ob nvidia-smi da ist.
case "$MODE" in
  boost|normal|eco|eco+|restore|status) ;;
  *) echo "Aufruf: $0 {boost|normal|eco|eco+|restore|status}" >&2; exit 2 ;;
esac
[ -x "$NVSMI" ] || { echo "nvidia-smi fehlt unter $NVSMI" >&2; exit 1; }

# Scheitert nvidia-smi, wird die Stufe NICHT gespeichert und der Aufruf
# endet mit Exit 1 — der Agent meldet das dann als last_error, statt dass
# /etc/mc-gpu-mode etwas behauptet, was die GPU gar nicht fährt.
apply() {
  if ! "$NVSMI" -lgc "0,$1" >/dev/null 2>&1; then
    echo "nvidia-smi -lgc 0,$1 fehlgeschlagen" >&2
    return 1
  fi
}
release() {
  if ! "$NVSMI" -rgc >/dev/null 2>&1; then
    echo "nvidia-smi -rgc fehlgeschlagen" >&2
    return 1
  fi
}

case "$MODE" in
  boost)  release       && echo boost  > "$STATE" || exit 1 ;;
  normal) apply 2200    && echo normal > "$STATE" || exit 1 ;;
  eco)    apply 2000    && echo eco    > "$STATE" || exit 1 ;;
  eco+)   apply 1800    && echo "eco+" > "$STATE" || exit 1 ;;
  restore)                                     # beim Systemstart: gespeicherte Stufe setzen
          M=$(cat "$STATE" 2>/dev/null || echo eco)
          case "$M" in boost|normal|eco|eco+) ;; *) M=eco ;; esac
          exec "$0" "$M" ;;
  status) ;;
esac

GESPEICHERT=$(cat "$STATE" 2>/dev/null || echo "-")
IST=$("$NVSMI" --query-gpu=clocks.gr --format=csv,noheader 2>/dev/null)
WATT=$("$NVSMI" --query-gpu=power.draw --format=csv,noheader 2>/dev/null)
TEMP=$("$NVSMI" --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null)
echo "Modus: ${GESPEICHERT} | Takt: ${IST} | ${WATT} | ${TEMP} °C"
