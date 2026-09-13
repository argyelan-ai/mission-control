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
#
# Deliberately NOT `-1`/--one-message: that reads exactly one WS message and
# stops, so an unsolicited event arriving before the id-matched response
# (Rex' review on PR #544 — measured exit 1 after 0.16s while the real
# answer was 0.2s away) false-positives the check red. Without -1, websocat
# keeps forwarding every message it receives, one per line, until the
# connection closes or `timeout 5` kills it — so grep effectively loops over
# incoming messages until it sees the matching id, still bounded by the same
# 5s. -n stays: it only suppresses the Close frame websocat would otherwise
# send on stdin EOF (right after the single request line), which is what
# keeps the socket open long enough to read the response at all.
#
# grep only matches on id, not on result-vs-error — an id:1 *error* response
# (the command failing browser-side) still counts as "answered" here, and
# that's intentional: this check verifies the WS command channel itself
# round-trips, not that Target.getTargets specifically succeeds.
set -eu

CDP_HOST="${CDP_HOST:-127.0.0.1:9223}"

WS_URL=$(timeout 3 wget -qO- "http://$CDP_HOST/json/version" 2>/dev/null \
  | sed -n 's/.*"webSocketDebuggerUrl": *"\([^"]*\)".*/\1/p')

[ -n "$WS_URL" ] || exit 1

echo '{"id":1,"method":"Target.getTargets"}' \
  | timeout 5 websocat -n "$WS_URL" 2>/dev/null \
  | grep -q '"id":1'
