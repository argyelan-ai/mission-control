#!/bin/sh
# Migrate-then-serve: der Standard fuer Self-Hosted-Produkte.
#
# Ohne das crasht ein frischer Stack im Henne-Ei: `docker compose up`
# startet das Backend auf leerer DB (Scheduler-Startup braucht Tabellen
# -> Application startup failed -> unhealthy), waehrend die Migrationen
# laut Doku erst NACH dem up laufen sollen — und das Frontend wartet via
# depends_on auf ein healthy Backend, das nie kommt (CI-Fund 2026-07-02).
#
# Alembic ist idempotent (no-op wenn aktuell); seit Architektur E Teil 2
# gibt es zusaetzlich den mc-worker-Container, der MC_SKIP_MIGRATIONS=1
# setzt — Migrationen macht allein der backend-Container (der Worker startet
# erst bei service_healthy des Backends), also kein Migrations-Wettlauf.
# Postgres ist via depends_on:service_healthy schon erreichbar, ein kurzer
# Retry faengt Rest-Latenz ab. MC_SKIP_MIGRATIONS=1 schaltet das Verhalten
# ab (z.B. fuer bewusst manuell verwaltete Deployments).
set -e

# ── Host-UID-Angleich (PUID/PGID) ────────────────────────────────────────────
# ~/.mc ist ein Bind-Mount und gehoert dem Host-User. Auf Linux zaehlt die
# echte uid: laeuft der Container als mcuser (1000) und der Host-User hat eine
# andere uid (NAS, Firmenkonto, zweiter User — z.B. 1001), darf das Backend
# nichts in ~/.mc schreiben (Migration 0112 crashte, Backend blieb unhealthy).
# Docker Desktop (macOS/Windows) bildet jede Container-uid auf den Host-User ab,
# dort war das nie sichtbar.
#
# Das Image startet als root NUR fuer diesen Block und gibt die Rechte sofort
# ab (setpriv, util-linux; gleiche Rolle wie gosu):
#   * PUID/PGID gesetzt (setup.sh schreibt sie auf Linux aus `id -u`/`id -g`):
#     mcuser bekommt diese uid/gid, nur root-eigene MC-Ordner, die Docker beim
#     ersten Start selbst angelegt hat, werden dem Host-User gegeben (nicht
#     rekursiv, keine Nutzerdaten).
#   * PUID/PGID leer (macOS, Windows, Bestandsinstallationen): nichts wird
#     umgestellt — der Prozess laeuft wie bisher als mcuser 1000.
# Laeuft der Container schon nicht-root (compose `user:` o.ae.), bleibt alles
# wie es ist.
if [ "$(id -u)" = "0" ]; then
  _mc_uid="${PUID:-}"
  _mc_gid="${PGID:-}"
  _cur_uid="$(id -u mcuser)"
  _cur_gid="$(id -g mcuser)"
  if [ -n "$_mc_uid" ] || [ -n "$_mc_gid" ]; then
    [ -n "$_mc_uid" ] || _mc_uid="$_cur_uid"
    [ -n "$_mc_gid" ] || _mc_gid="$_mc_uid"
    case "$_mc_uid$_mc_gid" in
      *[!0-9]*)
        echo "FATAL: PUID/PGID must be numbers (got PUID='$_mc_uid' PGID='$_mc_gid') — fix .env (PUID=\$(id -u), PGID=\$(id -g))" >&2
        exit 1 ;;
    esac
    if [ "$_mc_uid" = "0" ]; then
      echo "FATAL: PUID=0 (root) is not supported — set PUID to your login user's \`id -u\` in .env" >&2
      exit 1
    fi
    if [ "$_mc_gid" != "$_cur_gid" ]; then
      sed -i "s/^mcuser:x:[0-9]*:/mcuser:x:${_mc_gid}:/" /etc/group
    fi
    if [ "$_mc_uid" != "$_cur_uid" ] || [ "$_mc_gid" != "$_cur_gid" ]; then
      # passwd direkt statt usermod: usermod chownt rekursiv alles unter dem
      # Home — inklusive dort eingehaengter Host-Ordner (SSH-Keys,
      # free-code-projects). find -xdev bleibt im Image-Dateisystem.
      sed -i "s/^mcuser:x:[0-9]*:[0-9]*:/mcuser:x:${_mc_uid}:${_mc_gid}:/" /etc/passwd
      find /home/mcuser /app/config -xdev \( -user "$_cur_uid" -o -group "$_cur_gid" \) \
        -exec chown -h "${_mc_uid}:${_mc_gid}" {} + 2>/dev/null || true
      echo "[entrypoint] mcuser runs as uid ${_mc_uid} gid ${_mc_gid} (PUID/PGID)" >&2
    fi
    # Ordner, die Docker fuer fehlende Bind-Mount-Quellen als root anlegt
    # (~/.mc selbst, deliverables/<slug>, mcp-screenshots): nur das Verzeichnis
    # selbst, nur wenn es root gehoert.
    # HOME_HOST = host $HOME (compose); catalog installs mount ~/.mc at
    # /home/mcuser/.mc and leave HOME_HOST unset.
    _mc_home="${HOME_HOST:-/home/mcuser}/.mc"
    if [ -d "$_mc_home" ]; then
      for _d in "$_mc_home" "$_mc_home/deliverables" "$_mc_home/mcp-screenshots" "$_mc_home"/deliverables/*; do
        if [ -d "$_d" ] && [ ! -L "$_d" ] && [ "$(stat -c %u "$_d")" = "0" ]; then
          chown "${_mc_uid}:${_mc_gid}" "$_d" && echo "[entrypoint] gave root-owned $_d to ${_mc_uid}:${_mc_gid}" >&2 || true
        fi
      done
    fi
  fi
  _run_uid="$(id -u mcuser)"
  _run_gid="$(id -g mcuser)"
  export HOME=/home/mcuser USER=mcuser LOGNAME=mcuser
  exec setpriv --reuid="$_run_uid" --regid="$_run_gid" --init-groups -- "$0" "$@"
fi

if [ "${MC_SKIP_MIGRATIONS:-0}" != "1" ]; then
  tries=0
  until alembic upgrade head; do
    tries=$((tries + 1))
    if [ "$tries" -ge 5 ]; then
      echo "FATAL: migrations failed after $tries attempts" >&2
      exit 1
    fi
    echo "migrations not applied yet (db warming up?) — retry $tries/5 in 3s" >&2
    sleep 3
  done
fi

# ── Proxy-Trust: welchen Absendern glauben wir X-Forwarded-For? ──────────────
# Uvicorn liest FORWARDED_ALLOW_IPS aus der Umgebung (uvicorn/config.py). Wir
# setzen es hier auf "eigenes Container-Netz OHNE die Gateway-Adressen": Caddy
# (Container-IP) wird geglaubt, ein Host-Prozess durch den publizierten Port
# 127.0.0.1:8000 (kommt als Gateway-IP an) nicht. Begruendung + Messung:
# app/proxy_trust.py (PR #404 Review, HOCH-1).
#
# MC_FORWARDED_ALLOW_IPS ueberschreibt das von Hand (exotische Netz-Setups).
# Schlaegt die Berechnung fehl, fallen wir laut auf "*" zurueck — das ist das
# bisherige Verhalten und niemals schlechter als jetzt; stiller Rueckfall auf
# uvicorns Default 127.0.0.1 wuerde dagegen unbemerkt wieder alle Clients in
# einen gemeinsamen Rate-Limit-Bucket werfen.
if [ -n "${MC_FORWARDED_ALLOW_IPS:-}" ]; then
  FORWARDED_ALLOW_IPS="$MC_FORWARDED_ALLOW_IPS"
  echo "[entrypoint] forwarded-allow-ips (manuell gesetzt): $FORWARDED_ALLOW_IPS" >&2
elif FORWARDED_ALLOW_IPS=$(python3 -m app.proxy_trust 2>/dev/null) && [ -n "$FORWARDED_ALLOW_IPS" ]; then
  echo "[entrypoint] forwarded-allow-ips (Container-Netz ohne Gateway): $FORWARDED_ALLOW_IPS" >&2
else
  FORWARDED_ALLOW_IPS="*"
  echo "[entrypoint] WARNUNG: Container-Netz nicht ermittelbar — forwarded-allow-ips faellt auf '*' zurueck. Host-Prozesse koennen X-Forwarded-For faelschen (betrifft Rate-Limit-Buckets und IP-Logs)." >&2
fi
export FORWARDED_ALLOW_IPS

exec "$@"
