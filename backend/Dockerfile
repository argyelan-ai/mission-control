FROM python:3.12-slim AS base

# Git + GitHub CLI + Docker CLI (fuer Agent Git-Workflow + PTY Terminal)
# Node.js 20 via NodeSource (fuer MCPRegistry.install() von npm-basierten MCP-Servern)
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
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh docker-ce-cli docker-compose-plugin nodejs \
    && rm -rf /var/lib/apt/lists/*

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
USER mcuser
EXPOSE 8000
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh
ENTRYPOINT ["/docker-entrypoint.sh"]
# --proxy-headers/--forwarded-allow-ips: without this, request.client.host is
# ALWAYS the Caddy container's IP for every request (Caddy is a separate
# container in the compose network; uvicorn's own default forwarded_allow_ips
# is 127.0.0.1, which Caddy is not) — the login rate limiter and slowapi's
# get_remote_address both key on this and end up bucketing every real client
# together (PR #404 Rex review, MEDIUM/LOW). '*' is safe here specifically
# because `backend` publishes no host port (see docker-compose.yml) — only
# containers already inside the compose network can reach it directly, the
# same trust boundary SECURITY.md already documents for this network.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--proxy-headers", "--forwarded-allow-ips=*"]

# ── release target (default) ─────────────────────────────────────────────────
FROM base AS release
COPY . .

# Create non-root user and give write access to config
RUN adduser --disabled-password --gecos "" mcuser \
    && chown -R mcuser:mcuser /app/config
USER mcuser

EXPOSE 8000

# Migrate-then-serve (siehe docker-entrypoint.sh): alembic upgrade head
# vor dem Serverstart — loest das Henne-Ei "Backend braucht Schema,
# Migrationen brauchen laufenden Container" fuer frische Installs.
COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh
ENTRYPOINT ["/docker-entrypoint.sh"]
# See the dev-target CMD above for why --proxy-headers/--forwarded-allow-ips
# are required (PR #404 Rex review, MEDIUM/LOW).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
