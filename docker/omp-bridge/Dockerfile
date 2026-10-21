# docker/omp-bridge/Dockerfile — third harness image (ADR-045).
#
# Parallel to mc-agent-base (openclaude) and mc-claude-agent (native claude).
# Window 0 runs `bridge.py --serve` (the persistent poll -> omp -> lifecycle
# driver) instead of an interactive openclaude pane scraped by poll.sh.
#
# Build via:  scripts/build-agent-images.sh mc-omp-agent
# which materialises the mc CLI (scripts/mc-cli/) into ./mc-cli in this context.
#
# omp is NOT an npm package — the npm name "omp" is an unrelated squat. omp is a
# standalone Bun-compiled binary distributed as a GitHub release asset. We fetch
# omp-linux-arm64 (the Mac-Mini-M4 / DGX-Spark target is arm64) and verify its
# sha256 against the pin. glibc-linked -> a glibc base (debian-slim), NOT Alpine
# (musl), or the binary's loader is missing at runtime.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Base tools — same surface as mc-agent-base so the container lifecycle
# (health-check tmux pane, recycler, git workflow) behaves identically. omp
# bundles its own runtime and the mc CLI is Python; Node.js is installed further
# down for the AGENT's own work (checking the JS it writes), not for the harness.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        python3 \
        python3-pip \
        # procps: pgrep/ps fuer omp-recycler.sh proc_alive/bridge_rss_mb —
        # ohne das killt der Recycler die gesunde Bridge alle 30s (Crash-Loop)
        procps \
        bash \
        openssh-client \
        tmux \
        pandoc \
        # ffmpeg + imagemagick: Video/Audio und Bilder bearbeiten (Thumbnails,
        # Formatwandlung, Screenshots zuschneiden). Fehlte dem omp-Agenten am 04.10.2026
        # ("No ffmpeg, no imagemagick") bei Medien-Aufgaben.
        ffmpeg \
        imagemagick \
        # poppler-utils: das `Read`-Werkzeug der CLIs rendert PDF-Seiten mit
        # `pdftoppm`. Ohne das Paket bricht jedes mehrseitige PDF mit
        # "pdftoppm is not installed" ab — und der naheliegende Ausweg (macOS
        # `sips`) rendert nur Seite 1, ein mehrseitiges Dokument bleibt also
        # unlesbar. Aufgefallen 19.08.2026, als der erste Datei-Anhang im
        # Sessions-Chat ein PDF war: Bilder las der Agent sofort, PDFs gar nicht.
        poppler-utils \
        # socat: lokales CDP-Relay (cdp-relay.sh) — omps Browser-Werkzeug haengt
        # sich darueber an den gemeinsamen Agenten-Browser (cdp-browser), weil
        # Chromium Dienstnamen im Host-Header ablehnt und omps eigener Chrome
        # auf arm64 nicht existiert.
        socat \
    # github CLI (not in default Debian repos) via the official apt source
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# --- omp install (verified GitHub release binary; pin + sha256 overridable) ---
# The canonical pin lives in docker/cli-versions.json; scripts/build-agent-images.sh
# passes it as build args. These defaults mirror that pin so a bare `docker build`
# installs the same binary. sha256 = the GitHub release asset digest of
# omp-linux-arm64. Override at build:
#   --build-arg OMP_VERSION=<ver> --build-arg OMP_SHA256=<sha>
ARG OMP_VERSION=18.10.1
ARG OMP_SHA256=5cad38a8edee60cec9c0ce91810eba38bda0efe349562157023810343dacb8d5
RUN curl -fsSL \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/omp-linux-arm64" \
        -o /usr/local/bin/omp \
    && echo "${OMP_SHA256}  /usr/local/bin/omp" | sha256sum -c - \
    && chmod +x /usr/local/bin/omp \
    && /usr/local/bin/omp --version
LABEL mc.cli.name="omp" mc.cli.version="${OMP_VERSION}"

# --- Node.js for the agent's own checks (pinned LTS, verified tarball) ---
# An omp agent that writes a web page must be able to check its JS itself:
# `node --check app.js`, `node -e …`, `npx <tool>`. Without node (live
# 21.10.2026) an agent searched for node/npx/chromium, found nothing, failed to
# pip-build a JS parser (no compiler here) and fell back to a Python parser.
# Not omp's own runtime: `BUN_BE_BUN=1 omp` is Bun, but its `--check` RUNS the
# file instead of parsing it, and the variable leaks into child processes (an
# `omp` started from such a script answers as Bun). So: real Node.
# Official nodejs.org release tarball, linux-arm64 like omp above (the image is
# arm64-only). NODE_SHA256 = the tarball's line in the release's signed
# SHASUMS256.txt. Only bin/ and lib/ are unpacked (node, npm, npx, corepack);
# headers and docs are left out — nothing here compiles native addons.
# Override at build: --build-arg NODE_VERSION=<ver> --build-arg NODE_SHA256=<sha>
# tests/test_image_has_node.py guards the pin and, with MC_OMP_IMAGE set, the
# built image.
ARG NODE_VERSION=24.22.1
ARG NODE_SHA256=33c37be234ba8bee041961047b19160f4d7d0f164e8e246f78bdd10feff148b3
RUN curl -fsSL \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.gz" \
        -o /tmp/node.tar.gz \
    && echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner \
        "node-v${NODE_VERSION}-linux-arm64/bin" "node-v${NODE_VERSION}-linux-arm64/lib" \
    && rm /tmp/node.tar.gz \
    && node --version \
    && npm --version \
    && npx --version
# No "new npm version, run npm install -g npm@…" notice after every npx: the
# agent would read it as a task, and as the agent user a global install into
# /usr/local fails anyway. The pin above is the update path.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false

# Non-root user (mirrors mc-agent-base).
RUN groupadd -r agent && useradd -r -g agent -s /bin/bash -m agent

# Office-format libs for document skills (pure-Python, ARM-neutral) — parity
# with mc-agent-base so an omp agent can produce Word/Excel/PowerPoint.
RUN pip install --break-system-packages --no-cache-dir \
    python-docx==1.1.2 \
    openpyxl==3.1.5 \
    python-pptx==1.0.2

# The mc CLI is COPIED from the canonical source (scripts/mc-cli/, materialised
# as ./mc-cli by scripts/build-agent-images.sh) so the lifecycle contract
# (`mc ack` / `mc finish` / `mc blocked`) is byte-identical to every other agent.
# It is STDLIB-ONLY (urllib): no `pip install` — just chmod + symlink the
# launcher, which adds its own dir to sys.path to import the mc_cli package.
# Mirrors mc-agent-base exactly.
COPY --chown=agent:agent mc-cli /home/agent/.mc-cli
RUN chmod +x /home/agent/.mc-cli/mc \
    && ln -sf /home/agent/.mc-cli/mc /usr/local/bin/mc

# The bridge driver + native-TUI hook + launcher + forked recycler + entrypoint.
# context_detect.py (CTX-01 Nachzug Teil 2): harness-aware context%-Scraper,
# hand-maintained duplicate of scripts/context_detect.py (byte-identity
# geprueft von backend/tests/test_context_detect_equivalence.py) — muss NEBEN
# bridge.py liegen, damit `import context_detect` (kein Package, kein Pfad-
# Setup) das ausfuehrende Skriptverzeichnis findet.
COPY bridge.py            /opt/omp-bridge/bridge.py
COPY acp_client.py        /opt/omp-bridge/acp_client.py
COPY acp_chat_events.py   /opt/omp-bridge/acp_chat_events.py
COPY acp_chat.py          /opt/omp-bridge/acp_chat.py
# sink_migration.py (S1-R3): acp_chat.py imports it at start — moves MC's old
# display copies into <folder>/.mc/. Without this line the chat daemon does not
# start (tests/test_dockerfile_copies_modules.py guards every such import).
COPY sink_migration.py    /opt/omp-bridge/sink_migration.py
# model_errors.py (S1-R6): bridge.py AND acp_chat.py import it at start — the
# "model is not answering" rule. Without this line neither the task driver
# nor the chat daemon starts.
COPY model_errors.py      /opt/omp-bridge/model_errors.py
COPY acp_model.py         /opt/omp-bridge/acp_model.py
COPY acp_chat_ctl.py      /opt/omp-bridge/acp_chat_ctl.py
COPY context_detect.py    /opt/omp-bridge/context_detect.py
COPY turn-end-hook.mjs    /opt/omp-bridge/turn-end-hook.mjs
# guard-hook.mjs + guards/: the lesson guards (tools/guards, synced into this
# context by scripts/build-agent-images.sh) on every omp bash call.
COPY guard-hook.mjs       /opt/omp-bridge/guard-hook.mjs
COPY guards/              /opt/omp-bridge/guards/
COPY launch-omp.sh        /opt/omp-bridge/launch-omp.sh
# render-omp-config.sh (ADR-078): schreibt models.yml + omp.env. Der Entrypoint
# ruft es beim Start; MC ruft es im LAUFENDEN Container per
# `docker exec <container> render-omp-config.sh`, wenn hinter der Box-URL ein
# anderes Modell startet — das ersetzt den Container-Neustart. Deshalb liegt es
# ZUSÄTZLICH als Symlink in PATH: `docker exec` startet keine Login-Shell und
# löst nur absolute Pfade oder PATH-Einträge auf.
COPY render-omp-config.sh /opt/omp-bridge/render-omp-config.sh
COPY cdp-relay.sh         /opt/omp-bridge/cdp-relay.sh
# cdp_relay.py: the attributing relay cdp-relay.sh starts instead of socat
# when cdp-gateway is the target — prefixes every CDP request with
# /a/<slug> so the operator's per-agent browser panel knows whose tab it is.
COPY cdp_relay.py         /opt/omp-bridge/cdp_relay.py
COPY sigforward.sh        /opt/omp-bridge/sigforward.sh
COPY entrypoint.sh        /usr/local/bin/entrypoint.sh
COPY omp-recycler.sh      /usr/local/bin/omp-recycler.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/omp-recycler.sh \
        /opt/omp-bridge/sigforward.sh /opt/omp-bridge/launch-omp.sh /opt/omp-bridge/render-omp-config.sh \
        /opt/omp-bridge/cdp-relay.sh \
        /opt/omp-bridge/cdp_relay.py \
    && ln -sf /opt/omp-bridge/launch-omp.sh /usr/local/bin/launch-omp.sh \
    && ln -sf /opt/omp-bridge/render-omp-config.sh /usr/local/bin/render-omp-config.sh

USER agent
WORKDIR /home/agent

# omp config dir (isolated per container; auth comes from bootstrap at runtime).
# With OMP_PROFILE set, models.yml is read from
# $HOME/.omp/profiles/mc-agent/agent/models.yml — the entrypoint renders it there.
ENV HOME=/home/agent \
    OMP_HOME=/home/agent/.omp \
    PI_CODING_AGENT_DIR=/home/agent/.omp/agent \
    OMP_PROFILE=mc-agent
RUN mkdir -p /home/agent/.omp/profiles/mc-agent/agent

# Health-check anchor (ADR-049): Window 0 now runs the native omp TUI, so its
# readiness anchor is the TUI chat glyph (`╭─` / `❯`) in the visible pane — the
# same anchor every interactive agent uses — NOT the OMP_BRIDGE_READY sentinel
# (which the headless design printed into Window 0). The bridge still prints
# OMP_BRIDGE_READY, but into Window 1. See ADR-049 §5 for the one-line backend
# ready_signals follow-up this implies.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
