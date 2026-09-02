#!/bin/bash
# mc-set-mtu.sh <schnittstelle> <mtu> — MTU einer Netz-Schnittstelle setzen.
#
# Warum ein Wrapper statt `ip link set * mtu *` direkt in sudoers: die zwei
# `*` passen laut `man sudoers` über Wortgrenzen hinweg, die Regel liesse also
#     ip link set eth0 down mtu 1500      (Schnittstelle aus)
#     ip link set eth0 netns 1 mtu 1500   (Schnittstelle in anderen Namensraum)
# durch. Dieser Wrapper ist deshalb die eigentliche Argument-Grenze: GENAU
# zwei Argumente, Schnittstelle nur aus dem erlaubten Zeichensatz UND in
# /sys/class/net vorhanden, MTU nur Ziffern im erlaubten Bereich. Alles
# andere endet mit Exit 2, bevor irgendetwas ausgeführt wird.
#
# Wird von mc-node-agent.py installiert (root:root 0755 in /usr/local/sbin,
# nur mit --install --allow-control). Die Grenzen sind absichtlich IDENTISCH
# zu MTU_MIN/MAX im Agenten und MTU_RANGE im Backend.
set -u

MIN=1500   # Ethernet-Standard — kleiner bremst nur und bricht Verbund-Traffic
MAX=9000   # Jumbo-Frames

if [ "$#" -ne 2 ]; then
  echo "abgelehnt: genau zwei Argumente erwartet (schnittstelle mtu), bekommen: $#" >&2
  exit 2
fi
IFACE="$1"
MTU="$2"

# Schnittstellenname: Linux erlaubt max. 15 Zeichen (IFNAMSIZ-1). Erlaubt
# sind nur Buchstaben, Ziffern, '-', '_' und '.' — kein '/', kein Leerzeichen,
# kein Optionsstrich am Anfang (sonst würde "-h" o.ä. als Option gelesen).
case "$IFACE" in
  ''|-*|*[!A-Za-z0-9._-]*) echo "abgelehnt: ungültiger Schnittstellenname '$IFACE'" >&2; exit 2 ;;
esac
if [ "${#IFACE}" -gt 15 ] || [ "$IFACE" = "." ] || [ "$IFACE" = ".." ]; then
  echo "abgelehnt: ungültiger Schnittstellenname '$IFACE'" >&2
  exit 2
fi
if [ ! -e "/sys/class/net/$IFACE" ] && [ "${MC_DEVICE_DRY_RUN:-}" != "1" ]; then
  echo "abgelehnt: Schnittstelle '$IFACE' gibt es nicht" >&2
  exit 2
fi

case "$MTU" in
  ''|*[!0-9]*) echo "abgelehnt: '$MTU' ist keine ganze Zahl" >&2; exit 2 ;;
esac
if [ "${#MTU}" -gt 4 ] || [ "$MTU" -lt "$MIN" ] || [ "$MTU" -gt "$MAX" ]; then
  echo "abgelehnt: MTU $MTU ausserhalb von $MIN..$MAX" >&2
  exit 2
fi

IP=/usr/sbin/ip
[ -x "$IP" ] || IP=/sbin/ip
[ -x "$IP" ] || IP=/usr/bin/ip

# Nur für Tests ohne root (sudo setzt die Umgebung zurück, env_reset — im
# echten Betrieb kommt diese Variable nie hier an).
if [ "${MC_DEVICE_DRY_RUN:-}" = "1" ]; then
  echo "DRY-RUN: $IP link set dev $IFACE mtu $MTU"
  exit 0
fi
# `dev` explizit: damit der Name nie als Schlüsselwort (up/down/netns…)
# gelesen werden kann, selbst wenn eine Schnittstelle so hiesse.
exec "$IP" link set dev "$IFACE" mtu "$MTU"
