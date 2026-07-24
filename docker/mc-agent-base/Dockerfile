# docker/mc-agent-base/Dockerfile
FROM node:22.11.0-alpine

# Basis-Tools (alle Agents)
# github-cli immer mitinstallieren — Agents die keine Git-Ops machen
# bekommen es trotzdem, kostet ~15MB und vermeidet build-arg Divergenz
# zwischen agent-type. Token-Injection laeuft zentral via bootstrap +
# entrypoint (gh auth login --with-token), ungenutzte Agents halten den
# Token einfach nie aktiv.
RUN apk add --no-cache \
    git \
    curl \
    python3 \
    py3-pip \
    bash \
    openssh-client \
    tmux \
    github-cli \
    pandoc

# Python-Libraries fuer Office-Formate (Word/Excel/PowerPoint). Pure Python,
# ARM-neutral. Anthropic document-skills (pdf/docx/xlsx/pptx) sind bereits
# als Plugin installiert und dokumentieren die Usage-Patterns.
RUN pip install --break-system-packages --no-cache-dir \
    python-docx==1.1.2 \
    openpyxl==3.1.5 \
    python-pptx==1.0.2

# Vercel CLI (nur deployer — via build arg)
ARG INSTALL_VERCEL=false
RUN if [ "$INSTALL_VERCEL" = "true" ]; then \
    npm install -g vercel@latest; \
    fi

# openclaude — exakte Version, kein auto-update. Version aus docker/cli-versions.json
# (Single Source of Truth); scripts/build-agent-images.sh setzt den build-arg, das
# Default hier ist nur der Fallback fuer manuelle `docker build`-Aufrufe.
ARG OPENCLAUDE_VERSION=0.7.0
RUN npm install -g @gitlawb/openclaude@${OPENCLAUDE_VERSION}
LABEL mc.cli.name="openclaude" mc.cli.version="${OPENCLAUDE_VERSION}"

# Ollama Web Search Plugin — offizielles openclaude-Plugin von Ollama.
# Registriert die Tools `web_search` und `web_fetch` gegen Ollama Cloud.
# Aktiviert, wenn OLLAMA_API_KEY env-var gesetzt ist (aus Secrets-Vault,
# propagiert via docker-compose.agents.yml → agent container).
RUN npm install -g @ollama/openclaw-web-search@0.2.2

# Non-root User
RUN addgroup -S agent && adduser -S -s /bin/sh agent -G agent
USER agent
WORKDIR /home/agent

# Config-Dir für openclaude (isoliert — kein persönliches Claude OAuth)
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude/
RUN mkdir -p /home/agent/.claude

# SSH: known_hosts fuer die ueblichen Git-Hoster pre-populaten + accept-new
# Verhindert interaktiven "fingerprint" Prompt beim ersten git clone / ssh.
# accept-new: neue, unbekannte Hosts automatisch akzeptieren (aber NICHT
# stillschweigend akzeptieren wenn sich ein bekannter Key aendert — das bleibt
# ein Fehler, damit MITM-Angriffe erkennbar bleiben).
RUN mkdir -p /home/agent/.ssh \
    && ssh-keyscan -H github.com gitlab.com bitbucket.org 2>/dev/null >> /home/agent/.ssh/known_hosts \
    && printf 'Host *\n    StrictHostKeyChecking accept-new\n    UserKnownHostsFile /home/agent/.ssh/known_hosts\n' > /home/agent/.ssh/config \
    && chmod 700 /home/agent/.ssh \
    && chmod 600 /home/agent/.ssh/config /home/agent/.ssh/known_hosts

# OpenAI-Routing zu Ollama Cloud (Standard: glm-5.1:cloud)
# Runtime declaration for ui-detect.sh (see mc-claude-agent Dockerfile note).
ENV PANE_UI_OVERRIDE=openclaude
ENV CLAUDE_CODE_USE_OPENAI=1
ENV OPENAI_BASE_URL=https://ollama.com/v1
ENV OPENAI_MODEL=glm-5.1:cloud

# poll.sh + entrypoint.sh + start-claude.sh + recycler.sh + recycler-lib.sh + lib/ kopieren
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
# scripts/build-agent-images.sh before `docker build` (copies scripts/mc-cli/).
COPY --chown=agent:agent mc-cli /home/agent/.mc-cli
RUN chmod +x /home/agent/.mc-cli/mc \
    && mkdir -p /home/agent/.local/bin \
    && ln -sf /home/agent/.mc-cli/mc /home/agent/.local/bin/mc
ENV PATH="/home/agent/.local/bin:${PATH}"

# Global git hooks — pre-push guard prevents "pushed to wrong remote"
# incidents (2026-04-19 FreeCode case). MC writes .mc-expected-remote
# into workspaces at dispatch; the hook reads it and aborts on mismatch.
RUN mkdir -p /home/agent/.git-hooks \
    && ln -s /home/agent/lib/mc-pre-push.sh /home/agent/.git-hooks/pre-push

# entrypoint.sh als PID 1 — startet poll.sh im tmux-Window (sichtbar im Browser-Terminal)
CMD ["/home/agent/entrypoint.sh"]
