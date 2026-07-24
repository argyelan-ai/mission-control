# docker/mc-kimi-agent/Dockerfile
#
# Docker-Image für Mission Control Agents auf der Kimi Code CLI (Moonshot AI,
# Binary `kimi`, Model K3 via Kimi-Subscription-OAuth). Vierte Harness neben
# claude / openclaude / omp.
#
# Binary: selbst-enthaltenes Single-Binary (embedded Node) von
# code.kimi.com — KEIN npm-Paket, kein Node im Image nötig. Download wird
# gegen die sha256 aus dem offiziellen Release-Manifest
# (code.kimi.com/kimi-code/binaries/<ver>/manifest.json, Key linux-arm64)
# gepinnt — gleiches Muster wie docker/omp-bridge.
#
# Auth: Kimi kennt KEINEN langlebigen Token (nur OAuth mit Refresh-ROTATION —
# eine kopierte Credentials-Datei stirbt, sobald ein anderes Gerät refresht;
# Spike 2026-07-24). Deshalb bekommt jeder Agent seinen EIGENEN Login:
# ~/.mc/agents/<slug>/kimi-config wird rw nach /home/agent/.kimi-code
# gemountet (KIMI_CODE_HOME) und persistiert credentials/ über Recreates.
# Erstes Provisioning: einmalig `/login` (Device-Code) im Sessions-Terminal.

FROM debian:bookworm-slim

# Basis-Tools (gleiche Menge wie mc-claude-agent, minus Node/Office-Stack)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    python3 \
    bash \
    openssh-client \
    tmux \
    gnupg \
    procps \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (für Agent Git-Workflow — gh auth via GH_TOKEN env)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Kimi Code CLI — gepinntes Release-Binary, sha256-verifiziert.
# Version + Checksum kommen aus docker/cli-versions.json via
# scripts/build-agent-images.sh; die Defaults hier sind nur Fallback für
# manuelle `docker build`-Aufrufe.
ARG KIMI_VERSION=0.29.1
ARG KIMI_SHA256=c2bdbced6becf516e270cea11a3ee7b54c789cbdfe15bd70797f05bd5f943ef0
RUN curl -fsSL \
      "https://code.kimi.com/kimi-code/binaries/${KIMI_VERSION}/kimi-code-linux-arm64" \
      -o /usr/local/bin/kimi \
    && echo "${KIMI_SHA256}  /usr/local/bin/kimi" | sha256sum -c - \
    && chmod +x /usr/local/bin/kimi \
    && /usr/local/bin/kimi --version
LABEL mc.cli.name="kimi" mc.cli.version="${KIMI_VERSION}"

# Non-root user mit Login-Shell
RUN groupadd -r agent && useradd -r -g agent -s /bin/bash -m agent
USER agent
WORKDIR /home/agent

# KIMI_CODE_HOME: getrennt vom Default-Pfad, wird per-Agent rw gemountet
# (~/.mc/agents/<slug>/kimi-config) — credentials/ + config.toml persistieren
# über Container-Recreates. Das Binary liegt bewusst in /usr/local/bin und
# NICHT unter ~/.kimi-code/bin, damit der Mount es nicht verdeckt.
ENV KIMI_CODE_HOME=/home/agent/.kimi-code
RUN mkdir -p /home/agent/.kimi-code /home/agent/.claude

# Die kimi-TUI rendert Box-Glyphs wie claude <=2.0 — die Pane-Heuristik
# erkennt kimi zwar an der `context: N% (x/1M)`-Statuszeile, aber das Image
# weiss seine Runtime mit Sicherheit: Override gewinnt (siehe docs/adapters.md).
ENV PANE_UI_OVERRIDE=kimi

# SSH: known_hosts pre-populaten für die üblichen Git-Hoster + accept-new
RUN mkdir -p /home/agent/.ssh \
    && ssh-keyscan -H github.com gitlab.com bitbucket.org 2>/dev/null \
      >> /home/agent/.ssh/known_hosts \
    && printf 'Host *\n    StrictHostKeyChecking accept-new\n    UserKnownHostsFile /home/agent/.ssh/known_hosts\n' \
      > /home/agent/.ssh/config \
    && chmod 700 /home/agent/.ssh \
    && chmod 600 /home/agent/.ssh/config /home/agent/.ssh/known_hosts

# Poll-Loop + Entrypoint + Kimi-Launcher + lib/
# (kein recycler.sh — der Claude-Process-Recycler ist claude-mem-spezifisch;
# kimi bekommt Session-Hygiene über poll.sh Session-Resets)
COPY --chown=agent:agent poll.sh /home/agent/poll.sh
COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh
COPY --chown=agent:agent start-kimi.sh /home/agent/start-kimi.sh
COPY --chown=agent:agent lib /home/agent/lib
RUN chmod +x /home/agent/poll.sh /home/agent/entrypoint.sh \
      /home/agent/start-kimi.sh /home/agent/lib/*.sh

# `mc` CLI — thin Python client for the agent-scoped API. Populated by
# scripts/build-agent-images.sh before `docker build`.
COPY --chown=agent:agent mc-cli /home/agent/.mc-cli
RUN chmod +x /home/agent/.mc-cli/mc \
    && mkdir -p /home/agent/.local/bin \
    && ln -sf /home/agent/.mc-cli/mc /home/agent/.local/bin/mc
ENV PATH="/home/agent/.local/bin:${PATH}"

# Global git hooks — pre-push guard (siehe PR #41 / Incident 2026-04-19)
RUN mkdir -p /home/agent/.git-hooks \
    && ln -s /home/agent/lib/mc-pre-push.sh /home/agent/.git-hooks/pre-push

# entrypoint.sh als PID 1 — tmux Window 0 = kimi interaktiv, Window 1 = poll.sh
CMD ["/home/agent/entrypoint.sh"]
