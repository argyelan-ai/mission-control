#!/bin/bash
# mc-set-min-free.sh <kbytes> — Speicher-Reserve (vm.min_free_kbytes) setzen.
#
# Warum ein Wrapper statt `sysctl -w vm.min_free_kbytes=*` direkt in sudoers:
# ein `*` in einer sudoers-Regel passt laut `man sudoers` auch ÜBER
# Wortgrenzen. Die Regel liesse also auch
#     sysctl -w vm.min_free_kbytes=65536 kernel.core_pattern=|/tmp/x
# durch — und das wäre root. Dieser Wrapper ist deshalb die eigentliche
# Argument-Grenze: GENAU ein Argument, nur Ziffern, nur im erlaubten Bereich.
# Alles andere endet mit Exit 2, bevor irgendetwas ausgeführt wird.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Die Grenzen sind absichtlich IDENTISCH
# zu MIN_FREE_KBYTES_MIN/MAX im Agenten und MIN_FREE_KBYTES_RANGE im Backend.
set -u

MIN=65536       # 64 MB — darunter tut der Kernel nichts Sinnvolles mehr
MAX=16777216    # 16 GB — mehr Reserve macht die Box unbenutzbar

if [ "$#" -ne 1 ]; then
  echo "abgelehnt: genau ein Argument erwartet (kbytes), bekommen: $#" >&2
  exit 2
fi
VALUE="$1"
# Nur Ziffern, 5-8 Stellen (65536 hat 5, 16777216 hat 8). Kein Vorzeichen,
# kein Leerzeichen, kein Gleichheitszeichen, kein zweiter Schlüssel.
case "$VALUE" in
  ''|*[!0-9]*) echo "abgelehnt: '$VALUE' ist keine ganze Zahl" >&2; exit 2 ;;
esac
if [ "${#VALUE}" -gt 8 ] || [ "$VALUE" -lt "$MIN" ] || [ "$VALUE" -gt "$MAX" ]; then
  echo "abgelehnt: $VALUE ausserhalb von $MIN..$MAX" >&2
  exit 2
fi

SYSCTL=/usr/sbin/sysctl
[ -x "$SYSCTL" ] || SYSCTL=/sbin/sysctl

# Nur für Tests ohne root (sudo setzt die Umgebung zurück, env_reset — im
# echten Betrieb kommt diese Variable nie hier an).
if [ "${MC_DEVICE_DRY_RUN:-}" = "1" ]; then
  echo "DRY-RUN: $SYSCTL -w vm.min_free_kbytes=$VALUE"
  exit 0
fi
exec "$SYSCTL" -w "vm.min_free_kbytes=$VALUE"
