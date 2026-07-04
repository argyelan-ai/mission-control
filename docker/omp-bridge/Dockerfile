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
# (health-check tmux pane, recycler, git workflow) behaves identically. No node:
# omp bundles its own runtime and the mc CLI is Python; nothing here needs node.
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
    # github CLI (not in default Debian repos) via the official apt source
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# --- omp install (verified GitHub release binary; pin + sha256 overridable) ---
# Pin matches the working local binary (omp v16.2.13). sha256 from the upstream
# Homebrew formula (can1357/tap). Override at build:
#   --build-arg OMP_VERSION=16.3.0 --build-arg OMP_SHA256=<sha>
ARG OMP_VERSION=16.2.13
ARG OMP_SHA256=7cc62ef691d38c837141d7040c0730ed9c66da16a2d77ae5ccec025c099ea89d
RUN curl -fsSL \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/omp-linux-arm64" \
        -o /usr/local/bin/omp \
    && echo "${OMP_SHA256}  /usr/local/bin/omp" | sha256sum -c - \
    && chmod +x /usr/local/bin/omp \
    && /usr/local/bin/omp --version

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
COPY bridge.py            /opt/omp-bridge/bridge.py
COPY turn-end-hook.mjs    /opt/omp-bridge/turn-end-hook.mjs
COPY launch-omp.sh        /opt/omp-bridge/launch-omp.sh
COPY entrypoint.sh        /usr/local/bin/entrypoint.sh
COPY omp-recycler.sh      /usr/local/bin/omp-recycler.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/omp-recycler.sh \
        /opt/omp-bridge/launch-omp.sh \
    && ln -sf /opt/omp-bridge/launch-omp.sh /usr/local/bin/launch-omp.sh

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
