"""Headless protocol smoke test — OpenAI Live API (gpt-live-1), no LiveKit.

ADR-083. Connects directly to wss://api.openai.com/v1/live/sessions with the
exact wire shape GPTLiveModel (LiveKit PR #7212) uses, sends a short German
PCM16/24kHz test utterance, and prints session.started + any output audio /
transcript events it gets back. Proves: key works, model name is accepted,
protocol round-trips — independent of LiveKit/AgentSession.

Usage (inside a container/venv with `aiohttp` installed — already a
dependency of livekit-plugins-openai):

    OPENAI_API_KEY=... python3 scripts/gpt_live_protocol_smoke.py path/to/audio_24k_mono_s16le.pcm

Prints a JSON summary line at the end: {"ok": bool, "session_id": ...,
"audio_deltas": N, "audio_bytes": N, "transcript": "...", "closed_reason": ...}
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid

import aiohttp

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
MODEL = os.environ.get("VOICE_MODEL", "gpt-live-1")
VOICE = os.environ.get("VOICE_VOICE_ID", "marin")


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: gpt_live_protocol_smoke.py <pcm16_24k_mono_file>", file=sys.stderr)
        return 2
    pcm_path = sys.argv[1]
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    with open(pcm_path, "rb") as f:
        pcm = f.read()

    session_id = None
    audio_deltas = 0
    audio_bytes = 0
    transcript_parts: list[str] = []
    closed_reason = None
    errors: list[dict] = []

    headers = {
        "User-Agent": "MC-gpt-live-smoke/1.0 (ADR-083)",
        "Authorization": f"Bearer {api_key}",
    }

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(url=LIVE_URL, headers=headers) as ws:
            start_event = {
                "type": "session.start",
                "event_id": f"session_start_{uuid.uuid4().hex[:8]}",
                "session": {
                    "model": MODEL,
                    "instructions": (
                        "Du bist ein kurzer Verbindungstest fuer Jarvis (Mission "
                        "Control). Antworte knapp auf Deutsch."
                    ),
                    "audio": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "output": {"voice": VOICE},
                    },
                    "delegation": {
                        "type": "responses",
                        "responses": {
                            "model": os.environ.get("JARVIS_FRONTIER_MODEL", "gpt-5.5"),
                            "instructions": "Antworte in einem kurzen Satz auf Deutsch.",
                        },
                    },
                },
            }
            t0 = time.time()
            await ws.send_str(json.dumps(start_event))
            print(f"-> session.start sent (model={MODEL!r}, voice={VOICE!r})", file=sys.stderr)

            # Wait for session.started before sending audio (protocol requirement).
            started = False
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(msg.data)
                etype = event.get("type")
                if etype == "session.started":
                    session_id = (event.get("session") or {}).get("id")
                    started = True
                    print(
                        f"<- session.started after {time.time()-t0:.2f}s "
                        f"(session_id={session_id})",
                        file=sys.stderr,
                    )
                    break
                if etype == "error":
                    errors.append(event)
                    print(f"<- error before start: {event}", file=sys.stderr)
                    break

            if not started:
                print(json.dumps({"ok": False, "reason": "no_session_started", "errors": errors}))
                return 1

            # Stream the PCM in ~20ms chunks (24000 * 2 bytes/sample * 0.02s = 960 bytes).
            chunk = 960
            for i in range(0, len(pcm), chunk):
                b64 = base64.b64encode(pcm[i : i + chunk]).decode("ascii")
                await ws.send_str(json.dumps({"type": "session.input_audio.append", "audio": b64}))
                await asyncio.sleep(0.02)
            print(f"-> sent {len(pcm)} bytes of test audio", file=sys.stderr)

            # Collect responses for up to 20s or until session.closed.
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=deadline - time.time())
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                event = json.loads(msg.data)
                etype = event.get("type")
                if etype == "session.output_audio.delta":
                    delta = event.get("delta", "")
                    audio_deltas += 1
                    audio_bytes += len(base64.b64decode(delta)) if delta else 0
                elif "transcript" in etype and "delta" in event:
                    transcript_parts.append(event.get("delta", ""))
                elif etype == "session.usage.updated":
                    print(f"<- usage: {event.get('usage')}", file=sys.stderr)
                elif etype == "session.closed":
                    closed_reason = (event.get("session") or {}).get("closed", {}).get("reason") or event.get(
                        "reason"
                    )
                    print(f"<- session.closed reason={closed_reason}", file=sys.stderr)
                    break
                elif etype == "error":
                    errors.append(event)
                    print(f"<- error: {event}", file=sys.stderr)
                else:
                    print(f"<- {etype}", file=sys.stderr)

            try:
                await ws.send_str(
                    json.dumps({"type": "session.close", "event_id": f"close_{uuid.uuid4().hex[:8]}"})
                )
            except Exception:
                pass

    result = {
        "ok": audio_deltas > 0 and not errors,
        "session_id": session_id,
        "audio_deltas": audio_deltas,
        "audio_bytes": audio_bytes,
        "transcript": "".join(transcript_parts),
        "closed_reason": closed_reason,
        "errors": errors,
    }
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
