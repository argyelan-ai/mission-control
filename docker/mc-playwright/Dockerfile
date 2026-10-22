# Zentraler Visual-Verifier Service — laeuft mit vorinstalliertem Chromium
# damit Agents keine eigenen Playwright-Setups mehr brauchen.
#
# Fix fuer Bug 3 (2026-04-22): Tester verbrachte 12+ Min mit Chrome-Path-Suche
# und libcairo-Errors weil sein Container kein Browser-Setup hat.
#
# Patched Node for the Playwright driver (see the PLAYWRIGHT_NODEJS_PATH block
# below). Version tag AND digest: the digest is what Docker pulls, the tag
# says which Node it is. Refresh: `docker buildx imagetools inspect
# node:<ver>-bookworm-slim` -> the top-level (multi-arch index) "Digest:".
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS node

# Playwright 1.64.0 (2026-10-13) on Ubuntu 24.04 (noble): Chrome for Testing
# 156.0.8078.4. The old v1.48.0-jammy base shipped Chromium 130 (Oct 2024, a
# year of browser fixes behind) and a driver on Node 20 (end of life). Base
# pinned by tag AND digest; the pip pin of `playwright` further down must
# match the tag (backend/tests/test_image_security_floor.py checks both).
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23

WORKDIR /app

# ca-certificates floor = the eMudhra fix (Oct 2026: the intermediates' keys
# leaked). The apt line below upgrades the package and the build FAILS below
# this version — a stale base image can no longer ship the old bundle, and a
# cached layer only exists if it passed. backend/tests/test_image_security_floor.py
# guards the value per distro.
ARG CA_CERTIFICATES_MIN=20260816ubuntu0.24.04.1
# ffmpeg fuer /record (webm->H.264 mp4) + /compose (Grid-Video);
# fonts-dejavu-core garantiert /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
# fuer drawtext-Labels (im Playwright-Image i.d.R. schon vorhanden — hier explizit).
# ca-certificates: listed so the build UPGRADES the base's stale bundle
# (v1.64.0-noble ships 20260601~24.04.1, which still trusts the compromised
# eMudhra intermediates; 20260816ubuntu0.24.04.1 = USN-8944 fix). `apt-get
# install` of an installed package upgrades it to the current candidate.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && v="$(dpkg-query -W -f='${Version}' ca-certificates)" \
    && echo "ca-certificates $v (floor ${CA_CERTIFICATES_MIN})" \
    && dpkg --compare-versions "$v" ge "${CA_CERTIFICATES_MIN}"

RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn[standard]==0.32.1 \
    playwright==1.64.0 \
    httpx==0.27.2 \
    pydantic==2.10.3 \
    markdown==3.7 \
    pygments==2.18.0 \
    "certifi>=2026.10.19"

# Playwright's driver runs on a Node it bundles — 1.64.0 bundles v24.21.0,
# below the Node.js 2026-10-20 security release (24.22.1: TLS hostname-check
# bypasses, Brotli RCE, eMudhra-free CA store). Playwright reads the Node to
# use from PLAYWRIGHT_NODEJS_PATH (playwright/_impl/_driver.py); the bundled
# binary is replaced by a link to the patched one so nothing — not the env
# var, not `python -m playwright` — can start the stale copy. The build FAILS
# if the driver's Node is below NODE_MIN.
ARG NODE_MIN=24.22.1
COPY --from=node /usr/local/bin/node /usr/local/bin/node
ENV PLAYWRIGHT_NODEJS_PATH=/usr/local/bin/node
RUN d="$(python3 -c 'import os, playwright; print(os.path.dirname(playwright.__file__))')/driver" \
    && ln -sf /usr/local/bin/node "$d/node" \
    && v="$("$d/node" --version)" \
    && echo "playwright driver node $v (floor ${NODE_MIN})" \
    && "$d/node" -e 'const [a,b]=[process.argv[1],process.argv[2]].map(s=>s.replace(/^v/,"").split(".").map(Number));const ok=a[0]===b[0]&&(a[1]>b[1]||(a[1]===b[1]&&a[2]>=b[2]));process.exit(ok?0:1)' "$v" "${NODE_MIN}" \
    && python3 -m playwright --version

# Managed Chromium policy: distrust the 12 eMudhra TLS intermediates whose
# private keys leaked in Oct 2026 (same file as cdp-browser). Chromium checks
# its BUILT-IN root store, not the system bundle, and Google's block went out
# through CRLSets, which a Playwright browser never downloads. Measured
# 2026-10-22 on 156.0.8078.4 without the policy: the emSign test site loads.
# Playwright 1.64 ships Chrome for Testing on arm64 and x64; it reads
# /etc/opt/chrome_for_testing/policies/managed (measured; it ignores
# /etc/chromium/…). The /etc/chromium copy only covers a future
# Chromium-branded build. chrome-headless-shell (Playwright's default for
# headless=True) reads NO policy at all: service.py launches the full browser
# in new headless mode (`channel="chromium"`, see _launch_chromium), and the
# headless shell is deleted so a launch that forgets the channel fails loudly
# instead of quietly trusting the chain. Drop the policy once Chromium's own
# root store excludes these certificates.
COPY policies/emudhra-distrust.json /etc/opt/chrome_for_testing/policies/managed/emudhra-distrust.json
COPY policies/emudhra-distrust.json /etc/chromium/policies/managed/emudhra-distrust.json
RUN rm -rf /ms-playwright/chromium_headless_shell-*

COPY media.py /app/media.py
COPY service.py /app/service.py
COPY deterministic_shim.js /app/deterministic_shim.js
COPY record_network.py /app/record_network.py
COPY record_network_guard.js /app/record_network_guard.js
COPY templates/ /app/templates/

# Proof by effect, at build time and on whatever architecture builds the
# image: the service's browser lists the policy on chrome://policy, and a
# launch without the channel finds no headless shell. Either one wrong -> the
# build fails (check_browser.py; no network needed).
COPY check_browser.py /app/check_browser.py
RUN python3 /app/check_browser.py

# Bench-video branding fonts: fetched + inlined at build time (never
# committed — Fontshare fonts must not be redistributed verbatim, see
# templates/bench/fetch_fonts.py). Network failure must NOT fail the build —
# the script itself guarantees that (writes an empty css + exits 0).
RUN python3 /app/templates/bench/fetch_fonts.py

ENV PYTHONUNBUFFERED=1

EXPOSE 8790

# /shared-deliverables is a named volume owned by root (this container). The
# backend runs as mcuser(1000) and must create bench-<id>/ dirs in it
# (bench_studio artifact collection) — give the volume /tmp semantics
# (1777) before serving. Non-fatal if the mount is absent (tests).
CMD ["sh", "-c", "chmod 1777 /shared-deliverables 2>/dev/null; exec uvicorn service:app --host 0.0.0.0 --port 8790"]
