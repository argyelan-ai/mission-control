# CDP-exposing Chromium with an in-container socat forwarder.
#
# Chromium >= M113 pins its remote-debugging listener to 127.0.0.1 even with
# --remote-debugging-address=0.0.0.0, so a forwarder is needed to expose it to
# other containers. Previously that was a SEPARATE `cdp-socat` service sharing
# this container's netns (network_mode: service:cdp-browser). That coupling was
# fragile: whenever Chromium crash-looped (RestartCount hit 21), its netns was
# recreated but the standalone socat did NOT restart with it — its :9223
# listener ended up bound to a dead netns, so agents got ECONNREFUSED on the
# CDP endpoint even though Chromium itself was healthy.
#
# Folding socat INTO this image ties it to Chromium's exact lifecycle: they
# share one PID namespace and one container, so socat always restarts together
# with the browser and can never outlive its netns.
FROM zenika/alpine-chrome:latest

USER root
# websocat: does the WS leg of the healthcheck (see healthcheck.sh) — a real
# CDP roundtrip, not just the HTTP /json/version probe that missed the
# 2026-09-13 wedged-socket incident.
RUN apk add --no-cache socat websocat
COPY entrypoint.sh /usr/local/bin/cdp-entrypoint.sh
COPY healthcheck.sh /usr/local/bin/cdp-healthcheck.sh
RUN chmod +x /usr/local/bin/cdp-entrypoint.sh /usr/local/bin/cdp-healthcheck.sh
USER chrome

ENTRYPOINT ["/usr/local/bin/cdp-entrypoint.sh"]
