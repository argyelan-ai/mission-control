FROM python:3.12-slim

WORKDIR /app

# System deps fuer livekit-agents (audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repo root (ADR-061). Install deps first for layer caching.
COPY voice_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- ADR-083: GPT-Live transport, vorab ueber den offenen LiveKit-PR #7212 ---
# Seit 10.09.2026 der REGULAERE Build-Pfad (Marks Entscheid: gpt-live-1
# ersetzt Jarvis' Realtime-Transport, kein separates Test-Image mehr — dieser
# Block war vorher in voice_worker/Dockerfile.gpt-live, jetzt hier).
#
# "feat: add openai gpt-live duplex support" (livekit/agents), aufgesetzt auf
# PR #6677 (Duplex-Adapter-Core). GPTLiveModel ist NICHT auf PyPI (livekit-agents
# stable ist bei 1.8.0 ohne dieses Feature). Deshalb force-reinstall genau
# dieser zwei Packages aus dem PR-Head-Commit, ueber die normalen
# requirements.txt hinaus.
#
# HEAD SHA geprueft am 10.09.2026 via:
#   gh pr view 7212 -R livekit/agents --json headRefOid
#   -> de3c5ce66058c6ab437ad41f963cbaeb39046c6d
#
# ERSETZEN sobald LiveKit PR #7212 released ist: dann reicht die normale
# requirements.txt-Zeile wieder, dieser ganze Block + das ARG darunter faellt
# weg, und der Build-Time-Check unten (der bleibt als Regressionsschutz)
# bestaetigt es einfach gegen die released Version statt gegen den PR-SHA.
#
# --force-reinstall --no-deps ist PFLICHT hier: PyPI hat bereits ein
# livekit-plugins-openai==1.8.0 (ohne GPTLiveModel — anderer Inhalt, gleiche
# Versionsnummer), das requirements.txt oben schon zieht. Ohne
# --force-reinstall haelt pip die Version fuer "already satisfied" und
# installiert die PR-Variante NIE — GPTLiveModel bleibt dann unauffindbar
# (live verifiziert 10.09.2026: genau dieser Fehlschlag reproduziert).
# --no-deps: die Kern-Dependencies kommen schon aus requirements.txt oben;
# ein voller Resolve wuerde livekit-plugins-xai (PyPI-Range) neu aufloesen
# wollen und mit dem git-Core kollidieren.
#
# Bekannte Luecke: livekit-plugins-xai bleibt bei seiner urspruenglichen
# Installation haengen (PyPI-Version, kompatibel zu >=1.8.0 laut deren
# pyproject). Der xai-Realtime-Pfad (Provider "xai" — der dokumentierte
# Rueckweg fuer diesen Arm) laeuft auf demselben gebumpten
# livekit-plugins-openai==1.8.0 wie GPT-Live — siehe
# _build_realtime_transport() in main.py fuer die dafuer noetige typisierte
# TurnDetection (ADR-083 Nachschliff). Siehe
# docs/decisions/083-jarvis-gpt-live-transport.md.
ARG GPT_LIVE_PR_SHA=de3c5ce66058c6ab437ad41f963cbaeb39046c6d
RUN pip install --no-cache-dir --force-reinstall --no-deps \
    "git+https://github.com/livekit/agents@${GPT_LIVE_PR_SHA}#subdirectory=livekit-agents" \
    "git+https://github.com/livekit/agents@${GPT_LIVE_PR_SHA}#subdirectory=livekit-plugins/livekit-plugins-openai"

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
