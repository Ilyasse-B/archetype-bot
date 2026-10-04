"""
one open connection for migrations --follow.

the cli subscribes. we push a line when a migration or an agent
event actually happens. no once-a-second poll.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class LiveHub:
    """fan-out to every open follow connection."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """a queue that gets each later event."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=400)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        """hand one event to every follower. drop the oldest if a queue is full."""
        for q in list(self._subs):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except Exception:  # noqa: BLE001
                    self._subs.discard(q)


def sse_data(payload: dict[str, Any]) -> str:
    """one server-sent event."""
    return "data: " + json.dumps(payload, default=str) + "\n\n"
