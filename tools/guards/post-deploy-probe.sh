#!/usr/bin/env bash
# post-deploy-probe.sh — the after-deploy proofs as code, loud when one is missing.
#
# Lessons behind each check:
#  * "Image built, container started" proved nothing: a build in an `A && B &` chain
#    silently did not run. Only the container's age and a file compare prove it.
#  * A container built from a feature checkout instead of the deploy worktree.
#  * Two watchdogs (API and worker) after a half deploy.
#  * A required value came back empty and the script printed it and went on.
#    Rule: an empty proof stops the run.
#
# Two steps, so the decision is testable without a running stack:
#   capture  — read-only: git rev-parse/log/diff, docker inspect/logs/exec md5sum,
#              alembic current, one GET on the bootstrap endpoint (must be 403).
#   evaluate — red when any check fails or any required proof is empty.
#
# Usage: post-deploy-probe.sh --deploy-wt <dir> [--range A..B] [--expect-ref origin/main]
#                             [--backend <container>] [--worker <container>] [--base-url URL]
#        post-deploy-probe.sh --capture --deploy-wt <dir> ...   print the evidence only
#        post-deploy-probe.sh <evidence-file>                   evaluate a capture
# Evidence format: "## <name>" on its own line, then the value lines.
# Exit:  0 all proofs green, 1 a proof failed or is empty, 2 usage.
set -u

wt=""; range=""; expect_ref="origin/main"; mode=live; evidence=""
backend="${MC_BACKEND_CONTAINER:-mission-control-backend-1}"
worker="${MC_WORKER_CONTAINER:-mission-control-mc-worker-1}"
base_url="${MC_BASE_URL:-http://localhost}"
while [ $# -gt 0 ]; do
  case "$1" in
    --deploy-wt) wt="${2:?}"; shift ;;
    --range) range="${2:?}"; shift ;;
    --expect-ref) expect_ref="${2:?}"; shift ;;
    --backend) backend="${2:?}"; shift ;;
    --worker) worker="${2:?}"; shift ;;
    --base-url) base_url="${2:?}"; shift ;;
    --capture) mode=capture ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    -*) echo "post-deploy-probe: unknown option $1" >&2; exit 2 ;;
    *) mode=evaluate; evidence="$1" ;;
  esac
  shift
done

_epoch() { # ISO time from docker inspect -> epoch seconds
  python3 -c 'import sys,datetime; s=sys.argv[1].strip(); s=s[:19]; print(int(datetime.datetime.strptime(s,"%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()))' "$1" 2>/dev/null
}

section() { printf '## %s\n' "$1"; [ -n "${2:-}" ] && printf '%s\n' "$2"; return 0; }

capture() {
  [ -n "$wt" ] || { echo "post-deploy-probe: --deploy-wt is required for a live probe" >&2; return 2; }
  local head f rel a b files
  section expect_ref "$(git -C "$wt" rev-parse "$expect_ref" 2>/dev/null)"
  head="$(git -C "$wt" rev-parse HEAD 2>/dev/null)"
  section deployed_ref "$head"
  section ref_commit_epoch "$(git -C "$wt" log -1 --format=%ct HEAD 2>/dev/null)"
  section backend_created_epoch "$(_epoch "$(docker inspect -f '{{.Created}}' "$backend" 2>/dev/null)")"
  section worker_created_epoch "$(_epoch "$(docker inspect -f '{{.Created}}' "$worker" 2>/dev/null)")"
  section expect_workdir "$(cd "$wt" 2>/dev/null && pwd -P)"
  section backend_workdir "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$backend" 2>/dev/null)"
  # Files to compare: backend files changed in --range, else in the last 10 commits,
  # else a fixed sample — never an empty list.
  [ -n "$range" ] || range="HEAD~10..HEAD"
  files="$(git -C "$wt" diff --name-only "$range" -- backend/app/ 2>/dev/null | head -8)"
  [ -n "$files" ] || files="$(git -C "$wt" ls-files 'backend/app/*.py' | head -5)"
  echo "## file_compare"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    [ -f "$wt/$f" ] || continue  # deleted in the range
    rel="${f#backend/}"
    a="$(python3 -c 'import hashlib,sys; print(hashlib.md5(open(sys.argv[1],"rb").read()).hexdigest())' "$wt/$f")"
    b="$(docker exec "$backend" md5sum "/app/$rel" 2>/dev/null | cut -d' ' -f1)"
    if [ -n "$b" ] && [ "$a" = "$b" ]; then echo "SAME $f"; else echo "DIFF $f"; fi
  done <<< "$files"
  section api_log "$(docker logs "$backend" 2>&1 | grep -m1 'ENABLE_BACKGROUND_SERVICES=false')"
  section api_watchdog_count "$(docker logs "$backend" 2>&1 | grep -c 'Watchdog started')"
  section worker_log "$(docker logs "$worker" 2>&1 | grep -E 'Hintergrund-Dienste|Watchdog started' | tail -3)"
  section worker_health "$(docker inspect -f '{{.State.Health.Status}}' "$worker" 2>/dev/null)"
  section bootstrap_status "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$base_url/api/v1/internal/bootstrap?agent_name=probe-nobody")"
  section alembic_current "$(docker exec "$backend" alembic current 2>/dev/null | tail -1)"
  return 0
}

evaluate() { # evidence file
  local f="$1" fails=0
  get() { awk -v k="## $1" '$0==k{on=1;next} /^## /{on=0} on' "$f"; }
  check() { # name ok? message
    if [ "$2" = 1 ]; then echo "PASS  $1"; else echo "FAIL  $1 — $3"; fails=$((fails + 1)); fi
  }
  local req v
  # >>> guard-logic
  for req in expect_ref deployed_ref ref_commit_epoch backend_created_epoch worker_created_epoch \
             expect_workdir backend_workdir file_compare api_log api_watchdog_count worker_log \
             worker_health bootstrap_status alembic_current; do
    v="$(get "$req")"
    [ -n "$v" ] || check "$req" 0 "proof is EMPTY — an empty proof is a failed proof"
  done
  local exp dep refe be we ew bw fc
  exp="$(get expect_ref)"; dep="$(get deployed_ref)"
  check "deployed ref" "$([ -n "$dep" ] && [ "$dep" = "$exp" ] && echo 1)" "deploy worktree is at ${dep:-?}, expected ${exp:-?}"
  refe="$(get ref_commit_epoch)"; be="$(get backend_created_epoch)"; we="$(get worker_created_epoch)"
  check "backend newer than commit" "$([ -n "$be" ] && [ -n "$refe" ] && [ "$be" -ge "$refe" ] && echo 1)" "backend container ($be) is older than the deployed commit ($refe): the build did not run"
  check "worker newer than commit" "$([ -n "$we" ] && [ -n "$refe" ] && [ "$we" -ge "$refe" ] && echo 1)" "worker container ($we) is older than the deployed commit ($refe)"
  ew="$(get expect_workdir)"; bw="$(get backend_workdir)"
  check "built from deploy worktree" "$([ -n "$bw" ] && [ "$bw" = "$ew" ] && echo 1)" "backend was built from '${bw:-?}', expected '${ew:-?}'"
  fc="$(get file_compare)"
  check "files match container" "$(printf '%s\n' "$fc" | grep -q '^SAME ' && ! printf '%s\n' "$fc" | grep -q '^DIFF ' && echo 1)" "$(printf '%s\n' "$fc" | grep '^DIFF ' | tr '\n' ' ')"
  check "API keeps background off" "$(get api_log | grep -q 'ENABLE_BACKGROUND_SERVICES=false' && echo 1)" "API log has no 'ENABLE_BACKGROUND_SERVICES=false' line"
  check "no watchdog in API" "$([ "$(get api_watchdog_count)" = 0 ] && echo 1)" "API started a watchdog too ($(get api_watchdog_count)x): two watchdogs run"
  check "worker watchdog" "$(get worker_log | grep -q 'Watchdog started' && echo 1)" "worker log has no 'Watchdog started'"
  check "worker healthy" "$([ "$(get worker_health)" = healthy ] && echo 1)" "worker health is '$(get worker_health)'"
  check "bootstrap closed" "$([ "$(get bootstrap_status)" = 403 ] && echo 1)" "bootstrap endpoint answered $(get bootstrap_status), expected 403"
  check "alembic at head" "$(get alembic_current | grep -q '(head)' && echo 1)" "alembic current is '$(get alembic_current)'"
  # <<< guard-logic
  if [ "$fails" -gt 0 ]; then echo "DEPLOY NOT PROVEN: $fails check(s) failed"; return 1; fi
  echo "DEPLOY PROVEN"
  return 0
}

case "$mode" in
  capture) capture ;;
  evaluate)
    [ -f "$evidence" ] || { echo "post-deploy-probe: not found: $evidence" >&2; exit 2; }
    evaluate "$evidence" ;;
  live)
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
    capture > "$tmp" || exit 2
    evaluate "$tmp" ;;
esac
