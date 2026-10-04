"""
live agent lines for the migrations CLI.

the daemon keeps a short ring and pushes each line to live followers.
not the full memory — just what you want to watch while it thinks.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime
from threading import Lock
from typing import Any


class AgentFeed:
    """in-memory event ring. ids only go up."""

    def __init__(self, limit: int = 300) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=max(20, limit))
        self._n = 0
        self._lock = Lock()
        # set by the daemon so migrations --follow hears each line
        self.on_push: Callable[[dict[str, Any]], None] | None = None

    def push(self, kind: str, **fields: Any) -> dict[str, Any]:
        """append one line. extra fields should stay small."""
        with self._lock:
            self._n += 1
            item = {
                "id": self._n,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "kind": kind,
            }
            for key, val in fields.items():
                if val is None:
                    continue
                item[key] = val
            self._items.append(item)
            saved = dict(item)
        cb = self.on_push
        if cb is not None:
            try:
                cb(saved)
            except Exception:  # noqa: BLE001
                pass
        return saved

    def since(self, after_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """events with id > after_id, oldest first."""
        with self._lock:
            rows = [dict(item) for item in self._items if int(item["id"]) > int(after_id)]
        if limit > 0:
            rows = rows[:limit]
        return rows
