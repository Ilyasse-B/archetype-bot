"""
move-alert engine — daemon WS watcher that queues large %% moves for the agent.

one vault WS per subscribed mint. baseline resets after each alert so we
dont spam the same move. independent of TP/SL (stops when unsubscribed).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archetype.execution.wallet import get_position
from archetype.filters.helius import HeliusRpc
from archetype.price.stream import aiter_price_ticks_ws
from archetype.tools.models import MoveAlert

log = logging.getLogger("archetype.alerts")

DEFAULT_THRESHOLD_PCT = 10.0
MAX_PENDING = 50


@dataclass
class _Sub:
    mint: str
    threshold_pct: float
    baseline_price: float | None = None
    baseline_fdv: float | None = None
    name: str | None = None
    symbol: str | None = None
    pool: str | None = None


@dataclass
class MoveAlertEngine:
    """background % move monitors + pending queue for the agent."""

    rpc: HeliusRpc
    ledger_path: Path
    default_threshold_pct: float = DEFAULT_THRESHOLD_PCT
    _subs: dict[str, _Sub] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _pending: deque[MoveAlert] = field(default_factory=deque)
    _seq: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        log.info(
            "move-alert engine: ready (default threshold=%.1f%%)",
            self.default_threshold_pct,
        )

    async def stop(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            self._subs.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("move-alert engine: stopped")

    def status(self) -> dict[str, Any]:
        return {
            "subscriptions": [
                {
                    "mint": s.mint,
                    "threshold_pct": s.threshold_pct,
                    "monitoring": s.mint in self._tasks,
                    "baseline_price_sol": s.baseline_price,
                    "baseline_fdv_usd": s.baseline_fdv,
                }
                for s in self._subs.values()
            ],
            "pending_count": len(self._pending),
        }

    async def subscribe(
        self,
        mint: str,
        *,
        threshold_pct: float | None = None,
    ) -> dict[str, Any]:
        mint = mint.strip()
        thr = float(
            threshold_pct
            if threshold_pct is not None
            else self.default_threshold_pct
        )
        if thr <= 0:
            raise ValueError("threshold_pct must be > 0")

        pos = await asyncio.to_thread(get_position, self.ledger_path, mint)
        sub = _Sub(
            mint=mint,
            threshold_pct=thr,
            name=pos.name if pos else None,
            symbol=pos.symbol if pos else None,
            pool=pos.pool if pos else None,
        )
        async with self._lock:
            self._subs[mint] = sub
            existing = self._tasks.get(mint)
            if existing is None or existing.done():
                self._tasks[mint] = asyncio.create_task(
                    self._watch(mint), name=f"alert-{mint[:8]}"
                )
        log.info(
            "MOVE ALERT sub %s threshold=±%.2f%%",
            mint,
            thr,
        )
        return {
            "ok": True,
            "mint": mint,
            "threshold_pct": thr,
            "monitoring": True,
        }

    async def unsubscribe(self, mint: str) -> dict[str, Any]:
        mint = mint.strip()
        async with self._lock:
            self._subs.pop(mint, None)
            task = self._tasks.pop(mint, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        log.info("MOVE ALERT unsub %s", mint)
        return {"ok": True, "mint": mint, "monitoring": False}

    def drain(self, *, limit: int = 20) -> list[MoveAlert]:
        """pop pending alerts oldest-first."""
        out: list[MoveAlert] = []
        while self._pending and len(out) < max(1, limit):
            out.append(self._pending.popleft())
        return out

    async def _watch(self, mint: str) -> None:
        try:
            sub = self._subs.get(mint)
            if sub is None:
                return
            log.info("MOVE ALERT watch start %s", mint)
            async for tick in aiter_price_ticks_ws(
                self.rpc,
                mint,
                known_pool=sub.pool,
                name=sub.name,
                symbol=sub.symbol,
                ledger_path=self.ledger_path,
            ):
                sub = self._subs.get(mint)
                if sub is None:
                    break
                # seed baseline from first tick
                if sub.baseline_price is None or sub.baseline_price <= 0:
                    sub.baseline_price = float(tick.price_sol)
                    sub.baseline_fdv = float(tick.fdv_usd)
                    sub.name = tick.name or sub.name
                    sub.symbol = tick.symbol or sub.symbol
                    continue

                base = sub.baseline_price
                cur = float(tick.price_sol)
                if base <= 0 or cur <= 0:
                    continue
                change = (cur - base) / base * 100.0
                if abs(change) < sub.threshold_pct:
                    continue

                self._seq += 1
                alert = MoveAlert(
                    mint=mint,
                    direction="up" if change > 0 else "down",
                    change_pct=float(change),
                    from_price_sol=base,
                    to_price_sol=cur,
                    from_fdv_usd=float(sub.baseline_fdv or 0),
                    to_fdv_usd=float(tick.fdv_usd),
                    name=tick.name or sub.name,
                    symbol=tick.symbol or sub.symbol,
                    threshold_pct=sub.threshold_pct,
                    seq=self._seq,
                    ts=datetime.now(timezone.utc).isoformat(),
                )
                self._pending.append(alert)
                while len(self._pending) > MAX_PENDING:
                    self._pending.popleft()
                # reset baseline so next move is from here
                sub.baseline_price = cur
                sub.baseline_fdv = float(tick.fdv_usd)
                sign = "+" if change > 0 else ""
                log.info(
                    "MOVE ALERT %s %s %s%.2f%%  FDV $%.0f → $%.0f",
                    alert.direction.upper(),
                    alert.symbol or mint[:8],
                    sign,
                    change,
                    alert.from_fdv_usd,
                    alert.to_fdv_usd,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("MOVE ALERT watch error %s: %s", mint, exc)
        finally:
            async with self._lock:
                self._tasks.pop(mint, None)
            log.info("MOVE ALERT watch stop %s", mint)
