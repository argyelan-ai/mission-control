#!/usr/bin/env bash
# register-omp-runtime.sh — idempotent registration of the omp+Qwen runtime row.
#
# GATED: this WRITES to the real MC database via POST /api/v1/runtimes/db.
# Do NOT run it from a build/CI workflow — the operator runs it deliberately (ADR-045,
# docs/plans/omp-runtime-design.md §5/§6).
#
# Idempotent: a duplicate slug returns HTTP 409 (routers/runtimes.py:608-610),
# which this script treats as success. To CHANGE fields after first registration
# use PATCH /api/v1/runtimes/db/omp-qwen (the seeder/POST are insert-only).
#
# Alternative to this script: add the same entry to backend/config/runtimes.json
# and restart the backend — the lifespan seed_runtimes() inserts it on boot.
set -euo pipefail

MC_API="${MC_API_URL:-http://localhost:8000}"
URL="${MC_API%/}/api/v1/runtimes/db"

read -r -d '' BODY <<'JSON' || true
{
  "slug": "omp-qwen",
  "display_name": "omp headless (Qwen)",
  "runtime_type": "omp",
  "endpoint": "http://192.0.2.20:8000/v1",
  "healthcheck_path": "/models",
  "model_identifier": "nvidia/Qwen3.6-35B-A3B-NVFP4",
  "role_tags": ["coder", "general"],
  "supports_tools": true,
  "supports_reasoning": true,
  "supports_streaming": true,
  "preferred_context_len": 32768,
  "max_context_len": 262144,
  "gpu_profile": "dgx_spark_heavy",
  "memory_notes": "omp headless driver (bridge.py). Reuses the Qwen vLLM on the DGX Spark (same endpoint as qwen-general).",
  "startup_notes": "Container boots bridge.py --serve; ready when the Window-0 pane prints OMP_BRIDGE_READY. Qwen must be warm (2-5 min after vLLM start).",
  "ui_order": 10,
  "enabled": true
}
JSON

echo "POST ${URL}  (slug=omp-qwen)"
HTTP_CODE=$(curl -s -o /tmp/omp-register-resp.json -w '%{http_code}' \
  -X POST "${URL}" -H 'content-type: application/json' -d "${BODY}")

case "${HTTP_CODE}" in
  2*)
    echo "OK (${HTTP_CODE}) — runtime registered:"
    cat /tmp/omp-register-resp.json
    ;;
  409)
    echo "OK (409) — already registered (idempotent no-op). Use PATCH /api/v1/runtimes/db/omp-qwen to update fields."
    ;;
  *)
    echo "FAILED (${HTTP_CODE}):" >&2
    cat /tmp/omp-register-resp.json >&2
    exit 1
    ;;
esac

echo
echo "Verify:  curl -s ${MC_API%/}/api/v1/runtimes | jq '.[] | select(.slug==\"omp-qwen\")'"
