FROM python:3.12-slim AS base

# Git + GitHub CLI + Docker CLI (fuer Agent Git-Workflow + PTY Terminal)
# Node.js via NodeSource (fuer MCPRegistry.install() von npm-basierten MCP-Servern).
#
# Node line = 22, on purpose the same major as the agent images
# (mc-claude-agent / mc-agent-base: node:22.x): the backend's `npm install`
# writes into ~/.mc/mcp-servers, and the AGENT containers run those servers
# with their own node from the /mc-servers mount — a package with a native
# addon built against another major (different NODE_MODULE_VERSION) would not
# load there. Node 20 is end of life (no security fixes); move backend and
# agent images to the next LTS together, not one alone.
#
# NODE_MIN is the security floor: the Node.js 2026-10-21 security release
# (22.23.4 / 24.22.1 / 26.12.1 — TLS hostname-verification bypasses, Brotli
# RCE, and the CA store update that distrusts the compromised eMudhra
# intermediates). The repo line floats so a rebuild picks up later patches;
# the check after the install fails the build if the mirror ever serves an
# older one.
ARG NODE_MAJOR=22
ARG NODE_MIN=22.23.4
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates tmux openssh-client gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       -o /etc/apt/keyrings/docker.gpg \
    && chmod go+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && chmod go+r /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh docker-ce-cli docker-compose-plugin nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node -e 'const [a,b]=[process.versions.node,process.argv[1]].map(v=>v.split(".").map(Number)); \
         const ok=a[0]===b[0]&&(a[1]>b[1]||(a[1]===b[1]&&a[2]>=b[2])); \
         console.log("node "+process.versions.node+(ok?" >= ":" < ")+process.argv[1]); process.exit(ok?0:1)' "${NODE_MIN}"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies (locked versions for reproducible builds)
COPY pyproject.toml requirements.lock ./
RUN uv pip install --system --no-cache -r requirements.lock

# ── dev target ───────────────────────────────────────────────────────────────
# `docker build --target dev` / compose `target: dev`: test extras + hot
# reload. Not what compose builds by default — the release stage below is
# last, so an untargeted build always produces the production image.
FROM base AS dev
COPY . .
RUN uv pip install --system --no-cache -e ".[test]" \
    && adduser --disabled-password --gecos "" mcuser \
    && chown -R mcuser:mcuser /app/config
# No `USER mcuser`: the entrypoint starts as root only to line mcuser up with
# the host user (PUID/PGID, see docker-entrypoint.sh) and then drops to mcuser
# via setpriv before running anything — same as the release target below.
EXPOSE 8000
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh
ENTRYPOINT ["/docker-entrypoint.sh"]
# --proxy-headers: without this, request.client.host is ALWAYS the Caddy
# container's IP for every request (Caddy is a separate container in the
# compose network; uvicorn's own default forwarded_allow_ips is 127.0.0.1,
# which Caddy is not) — the login rate limiter and slowapi's
# get_remote_address both key on this and end up bucketing every real client
# together (PR #404 Rex review, MEDIUM/LOW).
#
# WHICH senders we believe is decided at startup, not here: docker-entrypoint.sh
# exports FORWARDED_ALLOW_IPS (uvicorn reads that env var) with this container's
# own subnets MINUS the bridge gateways — Caddy is trusted, a host process
# reaching us through the published 127.0.0.1:8000 port is not. `backend` DOES
# publish a host port (docker-compose.yml, the host agents need it), so the
# earlier "'*' is safe, no host port" reasoning was simply wrong.
# See app/proxy_trust.py for the measurement behind it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--proxy-headers"]

# ── release target (default) ─────────────────────────────────────────────────
FROM base AS release
# Release version (release.yml: the git tag, e.g. v0.3.1). Local builds: "dev".
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
COPY . .

# Create non-root user and give write access to config.
# The process still runs as mcuser — but the image starts as root so that
# docker-entrypoint.sh can give mcuser the host user's uid/gid (PUID/PGID from
# .env; on Linux the ~/.mc bind mount is only writable for that uid) and then
# drops privileges with setpriv before migrations or the server start. Without
# PUID/PGID (macOS/Windows Docker Desktop, older installs) mcuser stays 1000.
# For one-off commands that write files use `docker compose exec -u mcuser`.
RUN adduser --disabled-password --gecos "" mcuser \
    && chown -R mcuser:mcuser /app/config

EXPOSE 8000

# Migrate-then-serve (siehe docker-entrypoint.sh): alembic upgrade head
# vor dem Serverstart — loest das Henne-Ei "Backend braucht Schema,
# Migrationen brauchen laufenden Container" fuer frische Installs.
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh
ENTRYPOINT ["/docker-entrypoint.sh"]
# See the dev-target CMD above for why --proxy-headers is required and where
# the trust list comes from (PR #404 Rex review, MEDIUM/LOW + HOCH-1).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
