# docker/mc-claude-agent/Dockerfile
#
# Docker-Image für Mission Control Agents auf Anthropic Claude Code CLI
# (claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5). Native Binary via
# Anthropic's install.sh — kein npm-Dep für das CLI selbst. Node bleibt als
# Plugin-Runtime (claude-mem worker-service, playwright mcp, etc.).
#
# Siehe mc-openclaude-agent/ (ehemals mc-agent-base) für Sparky (qwen via
# Ollama / LM Studio mit fixierter openclaude-Version).
#
# Auth: CLAUDE_CODE_OAUTH_TOKEN env-Var (1-Jahres-OAuth-Token, via 'claude
# setup-token' auf Host generiert). Kein Credential-File-Mount — bekannter
# Bug anthropics/claude-code#22066.

FROM node:22.11.0-bookworm-slim

# Basis-Tools (gleiche Set wie mc-agent-base, aber Debian statt Alpine)
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
    python3-pip \
    pandoc \
    && rm -rf /var/lib/apt/lists/*

# Python-Libraries fuer Office-Formate (Word/Excel/PowerPoint). Pure Python,
# ARM-neutral. Anthropic document-skills (pdf/docx/xlsx/pptx) sind bereits
# als Plugin installiert und dokumentieren die Usage-Patterns.
# pandoc zusaetzlich fuer Markdown↔DOCX Konvertierungen.
RUN pip install --break-system-packages --no-cache-dir \
    python-docx==1.1.2 \
    openpyxl==3.1.5 \
    python-pptx==1.0.2

# GitHub CLI (für Agent Git-Workflow — gh auth via GH_TOKEN env)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Vercel CLI (nur deployer — via build arg)
ARG INSTALL_VERCEL=false
RUN if [ "$INSTALL_VERCEL" = "true" ]; then \
      npm install -g vercel@latest; \
    fi

# Higgsfield CLI (nur Davinci — via build arg). Credentials werden via
# Read-Only Volume Mount aus dem Host-Pfad `~/.config/higgsfield/credentials.json`
# durchgereicht (siehe docker-compose.agents.yml). Damit teilt Davinci den
# OAuth-Token mit dem Operator auf dem Host — keine doppelten Logins, automatischer
# Refresh wenn der Operator sich auf dem Host neu einloggt.
ARG INSTALL_HIGGSFIELD=false
RUN if [ "$INSTALL_HIGGSFIELD" = "true" ]; then \
      ARCH="$(dpkg --print-architecture)"; \
      case "$ARCH" in \
        arm64) HF_ARCH="arm64" ;; \
        amd64) HF_ARCH="amd64" ;; \
        *) echo "Unsupported arch $ARCH for higgsfield" && exit 1 ;; \
      esac; \
      HF_VER="0.1.32"; \
      curl -fsSL "https://github.com/higgsfield-ai/cli/releases/download/v${HF_VER}/hf_${HF_VER}_linux_${HF_ARCH}.tar.gz" \
        -o /tmp/hf.tar.gz \
      && cd /tmp && tar -xzf hf.tar.gz \
      && mv /tmp/hf /usr/local/bin/higgsfield \
      && chmod +x /usr/local/bin/higgsfield \
      && rm -f /tmp/hf.tar.gz; \
    fi

# Remotion headless render deps (nur Davinci — via build arg). ffmpeg für
# Encoding, chromium für Headless-Render. Template-`npm install` läuft beim
# ersten Render lazy (siehe ~/.openclaw/skills/argyelan-viral-shorts/remotion-template).
ARG INSTALL_REMOTION=false
RUN if [ "$INSTALL_REMOTION" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        chromium \
        fonts-liberation \
      && rm -rf /var/lib/apt/lists/*; \
    fi
ENV REMOTION_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Non-root user mit Login-Shell (bash, nicht sh — Debian hat dash als default)
RUN groupadd -r agent && useradd -r -g agent -s /bin/bash -m agent
USER agent
WORKDIR /home/agent

# Claude Code CLI — Anthropic's native installer. Lädt Linux-arm64 oder
# Linux-amd64 Binary nach ~/.local/bin/claude, keine Node-Dep für das CLI
# selbst. Auto-Update ist in Containern deaktiviert (wir pinnen die Version
# nicht explizit, aber Image-Rebuild gibt Kontrolle).
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/agent/.local/bin:${PATH}"

# Config-Dir für claude-code. WICHTIG: getrennt vom Default ~/.claude, damit
# MC-Templates (SOUL.md, TOOLS.md, HEARTBEAT.md) nicht mit OAuth-Credentials
# kollidieren. OAuth wird via env-var (CLAUDE_CODE_OAUTH_TOKEN) durchgereicht,
# nicht via File.
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude/
RUN mkdir -p /home/agent/.claude

# SSH: known_hosts pre-populaten für die üblichen Git-Hoster + accept-new
RUN mkdir -p /home/agent/.ssh \
    && ssh-keyscan -H github.com gitlab.com bitbucket.org 2>/dev/null \
      >> /home/agent/.ssh/known_hosts \
    && printf 'Host *\n    StrictHostKeyChecking accept-new\n    UserKnownHostsFile /home/agent/.ssh/known_hosts\n' \
      > /home/agent/.ssh/config \
    && chmod 700 /home/agent/.ssh \
    && chmod 600 /home/agent/.ssh/config /home/agent/.ssh/known_hosts

# KEIN CLAUDE_CODE_USE_OPENAI, KEIN OPENAI_BASE_URL, KEIN OPENAI_MODEL —
# claude-code talkt direkt zur Anthropic API. Kein Ollama-Plugin — claude-code
# hat eingebaute web_search + web_fetch über die native Tools.

# Poll-Loop + Entrypoint + Claude-Launcher + Recycler + recycler-lib + lib/
COPY --chown=agent:agent poll.sh /home/agent/poll.sh
COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh
COPY --chown=agent:agent start-claude.sh /home/agent/start-claude.sh
COPY --chown=agent:agent recycler-lib.sh /home/agent/recycler-lib.sh
COPY --chown=agent:agent recycler.sh /home/agent/recycler.sh
COPY --chown=agent:agent lib /home/agent/lib
RUN chmod +x /home/agent/poll.sh /home/agent/entrypoint.sh \
      /home/agent/start-claude.sh /home/agent/recycler.sh \
      /home/agent/recycler-lib.sh /home/agent/lib/*.sh

# `mc` CLI — thin Python client for the agent-scoped API. Populated by
# scripts/build-agent-images.sh before `docker build` (copies the canonical
# source from scripts/mc-cli/). Dockerfile owns install + PATH symlink.
COPY --chown=agent:agent mc-cli /home/agent/.mc-cli
RUN chmod +x /home/agent/.mc-cli/mc \
    && mkdir -p /home/agent/.local/bin \
    && ln -sf /home/agent/.mc-cli/mc /home/agent/.local/bin/mc

# Global git hooks — pre-push guard (gleich wie mc-agent-base, verhindert
# "push auf fremdes Repo", siehe PR #41 und Incident 2026-04-19)
RUN mkdir -p /home/agent/.git-hooks \
    && ln -s /home/agent/lib/mc-pre-push.sh /home/agent/.git-hooks/pre-push

# entrypoint.sh als PID 1 — tmux Window 0 = claude interaktiv, Window 1 = poll.sh
CMD ["/home/agent/entrypoint.sh"]
