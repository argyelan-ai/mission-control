"""Live browser view — view-only CDP screencast of the agent browser.

The agent browser workflow runs against the shared `cdp-browser` service
(Chromium with an exposed CDP port, see docker-compose). playwright-mcp
drives it for the tester agents; this router attaches a SECOND, read-only
CDP session to the same pages and streams `Page.screencastFrame` JPEGs to
the operator UI over a WebSocket (pattern: cli_terminal WS proxy).

View-only by design: the proxy sends exactly three CDP methods
(startScreencast, screencastFrameAck, stopScreencast) — no input events.
"""

import asyncio
import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from app.auth import require_user
from app.config import settings

logger = logging.getLogger("mc.browser_live")

router = APIRouter(prefix="/api/v1/browser-live", tags=["browser-live"])

# cdp-socat re-exposes Chromium's 127.0.0.1-only debug port on this host:port
# inside the docker network. Overridable for tests/other topologies.
CDP_BASE_URL = os.environ.get("CDP_BROWSER_URL", "http://cdp-browser:9223")


def _rewrite_ws_url(ws_url: str, base_url: str) -> str:
    """CDP reports webSocketDebuggerUrl with its OWN idea of host (127.0.0.1)
    — rewrite host:port to the address we actually reach it under."""
    base = urlparse(base_url)
    parsed = urlparse(ws_url)
    return parsed._replace(netloc=base.netloc).geturl()


async def _list_page_targets() -> list[dict]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{CDP_BASE_URL}/json/list")
        resp.raise_for_status()
        targets = resp.json()
    pages = [t for t in targets if t.get("type") == "page"]
    # Newest tab last in most Chromium builds — surface newest first so the
    # UI defaults to what the tester is working on right now.
    return list(reversed(pages))


@router.get("/targets")
async def list_targets(current_user=Depends(require_user)):
    """Open pages in the shared agent browser (for the live-view picker)."""
    try:
        pages = await _list_page_targets()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Agent-Browser (cdp-browser) nicht erreichbar: {e}",
        )
    return [
        {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
        for t in pages
    ]


def _validate_ws_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        from jose import jwt as _jwt
        payload = _jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        return bool(payload.get("sub"))
    except Exception:
        return False


@router.websocket("/ws")
async def browser_live_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
    target: Optional[str] = None,
):
    """Stream JPEG screencast frames of one agent-browser page to the client.

    Auth: JWT via ?token=<jwt> (WebSocket can't send headers). Optional
    ?target=<cdp target id>; default = newest page. Messages to the client:
      {"type": "frame", "data": "<base64 jpeg>", "metadata": {...}}
      {"type": "status", "message": "..."}   (info/errors before close)
    Client messages are ignored (view-only).
    """
    if not _validate_ws_token(token):
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()

    try:
        pages = await _list_page_targets()
    except Exception as e:
        await websocket.send_json({"type": "status", "message": f"cdp-browser unreachable: {e}"})
        await websocket.close(code=4004)
        return
    if not pages:
        await websocket.send_json({"type": "status", "message": "No open page in the agent browser yet."})
        await websocket.close(code=4004)
        return

    page = next((p for p in pages if p.get("id") == target), pages[0])
    ws_url = _rewrite_ws_url(page["webSocketDebuggerUrl"], CDP_BASE_URL)

    import websockets

    msg_id = 0

    def _next_id() -> int:
        nonlocal msg_id
        msg_id += 1
        return msg_id

    try:
        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as cdp:
            await cdp.send(json.dumps({
                "id": _next_id(),
                "method": "Page.startScreencast",
                "params": {
                    "format": "jpeg",
                    "quality": 70,
                    "maxWidth": 1440,
                    "maxHeight": 900,
                    "everyNthFrame": 1,
                },
            }))

            async def drain_client() -> None:
                # View-only: we read (and drop) client messages solely to
                # notice the disconnect.
                while True:
                    await websocket.receive_text()

            async def pump_frames() -> None:
                while True:
                    raw = await cdp.recv()
                    msg = json.loads(raw)
                    if msg.get("method") == "Page.screencastFrame":
                        params = msg["params"]
                        # Ack FIRST — without it Chromium pauses the stream
                        # after a single frame (flow control).
                        await cdp.send(json.dumps({
                            "id": _next_id(),
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": params["sessionId"]},
                        }))
                        await websocket.send_json({
                            "type": "frame",
                            "data": params["data"],
                            "metadata": params.get("metadata", {}),
                        })

            drain = asyncio.create_task(drain_client())
            pump = asyncio.create_task(pump_frames())
            done, pending = await asyncio.wait(
                {drain, pump}, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            try:
                await cdp.send(json.dumps({"id": _next_id(), "method": "Page.stopScreencast"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info("browser_live stream ended: %s", e)
        try:
            await websocket.send_json({"type": "status", "message": str(e)[:200]})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
