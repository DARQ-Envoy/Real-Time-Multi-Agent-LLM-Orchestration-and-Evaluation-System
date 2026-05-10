from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from redis.asyncio import Redis

EVENT_LIST_TTL_SECONDS = 600
DEFAULT_POLL_INTERVAL = 0.1
DEFAULT_STREAM_TIMEOUT = 60.0


def _list_key(job_id: str) -> str:
    return f"job:{job_id}:events"


async def publish_event(redis: Redis, job_id: str, event: dict[str, Any]) -> None:
    payload = json.dumps(event)
    key = _list_key(job_id)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.rpush(key, payload)
        pipe.expire(key, EVENT_LIST_TTL_SECONDS)
        await pipe.execute()


async def subscribe(
    redis: Redis,
    job_id: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_STREAM_TIMEOUT,
) -> AsyncIterator[dict[str, Any]]:
    """Yield events for a job in publish order. Replays past events, then polls.

    Stops after a `job_complete` event is yielded or `timeout` seconds elapse
    with no progress. Polling (vs pub/sub) gives us a single source of truth
    and avoids the LRANGE/SUBSCRIBE race window for late subscribers.
    """
    key = _list_key(job_id)
    cursor = 0
    elapsed = 0.0
    while True:
        raw_events = await redis.lrange(key, cursor, -1)
        if raw_events:
            elapsed = 0.0
            for raw in raw_events:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8")
                event = json.loads(raw)
                cursor += 1
                yield event
                if event.get("type") == "job_complete":
                    return
        else:
            if elapsed >= timeout:
                return
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
