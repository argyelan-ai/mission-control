# Zentraler Visual-Verifier Service — laeuft mit vorinstalliertem Chromium
# damit Agents keine eigenen Playwright-Setups mehr brauchen.
#
# Fix fuer Bug 3 (2026-04-22): Tester verbrachte 12+ Min mit Chrome-Path-Suche
# und libcairo-Errors weil sein Container kein Browser-Setup hat.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# ca-certificates floor = the eMudhra fix (Oct 2026: the intermediates' keys
# leaked). The apt line below upgrades the package and the build FAILS below
# this version — a stale base image can no longer ship the old bundle, and a
# cached layer only exists if it passed. backend/tests/test_image_security_floor.py
# guards the value per distro.
ARG CA_CERTIFICATES_MIN=20260816ubuntu0.22.04.1
# ffmpeg fuer /record (webm->H.264 mp4) + /compose (Grid-Video);
# fonts-dejavu-core garantiert /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
# fuer drawtext-Labels (im Playwright-Image i.d.R. schon vorhanden — hier explizit).
# ca-certificates: listed so the build UPGRADES the base's stale bundle
# (v1.48.0-jammy ships 20240203~22.04.1, which still trusts the compromised
# eMudhra intermediates; 20260816ubuntu0.22.04.1 = USN-8944 fix). `apt-get
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
    playwright==1.48.0 \
    httpx==0.27.2 \
    pydantic==2.10.3 \
    markdown==3.7 \
    pygments==2.18.0 \
    "certifi>=2026.10.19"

COPY media.py /app/media.py
COPY service.py /app/service.py
COPY deterministic_shim.js /app/deterministic_shim.js
COPY templates/ /app/templates/

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
