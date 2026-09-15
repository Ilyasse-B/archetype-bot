"""
pumpportal websocket — subscribeMigration only (phase 2 step 1).

reconnects forever until cancelled; never logs the api key.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlencode

import websockets

from archetype.ingest.buffer import MigrationBuffer
from archetype.ingest.models import parse_pumpportal_message

log = logging.getLogger("archetype.pumpportal")

DEFAULT_WS_BASE = "wss://pumpportal.fun/api/data"


class PumpPortalMigrationFeed:
    """background feed: migrations → buffer + daemon logs."""

    def __init__(
        self,
        api_key: str,
        buffer: MigrationBuffer,
        ws_base: str = DEFAULT_WS_BASE,
        reconnect_sec: float = 3.0,
    ) -> None:
        self._api_key = api_key.strip()
        self._buffer = buffer
        self._ws_base = ws_base.rstrip("?")
        self._reconnect_sec = reconnect_sec
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """false if no key configured."""
        return bool(self._api_key)

    def _uri(self) -> str:
        """ws url with api key query param (not logged)."""
        return f"{self._ws_base}?{urlencode({'api-key': self._api_key})}"

    async def start(self) -> None:
        """spawn the listen loop as a task."""
        if not self.enabled:
            self._buffer.connected = False
            self._buffer.last_error = "ARCHETYPE_PUMPPORTAL_API_KEY not set"
            log.error("pumpportal key missing — migration feed disabled")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_forever(), name="pumpportal-migrations")

    async def stop(self) -> None:
        """cancel the listen loop."""
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
        """reconnect loop."""
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep feed alive
                self._buffer.connected = False
                self._buffer.last_error = str(exc)
                log.warning("pumpportal disconnected: %s — retry in %.1fs", exc, self._reconnect_sec)
                await asyncio.sleep(self._reconnect_sec)

    async def _connect_once(self) -> None:
        """one ws session until it drops."""
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
        """parse one ws frame."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("non-json pumpportal frame: %s", raw[:200])
            return

        # surface server errors / acks instead of silently dropping
        if isinstance(payload, dict) and "mint" not in payload and "token" not in payload:
            if "errors" in payload:
                err = str(payload.get("errors"))
                # pumpportal often nags about wallet balance even for free
                # migration streams — warn but dont treat as fatal disconnect
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
            if stored.symbol:
                bits.append(f"symbol={stored.symbol}")
            if stored.name:
                bits.append(f"name={stored.name!r}")
            if stored.pool:
                bits.append(f"pool={stored.pool}")
            if stored.signature:
                bits.append(f"sig={stored.signature[:16]}…")
            tx_type = stored.raw.get("txType")
            if tx_type:
                bits.append(f"txType={tx_type}")
            log.info(" | ".join(bits))
