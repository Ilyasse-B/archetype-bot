"""
pumpportal websocket — subscribeMigration + optional on-chain filter hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import websockets

from archetype.ingest.buffer import MigrationBuffer
from archetype.ingest.models import MigrationEvent, parse_pumpportal_message

log = logging.getLogger("archetype.pumpportal")

DEFAULT_WS_BASE = "wss://pumpportal.fun/api/data"

FilterHook = Callable[[MigrationEvent], Awaitable[MigrationEvent]]


class PumpPortalMigrationFeed:
    """background feed: migrations → buffer → optional filter → logs."""

    def __init__(
        self,
        api_key: str,
        buffer: MigrationBuffer,
        ws_base: str = DEFAULT_WS_BASE,
        reconnect_sec: float = 3.0,
        on_migration: FilterHook | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._buffer = buffer
        self._ws_base = ws_base.rstrip("?")
        self._reconnect_sec = reconnect_sec
        self._on_migration = on_migration
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _uri(self) -> str:
        return f"{self._ws_base}?{urlencode({'api-key': self._api_key})}"

    async def start(self) -> None:
        if not self.enabled:
            self._buffer.connected = False
            self._buffer.last_error = "ARCHETYPE_PUMPPORTAL_API_KEY not set"
            log.error("pumpportal key missing — migration feed disabled")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_forever(), name="pumpportal-migrations")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._buffer.connected = False
        log.info("pumpportal feed stopped")

    async def _run_forever(self) -> None:
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._buffer.connected = False
                self._buffer.last_error = str(exc)
                log.warning("pumpportal disconnected: %s — retry in %.1fs", exc, self._reconnect_sec)
                await asyncio.sleep(self._reconnect_sec)

    async def _connect_once(self) -> None:
        log.info("connecting to pumpportal (subscribeMigration)…")
        async with websockets.connect(
            self._uri(),
            ping_interval=20,
            ping_timeout=20,
            max_size=4_000_000,
        ) as ws:
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            self._buffer.connected = True
            self._buffer.last_error = None
            log.info("pumpportal connected — listening for migrations")

            async for raw in ws:
                await self._handle_message(raw)

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("non-json pumpportal frame: %s", raw[:200])
            return

        if isinstance(payload, dict) and "mint" not in payload and "token" not in payload:
            if "errors" in payload:
                err = str(payload.get("errors"))
                if "balance" in err.lower() or "minimum" in err.lower():
                    log.warning("pumpportal notice: %s", err)
                else:
                    self._buffer.last_error = err
                    log.warning("pumpportal errors frame: %s", err)
                return
            if "message" in payload and len(payload) <= 3:
                msg = str(payload.get("message"))
                log.info("pumpportal: %s", msg)
                if "subscribed" in msg.lower():
                    self._buffer.last_error = None
                return

        events = parse_pumpportal_message(payload)
        if not events:
            log.debug("pumpportal non-migration frame: %s", str(payload)[:240])
            return

        for event in events:
            stored = await self._buffer.add(event)
            bits = [f"MIGRATION #{stored.id} mint={stored.mint}"]
            if stored.signature:
                bits.append(f"sig={stored.signature[:16]}…")
            log.info(" | ".join(bits))

            if self._on_migration is not None:
                try:
                    # copy so buffer still has filter_passed=None until update
                    # (in-place mutate would break pass/reject counters)
                    updated = await self._on_migration(stored.model_copy(deep=True))
                    await self._buffer.update(updated)
                except Exception as exc:  # noqa: BLE001
                    log.exception("filter hook failed for %s: %s", stored.mint, exc)
                    failed = stored.model_copy(deep=True)
                    failed.filter_passed = False
                    failed.filter_reasons = [f"filter error: {exc}"]
                    await self._buffer.update(failed)
