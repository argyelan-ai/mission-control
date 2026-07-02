#!/bin/sh
# Migrate-then-serve: der Standard fuer Self-Hosted-Produkte.
#
# Ohne das crasht ein frischer Stack im Henne-Ei: `docker compose up`
# startet das Backend auf leerer DB (Scheduler-Startup braucht Tabellen
# -> Application startup failed -> unhealthy), waehrend die Migrationen
# laut Doku erst NACH dem up laufen sollen — und das Frontend wartet via
# depends_on auf ein healthy Backend, das nie kommt (CI-Fund 2026-07-02).
#
# Alembic ist idempotent (no-op wenn aktuell); MC laeuft mit genau einem
# Backend-Container, es gibt also keinen Migrations-Wettlauf. Postgres
# ist via depends_on:service_healthy schon erreichbar, ein kurzer Retry
# faengt Rest-Latenz ab. MC_SKIP_MIGRATIONS=1 schaltet das Verhalten ab
# (z.B. fuer bewusst manuell verwaltete Deployments).
set -e

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

exec "$@"
