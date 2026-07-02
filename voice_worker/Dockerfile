FROM python:3.12-slim

WORKDIR /app

# System deps fuer livekit-agents (audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Voice-Worker laeuft permanent als LiveKit Agent Worker.
# Pre-download xai plugin models (no-op fuer realtime, aber Konvention).
RUN python -c "from livekit.plugins import xai; print('xai plugin loaded')" 2>/dev/null || true

CMD ["python", "main.py", "dev"]
