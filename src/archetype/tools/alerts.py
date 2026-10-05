"""
move-alert engine — daemon WS watcher for a price move.

one vault WS per coin the agent is actually on. the alert is the net
change over about 10 seconds, not every tick. after an alert the window
starts again from the new price.
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
from archetype.price.live import LivePrices
from archetype.price.models import PriceTick
from archetype.price.stream import aiter_price_ticks_ws
from archetype.tools.models import MoveAlert
from archetype.tools.price_change import (
    DEFAULT_FAST_MOVE_PCT,
    DEFAULT_FAST_RULES,
    DEFAULT_FAST_WINDOW_SEC,
    MIN_FAST_MOVE_PCT,
    detect_window_move,
    first_alert_note,
    trim_samples,
)

log = logging.getLogger("archetype.alerts")

# old name kept so imports dont break. this is the fast-move size, not "any 10%".
DEFAULT_THRESHOLD_PCT = DEFAULT_FAST_MOVE_PCT
MAX_PENDING = 50


@dataclass
class _Sub:
    mint: str
    threshold_pct: float
    window_sec: float = DEFAULT_FAST_WINDOW_SEC
    # (percent or more, max seconds). either rule can fire.
    rules: list[tuple[float, float]] = field(
        default_factory=lambda: list(DEFAULT_FAST_RULES)
    )
    # nothing smaller than this alerts, even if it was very fast
    min_pct: float = MIN_FAST_MOVE_PCT
    # (ts, price, fdv), oldest first
    samples: list[tuple[float, float, float]] = field(default_factory=list)
    name: str | None = None
    symbol: str | None = None
    pool: str | None = None
    # long sentence already sent for this watch
    coached: bool = False


@dataclass
class MoveAlertEngine:
    """background % move monitors + pending queue for the agent."""

    rpc: HeliusRpc
    ledger_path: Path
    # shared vault socket. peek reads the latest tick off this.
    live: LivePrices | None = None
    default_threshold_pct: float = DEFAULT_THRESHOLD_PCT
    default_window_sec: float = DEFAULT_FAST_WINDOW_SEC
    default_min_pct: float = MIN_FAST_MOVE_PCT
    default_rules: list[tuple[float, float]] = field(
        default_factory=lambda: list(DEFAULT_FAST_RULES)
    )
    _hooks: dict[str, Any] = field(default_factory=dict)
    _subs: dict[str, _Sub] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _pending: deque[MoveAlert] = field(default_factory=deque)
    _seq: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        log.info(
            "move-alert engine: ready (net move >=%.0f%% over %.0fs)",
            self.default_min_pct,
            self.default_window_sec,
        )

    async def stop(self) -> None:
        async with self._lock:
            mints = list(self._subs)
        for mint in mints:
            await self.unsubscribe(mint)
        log.info("move-alert engine: stopped")

    def status(self) -> dict[str, Any]:
        return {
            "subscriptions": [
                {
                    "mint": s.mint,
                    "threshold_pct": s.threshold_pct,
                    "window_sec": s.window_sec,
                    "monitoring": s.mint in self._hooks or s.mint in self._tasks,
                    "last_price_sol": s.samples[-1][1] if s.samples else None,
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
        window_sec: float | None = None,
        name: str | None = None,
        symbol: str | None = None,
        pool: str | None = None,
    ) -> dict[str, Any]:
        mint = mint.strip()
        thr = float(
            threshold_pct
            if threshold_pct is not None
            else self.default_threshold_pct
        )
        window = float(
            window_sec if window_sec is not None else self.default_window_sec
        )
        if thr <= 0:
            raise ValueError("threshold_pct must be > 0")
        if window <= 0:
            raise ValueError("window_sec must be > 0")

        async with self._lock:
            # already watching — dont restart or the fast window gets wiped
            if self._is_on(mint):
                return self._already(mint)
        pos = await asyncio.to_thread(get_position, self.ledger_path, mint)
        async with self._lock:
            if self._is_on(mint):
                return self._already(mint)
            sub = _Sub(
                mint=mint,
                threshold_pct=thr,
                window_sec=window,
                rules=list(self.default_rules),
                min_pct=float(self.default_min_pct),
                name=name or (pos.name if pos else None),
                symbol=symbol or (pos.symbol if pos else None),
                pool=pool or (pos.pool if pos else None),
            )
            self._subs[mint] = sub
            # same socket peek and tp/sl already use. no second one.
            if self.live is not None:
                hook = self._make_hook(mint)
                self._hooks[mint] = hook
            else:
                self._tasks[mint] = asyncio.create_task(
                    self._watch(mint), name=f"alert-{mint[:8]}"
                )
        if self.live is not None:
            await self.live.acquire(
                mint, pool=sub.pool, name=sub.name, symbol=sub.symbol
            )
            hook = self._hooks.get(mint)
            if hook is not None:
                self.live.add_listener(mint, hook)
        log.info(
            "MOVE ALERT sub %s net >=%.0f%% over %.0fs",
            mint,
            sub.min_pct,
            window,
        )
        return {
            "ok": True,
            "mint": mint,
            "threshold_pct": thr,
            "window_sec": window,
            "monitoring": True,
        }

    async def unsubscribe(self, mint: str) -> dict[str, Any]:
        mint = mint.strip()
        async with self._lock:
            self._subs.pop(mint, None)
            task = self._tasks.pop(mint, None)
            hook = self._hooks.pop(mint, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hook is not None and self.live is not None:
            self.live.remove_listener(mint, hook)
            await self.live.release(mint)
        # dont let this coin's queued alerts show up on the next one
        self._pending = deque(a for a in self._pending if a.mint != mint)
        log.info("MOVE ALERT unsub %s", mint)
        return {"ok": True, "mint": mint, "monitoring": False}

    def consider(
        self,
        mint: str,
        price: float,
        fdv: float,
        ts: float,
        *,
        name: str | None = None,
        symbol: str | None = None,
    ) -> MoveAlert | None:
        """
        one tick. returns an alert only when the move was fast.

        tests call this directly. the websocket loop does too.
        """
        sub = self._subs.get(mint)
        if sub is None:
            return None
        price = float(price)
        fdv = float(fdv)
        ts = float(ts)
        if price <= 0:
            return None
        if name:
            sub.name = name
        if symbol:
            sub.symbol = symbol
        hit = detect_window_move(
            sub.samples,
            price,
            ts,
            window_sec=sub.window_sec,
            min_pct=sub.min_pct,
        )
        if hit is not None:
            # next window starts here. dont keep the ticks that already alerted.
            sub.samples = [(ts, price, fdv)]
        else:
            sub.samples.append((ts, price, fdv))
            sub.samples = trim_samples(sub.samples, ts, keep_sec=sub.window_sec)
        if hit is None:
            return None
        from_price, from_fdv, elapsed, change = hit
        direction = "up" if change > 0 else "down"
        note = ""
        if not sub.coached:
            note = first_alert_note(direction, change, elapsed, from_fdv, fdv)
            sub.coached = True
        self._seq += 1
        alert = MoveAlert(
            mint=mint,
            direction=direction,  # type: ignore[arg-type]
            change_pct=float(change),
            elapsed_sec=float(elapsed),
            from_price_sol=float(from_price),
            to_price_sol=price,
            from_fdv_usd=float(from_fdv),
            to_fdv_usd=fdv,
            name=sub.name,
            symbol=sub.symbol,
            threshold_pct=sub.threshold_pct,
            note=note,
            seq=self._seq,
            ts=datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        )
        self._pending.append(alert)
        while len(self._pending) > MAX_PENDING:
            self._pending.popleft()
        sign = "+" if change > 0 else ""
        log.info(
            "MOVE ALERT %s %s %s%.1f%% in %.1fs",
            alert.direction.upper(),
            alert.symbol or mint[:8],
            sign,
            change,
            elapsed,
        )
        return alert

    def drain(self, *, limit: int = 20) -> list[MoveAlert]:
        """pop pending alerts oldest-first."""
        out: list[MoveAlert] = []
        while self._pending and len(out) < max(1, limit):
            out.append(self._pending.popleft())
        return out

    def _is_on(self, mint: str) -> bool:
        """sub is live, either on the shared socket or the old private one."""
        if mint not in self._subs:
            return False
        if mint in self._hooks:
            return True
        task = self._tasks.get(mint)
        return task is not None and not task.done()

    def _already(self, mint: str) -> dict[str, Any]:
        sub = self._subs[mint]
        return {
            "ok": True,
            "mint": mint,
            "threshold_pct": sub.threshold_pct,
            "window_sec": sub.window_sec,
            "monitoring": True,
            "already": True,
        }

    def _make_hook(self, mint: str):
        """one listener on the shared socket. tests never hit this."""

        async def _hook(tick: PriceTick) -> None:
            self.consider(
                mint,
                float(tick.price_sol),
                float(tick.fdv_usd),
                tick.at.timestamp(),
                name=tick.name,
                symbol=tick.symbol,
            )

        return _hook

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
                self.consider(
                    mint,
                    float(tick.price_sol),
                    float(tick.fdv_usd),
                    tick.at.timestamp(),
                    name=tick.name,
                    symbol=tick.symbol,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("MOVE ALERT watch error %s: %s", mint, exc)
        finally:
            async with self._lock:
                self._tasks.pop(mint, None)
            log.info("MOVE ALERT watch stop %s", mint)
