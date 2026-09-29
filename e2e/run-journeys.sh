#!/bin/sh
# Journey tests (E6) — build a throw-away MC stack, run the click tests
# against it, tear it down. docs/journeys.md has the why and the rollout.
#
#   e2e/run-journeys.sh                 nightly mode: update the runner's own
#                                       clone ($MC_HOME/journeys/src) to
#                                       origin/main and test that
#   e2e/run-journeys.sh --src .         test this checkout as it is (no fetch)
#   options: --keep (leave the stack up for debugging, prints how to stop it)
#            --grep TEXT (only journeys whose title matches)
#
# Result: $MC_HOME/journeys/last.json and runs/<stamp>/ (result.json,
# run.log, Playwright report). Exit 0 green or skipped, 1 red, 2 error.
#
# Never touches the live install: own compose project (mc-journeys), own
# images (mc-journeys-*:nightly), no ~/.mc mount, no docker socket, one port
# on localhost. Skips (exit 0, status "skipped") while a head runs or a
# deploy is going on, and when the disk is short.
set -eu

MC_HOME=${MC_HOME:-"$HOME/.mc"}
J_HOME="$MC_HOME/journeys"
PROJECT=mc-journeys
PORT=${MC_JOURNEYS_PORT:-18080}
TIME_LIMIT_S=${MC_JOURNEYS_TIME_LIMIT_S:-1800}
MIN_FREE_GB=${MC_JOURNEYS_MIN_FREE_GB:-20}
REPO_URL=${MC_JOURNEYS_REPO:-}
SRC=""
KEEP=0
GREP=""

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC=$(cd "$2" && pwd); shift 2 ;;
    --keep) KEEP=1; shift ;;
    --grep) GREP=$2; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

STAMP=${MC_JOURNEYS_STAMP:-$(date +%Y%m%d-%H%M%S)}
export MC_JOURNEYS_STAMP="$STAMP"
RUN_DIR="$J_HOME/runs/$STAMP"
mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/run.log"
STACK_HOME="$J_HOME/stack-home"
ENV_FILE="$J_HOME/stack.env"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; }

# result <status> <summary> [report.json]  → result.json + last.json
result() {
  python3 - "$1" "$2" "${3:-}" "$RUN_DIR" "$J_HOME" "$STAMP" "${COMMIT:-}" <<'PY'
import json, os, sys, time
status, summary, report, run_dir, j_home, stamp, commit = sys.argv[1:8]
out = {"status": status, "summary": summary, "run": stamp, "commit": commit or None,
       "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "report_dir": run_dir, "journeys": []}
if report and os.path.exists(report):
    data = json.load(open(report))
    def walk(suite, file=None):
        file = suite.get("file") or file
        for spec in suite.get("specs", []):
            for t in spec.get("tests", []):
                results = t.get("results") or [{}]
                out["journeys"].append({
                    "file": file, "title": spec.get("title"),
                    "status": results[-1].get("status", "skipped"),
                    "duration_ms": results[-1].get("duration"),
                })
        for child in suite.get("suites", []):
            walk(child, file)
    for s in data.get("suites", []):
        walk(s)
    counts = {}
    for j in out["journeys"]:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    out["counts"] = counts
    # pinned gaps: tests that assert a known product gap still exists
    out["known_gaps"] = [j["title"] for j in out["journeys"] if "known gap" in (j["title"] or "")]
tmp = os.path.join(j_home, ".last.json.tmp")
for path in (os.path.join(run_dir, "result.json"), tmp):
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)
os.replace(tmp, os.path.join(j_home, "last.json"))
print(f"journeys: {status} — {summary}")
PY
}

compose() {
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$SRC/e2e/stack/docker-compose.journeys.yml" "$@"
}

teardown() {
  [ -n "$SRC" ] && [ -f "$ENV_FILE" ] || return 0
  if [ "$KEEP" = 1 ]; then
    log "--keep: stack left running on http://localhost:$PORT — stop it with:"
    log "  docker compose -p $PROJECT --env-file $ENV_FILE -f $SRC/e2e/stack/docker-compose.journeys.yml down -v"
    return 0
  fi
  compose down -v --remove-orphans >>"$LOG" 2>&1 || true
  # Only our own leftovers: dangling images carrying the mc-journeys label.
  # Never a global prune — the build cache is shared with the live install.
  docker image prune -f --filter label=mc-journeys=1 >>"$LOG" 2>&1 || true
  rm -rf "$STACK_HOME" "$ENV_FILE"
}

# ── preflight: skip instead of competing with real work ─────────────────────
skip_reason() {
  free_gb=$(df -g "$MC_HOME" | awk 'NR==2 {print $4}')
  if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    echo "disk: ${free_gb} GB free, need $MIN_FREE_GB"; return
  fi
  if [ -e "$MC_HOME/locks/deploy.lock" ]; then
    echo "deploy running (deploy.lock)"; return
  fi
  if pgrep -f "docker.*compose.*(mission-control|deploy-main).*(build|up)" >/dev/null 2>&1; then
    echo "deploy running (docker compose build/up of the live project)"; return
  fi
  spool="$MC_HOME/heads/spool"
  if [ -d "$spool/processing" ] && [ -n "$(ls -A "$spool/processing" 2>/dev/null)" ]; then
    echo "head running (spool request in processing)"; return
  fi
  if [ -d "$MC_HOME/heads" ] && [ -n "$(find "$MC_HOME/heads" -maxdepth 3 -path '*/.wrapper/heartbeat' -mmin -3 2>/dev/null | head -1)" ]; then
    echo "head running (heartbeat in the last 3 min)"; return
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $PORT is busy"; return
  fi
  echo ""
}

main() {
  reason=$(skip_reason)
  if [ -n "$reason" ]; then
    log "skipped: $reason"
    result skipped "$reason"
    return 0
  fi

  # ── source: the runner's own clone at origin/main, or --src as it is ──────
  if [ -z "$SRC" ]; then
    SRC="$J_HOME/src"
    if [ ! -d "$SRC/.git" ]; then
      [ -n "$REPO_URL" ] || { log "MC_JOURNEYS_REPO is not set (see install-journeys.sh)"; return 2; }
      log "cloning $REPO_URL"
      git clone --quiet "$REPO_URL" "$SRC" >>"$LOG" 2>&1
    fi
    git -C "$SRC" fetch --quiet origin main >>"$LOG" 2>&1
    git -C "$SRC" checkout --quiet --force --detach FETCH_HEAD >>"$LOG" 2>&1
    git -C "$SRC" clean -fdxq -e e2e/node_modules >>"$LOG" 2>&1
  fi
  COMMIT=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo "")
  grep -q '^name: mc-journeys$' "$SRC/e2e/stack/docker-compose.journeys.yml" || {
    log "refusing: compose file is not the mc-journeys stack"; return 2; }
  log "testing $SRC at ${COMMIT:-?}"

  # ── fresh secrets + empty home for this run ───────────────────────────────
  rm -rf "$STACK_HOME"; mkdir -p "$STACK_HOME"
  ( umask 077
    fernet=$(openssl rand -base64 32 | tr '+/' '-_' | cut -c1-44)
    cat >"$ENV_FILE" <<EOF
DB_PASSWORD=$(openssl rand -hex 16)
REDIS_PASSWORD=$(openssl rand -hex 16)
JWT_SECRET_KEY=$(openssl rand -hex 32)
LOCAL_AUTH_TOKEN=$(openssl rand -hex 32)
SECRETS_ENCRYPTION_KEY=$fernet
INTERNAL_BOOTSTRAP_SECRET=$(openssl rand -hex 32)
MC_JOURNEYS_HOME=$STACK_HOME
MC_JOURNEYS_PORT=$PORT
EOF
  )

  log "build (cached)"
  compose build >>"$LOG" 2>&1 || { log "build failed"; result error "build failed"; return 2; }
  log "start"
  compose up -d --wait --wait-timeout 300 >>"$LOG" 2>&1 || {
    compose logs --tail=80 >>"$LOG" 2>&1 || true
    log "stack did not become healthy"; result error "stack did not become healthy"; return 2; }

  log "playwright"
  cd "$SRC/e2e"
  npm ci --no-audit --no-fund >>"$LOG" 2>&1
  npx playwright install chromium >>"$LOG" 2>&1
  npx tsc --noEmit >>"$LOG" 2>&1 || { log "journey specs do not type-check"; result error "journey specs do not type-check"; return 2; }
  report="$RUN_DIR/report.json"
  set +e
  MC_JOURNEYS_BASE="http://localhost:$PORT" MC_JOURNEYS_REPORT="$report" MC_JOURNEYS_HTML="$RUN_DIR/html" \
    npx playwright test --output "$RUN_DIR/test-results" ${GREP:+--grep "$GREP"} >>"$LOG" 2>&1
  rc=$?
  set -e
  if [ $rc -eq 0 ]; then
    result green "all journeys passed" "$report"
    return 0
  fi
  [ -f "$report" ] || { result error "playwright did not run (exit $rc)"; return 2; }
  result red "journeys failed — see $RUN_DIR/html" "$report"
  return 1
}

# ── time box: the whole run (build included) gets TIME_LIMIT_S ─────────────
if [ "${MC_JOURNEYS_CHILD:-}" = 1 ]; then
  trap teardown EXIT
  main
  exit $?
fi

# one run at a time (a stale lock from a killed run is taken over)
LOCK="$J_HOME/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if kill -0 "$(cat "$LOCK/pid" 2>/dev/null || echo 0)" 2>/dev/null; then
    echo "another journey run is active" >&2; exit 2
  fi
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# keep the last 14 runs
ls -1d "$J_HOME"/runs/*/ 2>/dev/null | sort -r | tail -n +15 | while read -r old; do rm -rf "$old"; done

set -m  # the child gets its own process group, so a timeout stops all of it
MC_JOURNEYS_CHILD=1 "$0" ${SRC:+--src "$SRC"} $( [ "$KEEP" = 1 ] && echo --keep ) ${GREP:+--grep "$GREP"} &
child=$!
waited=0
while kill -0 "$child" 2>/dev/null; do
  if [ "$waited" -ge "$TIME_LIMIT_S" ]; then
    log "time limit ${TIME_LIMIT_S}s reached — stopping the run"
    kill -TERM -"$child" 2>/dev/null || true
    sleep 20
    kill -KILL -"$child" 2>/dev/null || true
    # the child's EXIT trap may not have run: tear down here as well
    [ -n "$SRC" ] || SRC="$J_HOME/src"
    KEEP=0
    teardown
    result error "time limit ${TIME_LIMIT_S}s reached"
    exit 2
  fi
  sleep 5
  waited=$((waited + 5))
done
wait "$child"
