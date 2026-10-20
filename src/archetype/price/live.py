"""
one helius vault websocket per mint.

peek and tp/sl all read this. nobody opens a second
socket for the same coin, and peek does not poll rpc on its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from archetype.filters.helius import HeliusRpc
from archetype.price.models import PriceTick
from archetype.price.stream import aiter_price_ticks_ws

log = logging.getLogger("archetype.price.live")

TickFn = Callable[[PriceTick], Awaitable[None]]


class LivePrices:
    """refcount'd live price. latest tick stays in memory."""

    def __init__(self, rpc: HeliusRpc, ledger_path: Path) -> None:
        self.rpc = rpc
        self.ledger_path = ledger_path
        self._latest: dict[str, PriceTick] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._listeners: dict[str, list[TickFn]] = {}
        self._refs: dict[str, int] = {}
        # pool, name, symbol — first caller that knows them wins
        self._hints: dict[str, tuple[str | None, str | None, str | None]] = {}
        self._lock = asyncio.Lock()

    def watching(self, mint: str) -> bool:
        """true while the vault socket for this mint is up."""
        task = self._tasks.get(mint.strip())
        return task is not None and not task.done()

    def latest(self, mint: str) -> PriceTick | None:
        """last tick we already have. no network."""
        return self._latest.get(mint.strip())

    async def wait_latest(self, mint: str, timeout: float = 15.0) -> PriceTick | None:
        """
        the tick the socket already paid for.

        first think races the seed, so we wait for that instead of firing
        another helius read.
        """
        mint = mint.strip()
        got = self._latest.get(mint)
        if got is not None:
            return got
        ev = self._ready.get(mint)
        if ev is None:
            return None
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            return self._latest.get(mint)
        return self._latest.get(mint)

    async def acquire(
        self,
        mint: str,
        *,
        pool: str | None = None,
        name: str | None = None,
        symbol: str | None = None,
    ) -> None:
        """start the socket if it isnt already running. one ref per caller."""
        mint = mint.strip()
        async with self._lock:
            self._refs[mint] = self._refs.get(mint, 0) + 1
            old = self._hints.get(mint, (None, None, None))
            self._hints[mint] = (pool or old[0], name or old[1], symbol or old[2])
            if mint not in self._ready:
                self._ready[mint] = asyncio.Event()
            task = self._tasks.get(mint)
            if task is not None and not task.done():
                return
            self._tasks[mint] = asyncio.create_task(
                self._run(mint), name=f"price-{mint[:8]}"
            )
        log.info("live price start %s", mint)

    async def release(self, mint: str) -> None:
        """drop one ref. the socket stops when nobody is left."""
        mint = mint.strip()
        task: asyncio.Task[None] | None = None
        async with self._lock:
            left = self._refs.get(mint, 0) - 1
            if left > 0:
                self._refs[mint] = left
                return
            self._refs.pop(mint, None)
            self._latest.pop(mint, None)
            self._ready.pop(mint, None)
            self._hints.pop(mint, None)
            self._listeners.pop(mint, None)
            task = self._tasks.get(mint)
            # inside a tick callback — the loop notices refs hit 0 and exits
            if task is not None and task is asyncio.current_task():
                return
            self._tasks.pop(mint, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        log.info("live price stop %s", mint)

    def add_listener(self, mint: str, fn: TickFn) -> None:
        """called on the socket task for every tick, including the seed."""
        self._listeners.setdefault(mint.strip(), []).append(fn)

    def remove_listener(self, mint: str, fn: TickFn) -> None:
        mint = mint.strip()
        rows = self._listeners.get(mint)
        if not rows:
            return
        self._listeners[mint] = [row for row in rows if row is not fn]

    async def _run(self, mint: str) -> None:
        """keep the vault socket up until the refcount hits 0."""
        try:
            while True:
                async with self._lock:
                    if self._refs.get(mint, 0) <= 0:
                        return
                    pool, name, symbol = self._hints.get(mint, (None, None, None))
                try:
                    async for tick in aiter_price_ticks_ws(
                        self.rpc,
                        mint,
                        known_pool=pool,
                        name=name,
                        symbol=symbol,
                        ledger_path=self.ledger_path,
                    ):
                        self._latest[mint] = tick
                        ev = self._ready.get(mint)
                        if ev is not None and not ev.is_set():
                            ev.set()
                        for fn in list(self._listeners.get(mint, ())):
                            try:
                                await fn(tick)
                            except Exception:  # noqa: BLE001
                                log.exception("live price listener failed %s", mint)
                        if self._refs.get(mint, 0) <= 0:
                            return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("live price error %s", mint)
                    if self._refs.get(mint, 0) <= 0:
                        return
                    await asyncio.sleep(1.0)
        finally:
            async with self._lock:
                cur = self._tasks.get(mint)
                if cur is asyncio.current_task():
                    self._tasks.pop(mint, None)
