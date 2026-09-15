"""
in-memory ring buffer of recent migrations.

daemon logs live; cli pulls from here via fastapi.
"""

from __future__ import annotations

import asyncio
from collections import deque

from archetype.ingest.models import MigrationEvent


class MigrationBuffer:
    """async-safe buffer (one writer task, many readers)."""

    def __init__(self, maxlen: int = 200) -> None:
        self._events: deque[MigrationEvent] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        self._next_id = 1
        self.connected = False
        self.last_error: str | None = None
        self.received_count = 0

    async def add(self, event: MigrationEvent) -> MigrationEvent:
        """append one migration and assign a monotonic id."""
        async with self._lock:
            event.id = self._next_id
            self._next_id += 1
            self._events.append(event)
            self.received_count += 1
            return event

    async def recent(self, limit: int = 20) -> list[MigrationEvent]:
        """return up to `limit` most recent (oldest → newest)."""
        async with self._lock:
            items = list(self._events)
        if limit <= 0:
            return items
        return items[-limit:]

    async def since(self, after_id: int) -> list[MigrationEvent]:
        """events with id > after_id (for --follow polling)."""
        async with self._lock:
            return [e for e in self._events if e.id > after_id]

    def snapshot_status(self) -> dict[str, object]:
        """cheap status fields for /status."""
        return {
            "connected": self.connected,
            "received_count": self.received_count,
            "buffered": len(self._events),
            "last_error": self.last_error,
        }
