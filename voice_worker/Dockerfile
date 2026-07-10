FROM python:3.12-slim

WORKDIR /app

# System deps fuer livekit-agents (audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repo root (ADR-061). Install deps first for layer caching.
COPY voice_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared Jarvis package (persona, tools, mc_client) — imported as top-level
# `jarvis_core`. Copied in from the repo root so voice + telegram share one
# source of truth.
COPY jarvis_core/ ./jarvis_core/

# Voice-worker code (main.py etc.).
COPY voice_worker/ ./

# Voice-Worker laeuft permanent als LiveKit Agent Worker.
# Pre-download openai + xai plugin models (no-op fuer realtime, aber Konvention).
RUN python -c "from livekit.plugins import openai, xai; print('openai + xai plugins loaded')" 2>/dev/null || true

CMD ["python", "main.py", "dev"]
