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
#
# Base (ADR-088 step "images"): Debian's chromium, not zenika/alpine-chrome.
# That image is frozen at Chromium 124 — even its `latest` tag — so it never
# got an update; Alpine's own chromium (149) ships without SwiftShader, which
# leaves headless WebGL at zero. Debian trixie's chromium (154 at the time of
# writing) brings SwiftShader and passes test_webgl.sh. The base is pinned by
# digest; the chromium package follows Debian's security updates on rebuild,
# and the CI WebGL test guards every rebuild. Bump the digest on purpose.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

# chromium + socat (the forwarder above), wget + coreutils' timeout (the
# healthcheck), procps (test_webgl.sh finds the GPU process with pgrep).
# python3 + websockets: runs cdp-gateway.py (port 9300), the agent-aware
# front door — see gateway/cdp_gateway.py. Same pin as the gateway CI job.
# `--break-system-packages`: Debian's system python3, nothing else installed
# in this image for pip to conflict with.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium socat fonts-noto-core procps wget ca-certificates python3 python3-pip \
 && pip install --no-cache-dir --break-system-packages "websockets==15.0.1" \
 && apt-get purge -y python3-pip && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 chrome \
 && ln -s /usr/bin/chromium /usr/local/bin/chromium-browser

# websocat: does the WS leg of the healthcheck (see healthcheck.sh) — a real
# CDP roundtrip, not just the HTTP /json/version probe that missed the
# 2026-09-13 wedged-socket incident. No Debian package, and the upstream
# container image is musl-only: the static release binary, checksum-verified.
# The architecture comes from the image itself (dpkg), not from BuildKit's
# TARGETARCH — a classic `docker build` leaves that empty, and a silent
# amd64 fallback would put the wrong binary into an arm64 image.
ARG WEBSOCAT_VERSION=1.14.1
RUN set -eu; \
    dpkg_arch="$(dpkg --print-architecture)"; \
    case "${dpkg_arch}" in \
      arm64) arch=aarch64; sum=711a69576a2ff473fb01a90ffafb571c2ed019e55479d7ae71b12c2eadeb7011 ;; \
      amd64) arch=x86_64;  sum=66f8dd3a0394761556339117f8bb5123bddefd44e087af2a72ec22b0bd08d514 ;; \
      *) echo "unsupported arch: ${dpkg_arch}" >&2; exit 1 ;; \
    esac; \
    wget -qO /usr/local/bin/websocat \
      "https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/websocat.${arch}-unknown-linux-musl"; \
    echo "${sum}  /usr/local/bin/websocat" | sha256sum -c -; \
    chmod +x /usr/local/bin/websocat

COPY entrypoint.sh /usr/local/bin/cdp-entrypoint.sh
COPY healthcheck.sh /usr/local/bin/cdp-healthcheck.sh
COPY gateway/cdp_gateway.py /opt/cdp-gateway/cdp_gateway.py
RUN chmod +x /usr/local/bin/cdp-entrypoint.sh /usr/local/bin/cdp-healthcheck.sh
USER chrome
WORKDIR /home/chrome

ENTRYPOINT ["/usr/local/bin/cdp-entrypoint.sh"]
