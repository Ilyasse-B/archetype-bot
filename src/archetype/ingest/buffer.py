"""
in-memory ring buffer of recent migrations (raw + filtered views).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable

from archetype.ingest.models import MigrationEvent


class MigrationBuffer:
    """async-safe buffer (one writer task, many readers)."""

    def __init__(
        self,
        maxlen: int = 200,
        on_change: Callable[[MigrationEvent], None] | None = None,
    ) -> None:
        self._events: deque[MigrationEvent] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        # live follow. called after the lock is released.
        self._on_change = on_change
        self._next_id = 1
        self.connected = False
        self.last_error: str | None = None
        self.received_count = 0
        self.filtered_pass_count = 0
        self.filtered_reject_count = 0

    async def add(self, event: MigrationEvent) -> MigrationEvent:
        """append one migration and assign a monotonic id."""
        async with self._lock:
            event.id = self._next_id
            self._next_id += 1
            self._events.append(event)
            self.received_count += 1
        self._notify(event)
        return event

    async def update(self, event: MigrationEvent) -> None:
        """replace event with same id (after filter runs)."""
        async with self._lock:
            for i, existing in enumerate(self._events):
                if existing.id == event.id:
                    was = existing.filter_passed
                    self._events[i] = event
                    if was is None and event.filter_passed is True:
                        self.filtered_pass_count += 1
                    elif was is None and event.filter_passed is False:
                        self.filtered_reject_count += 1
                    break
        self._notify(event)

    def _notify(self, event: MigrationEvent) -> None:
        """tell live followers. a bad subscriber must not drop the migration."""
        if self._on_change is None:
            return
        try:
            self._on_change(event)
        except Exception:  # noqa: BLE001
            return

    async def recent(self, limit: int = 20, filtered_only: bool = False) -> list[MigrationEvent]:
        """return up to `limit` most recent (oldest → newest)."""
        async with self._lock:
            items = list(self._events)
        if filtered_only:
            items = [e for e in items if e.filter_passed is True]
        if limit <= 0:
            return items
        return items[-limit:]

    async def since(self, after_id: int, filtered_only: bool = False) -> list[MigrationEvent]:
        """events with id > after_id."""
        async with self._lock:
            items = [e for e in self._events if e.id > after_id]
        if filtered_only:
            items = [e for e in items if e.filter_passed is True]
        return items

    def snapshot_status(self) -> dict[str, object]:
        """cheap status fields for /status."""
        return {
            "connected": self.connected,
            "received_count": self.received_count,
            "buffered": len(self._events),
            "filtered_pass_count": self.filtered_pass_count,
            "filtered_reject_count": self.filtered_reject_count,
            "last_error": self.last_error,
        }
