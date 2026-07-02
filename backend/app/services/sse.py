"""
SSE fan-out via Redis pub/sub.
Each SSE endpoint subscribes to one or more Redis channels.
Publishers call broadcast() to push events to all connected clients.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from sse_starlette.sse import EventSourceResponse

from app.config import settings
from app.redis_client import get_redis


async def broadcast(channel: str, event_type: str, data: dict) -> None:
    redis = await get_redis()
    payload = json.dumps({"id": str(uuid.uuid4()), "event": event_type, "data": data})
    await redis.publish(channel, payload)


async def _sse_generator(
    channels: list[str],
    ping_interval: int = settings.sse_ping_interval,
) -> AsyncGenerator[dict, None]:
    redis_url = settings.redis_url
    pubsub_redis = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    pubsub = pubsub_redis.pubsub()
    await pubsub.subscribe(*channels)

    try:
        while True:
            message = await asyncio.wait_for(pubsub.get_message(ignore_subscribe_messages=True), timeout=ping_interval)
            if message is not None:
                try:
                    payload = json.loads(message["data"])
                    yield {
                        "id": payload.get("id", str(uuid.uuid4())),
                        "event": payload.get("event", "message"),
                        "data": json.dumps(payload.get("data", {})),
                    }
                except (json.JSONDecodeError, KeyError):
                    pass
            else:
                # Send keepalive ping
                yield {"event": "ping", "data": "{}"}
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(*channels)
        await pubsub.aclose()
        await pubsub_redis.aclose()


def make_sse_response(channels: list[str]) -> EventSourceResponse:
    return EventSourceResponse(_sse_generator(channels))
