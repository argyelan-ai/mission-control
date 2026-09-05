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
