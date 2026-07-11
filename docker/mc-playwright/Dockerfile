# Zentraler Visual-Verifier Service — laeuft mit vorinstalliertem Chromium
# damit Agents keine eigenen Playwright-Setups mehr brauchen.
#
# Fix fuer Bug 3 (2026-04-22): Tester verbrachte 12+ Min mit Chrome-Path-Suche
# und libcairo-Errors weil sein Container kein Browser-Setup hat.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# ffmpeg fuer /record (webm->H.264 mp4) + /compose (Grid-Video);
# fonts-dejavu-core garantiert /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
# fuer drawtext-Labels (im Playwright-Image i.d.R. schon vorhanden — hier explizit).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn[standard]==0.32.1 \
    playwright==1.48.0 \
    httpx==0.27.2 \
    pydantic==2.10.3 \
    markdown==3.7 \
    pygments==2.18.0

COPY media.py /app/media.py
COPY service.py /app/service.py

ENV PYTHONUNBUFFERED=1

EXPOSE 8790

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8790"]
