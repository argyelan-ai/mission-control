#!/usr/bin/env python3
"""Local STT server — the operator's voice never leaves the machine.

An OpenAI-compatible `/v1/audio/transcriptions` endpoint around Parakeet TDT
0.6B v3 (NVIDIA, 25 European languages incl. German) running on Apple Silicon
via MLX. MC's shared transcription chain speaks exactly this protocol, so
pointing `STT_BASE_URL` here is the whole integration — no MC code knows or
cares that the model is local.

Runs on the HOST (launchd, see com.mc.stt.plist), not in Docker: MLX needs
Metal, and the Docker VM has a hard 5 GB RAM budget that models must not
touch. The backend container reaches it via host.docker.internal.

Model: loaded once at startup (~2 GB RAM), downloaded from Hugging Face on
first run into the standard HF cache. The `model` form field is accepted and
ignored — this server serves ONE model; which one is its own business.

Setup + operation: see setup.sh / README.md in this directory.
"""
from __future__ import annotations

import io
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mc.stt-server")

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"
PORT = 8585

app = FastAPI(title="MC local STT", docs_url=None, redoc_url=None)
_model = None


def _load_model():
    global _model
    if _model is None:
        from parakeet_mlx import from_pretrained

        logger.info("loading %s (first run downloads it) …", MODEL_ID)
        t0 = time.monotonic()
        _model = from_pretrained(MODEL_ID)
        logger.info("model ready in %.1fs", time.monotonic() - t0)
    return _model


def _to_wav(data: bytes, suffix: str) -> Path:
    """Any container Slack/Telegram sends (m4a, ogg, mp4) -> 16 kHz mono wav.

    parakeet-mlx wants a file path and standard PCM. ffmpeg (Homebrew) does
    the conversion; afconvert would cover m4a but not ogg/opus, so one tool
    for everything wins.
    """
    src = tempfile.NamedTemporaryFile(suffix=suffix or ".bin", delete=False)
    src.write(data)
    src.close()
    dst = Path(src.name).with_suffix(".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src.name,
         "-ar", "16000", "-ac", "1", str(dst)],
        check=True, timeout=120,
    )
    Path(src.name).unlink(missing_ok=True)
    return dst


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL_ID, "loaded": _model is not None}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("")):
    """OpenAI-shape: multipart file in, ``{"text": …}`` out.

    Errors come back as JSON with a 500 — the MC side treats any failure as
    "no transcript" and tells the operator in the channel, so the contract
    here is only: never hang, never return half an answer as success.
    """
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty file"}, status_code=400)

    wav = None
    try:
        suffix = Path(file.filename or "audio.bin").suffix
        wav = _to_wav(data, suffix)
        result = _load_model().transcribe(str(wav))
        text = (result.text or "").strip()
        logger.info("transcribed %dKB (%s) -> %d chars", len(data) // 1024,
                    file.filename, len(text))
        return {"text": text}
    except Exception as e:  # noqa: BLE001 — one bad clip must not kill the server
        logger.exception("transcription failed")
        return JSONResponse({"error": type(e).__name__}, status_code=500)
    finally:
        if wav is not None:
            wav.unlink(missing_ok=True)


if __name__ == "__main__":
    import uvicorn

    _load_model()  # fail fast at startup, not on the first voice message
    uvicorn.run(app, host="127.0.0.1", port=PORT)
