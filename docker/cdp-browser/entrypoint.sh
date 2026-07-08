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

# exec so Chromium becomes PID 1: its exit restarts the container (restart:
# unless-stopped), taking socat down with it. "$@" = the chromium flags from
# the compose `command:`.
exec chromium-browser "$@"
