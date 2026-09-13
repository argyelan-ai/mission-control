#!/bin/sh
# Real CDP roundtrip healthcheck — not just HTTP.
#
# Incident 2026-09-13: HTTP GET /json/version answered fine on a container
# that had been running for 5 days, while Chromium's WebSocket handler was
# wedged — every Playwright call hung for the full 30s timeout. A check that
# only probes HTTP walks right past that failure mode, because the wedge was
# specifically in the WS path that /json/version never touches. This script
# instead opens the same WebSocket agents/Playwright use and round-trips
# Target.getTargets, so a wedged socket fails the check instead of hiding
# behind a 200 OK.
#
# Target.getTargets is a browser-process command, not a per-page one, so a
# single busy/rendering tab can't make it hang — that's what keeps this from
# crying wolf on a healthy-but-loaded browser.
#
# Two independent timeouts (3s HTTP fetch + 5s WS roundtrip = 8s worst case)
# keep this script from becoming a second hang of the same kind — it fits
# inside the 10s Docker-level healthcheck timeout with margin, and 5s for
# the WS call is roughly 15-20x the ~0.3s a healthy roundtrip takes in
# practice, so brief scheduling/GC jitter won't trip it while a genuinely
# wedged socket still gets caught well before Playwright's own 30s timeout.
set -eu

CDP_HOST="127.0.0.1:9223"

WS_URL=$(timeout 3 wget -qO- "http://$CDP_HOST/json/version" 2>/dev/null \
  | sed -n 's/.*"webSocketDebuggerUrl": *"\([^"]*\)".*/\1/p')

[ -n "$WS_URL" ] || exit 1

echo '{"id":1,"method":"Target.getTargets"}' \
  | timeout 5 websocat -1 -n "$WS_URL" 2>/dev/null \
  | grep -q '"id":1'
