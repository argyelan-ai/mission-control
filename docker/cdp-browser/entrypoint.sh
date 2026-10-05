#!/bin/sh
# Start the CDP forwarder, then hand off to Chromium as PID 1.
#
# socat re-exposes Chromium's loopback-only debug port (127.0.0.1:9222) on
# 0.0.0.0:9223 so other containers (playwright-mcp via CDP_BROWSER_URL) can
# reach it. `fork` handles one child per connection; it connects to :9222
# per-connection, so it tolerates Chromium not being up yet at boot (early
# connections fail, later ones succeed once Chromium is listening).
#
# Running in the SAME container as Chromium (vs the old standalone cdp-socat
# sidecar) means socat shares Chromium's lifecycle and netns — it restarts
# with the browser and can never bind to a stale namespace.
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

# cdp-gateway: agent-aware front door on :9300 (bauplan.md PR B1). It waits
# out Chromium's own startup itself (retries /json/version internally), so
# starting it before Chromium is listening is safe — same tolerance socat's
# `fork` already has for early connections. Runs as a background job in this
# same shell/container so it shares Chromium's lifecycle exactly like socat:
# a Chromium crash-loop takes it down and back up with the browser, it can
# never outlive a stale netns, and the container healthcheck (cdp-healthcheck.sh)
# catches it being dead the same way it already catches a wedged Chromium.
python3 /opt/cdp-gateway/cdp_gateway.py &

# exec so Chromium replaces this shell: its exit ends the container (restart:
# unless-stopped), taking socat down with it. PID 1 is the compose `init: true`
# reaper, not Chromium — Chromium never reaps orphans (the healthcheck's
# `timeout` children piled up as zombies, see docker-compose.yml). "$@" = the
# chromium flags from the compose `command:`.
exec chromium-browser "$@"
