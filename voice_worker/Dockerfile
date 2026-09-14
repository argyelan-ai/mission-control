FROM python:3.12-slim

WORKDIR /app

# System deps fuer livekit-agents (audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repo root (ADR-061). Install deps first for layer caching.
COPY voice_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- ADR-083: GPT-Live transport (regulaer aus PyPI seit 1.8.1) ---
# GPTLiveModel ("feat: add openai gpt-live duplex support", LiveKit-PR #7212)
# wurde am 10.09.2026 gemerged und ist ab livekit-agents /
# livekit-plugins-openai 1.8.1 regulaer auf PyPI. Bis dahin hat dieses
# Dockerfile die zwei Packages per
# `pip install --force-reinstall --no-deps git+...@<PR-SHA>` ueber die
# requirements.txt drueberinstalliert; dieser Bastel-Block ist mit dem
# Versionspin in requirements.txt (==1.8.1) ersatzlos entfallen.
#
# Der Import-Check unten bleibt als Regressionsschutz: er beweist bei JEDEM
# Build, dass GPTLiveModel auf diesem Image wirklich importierbar ist.
# Siehe docs/decisions/083-jarvis-gpt-live-transport.md.

# Build-time proof both plugins actually import on this image — fails the
# build loud (not a `|| true` swallow) so a build can never silently ship
# without the GPT-Live plugin (or a broken xai/openai Realtime import) again.
# Review-Fund (10.09.2026): the old preload check
# (`from livekit.plugins import openai, xai`) from before the Dockerfile
# consolidation was dropped, not carried over — restored below alongside the
# GPTLiveModel check.
RUN python -c "from livekit.plugins import openai, xai; print('openai + xai plugins OK')"
RUN python -c "from livekit.plugins.openai.realtime import GPTLiveModel; print('GPTLiveModel import OK')"

# Shared Jarvis package (persona, tools, mc_client) — imported as top-level
# `jarvis_core`. Copied in from the repo root so voice + telegram share one
# source of truth.
COPY jarvis_core/ ./jarvis_core/

# Voice-worker code (main.py etc.).
COPY voice_worker/ ./

CMD ["python", "main.py", "dev"]
