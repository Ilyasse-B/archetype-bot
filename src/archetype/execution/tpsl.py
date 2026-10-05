"""
daemon-side take-profit / stop-loss monitor.

when the shared live socket is up, tp/sl reads that. otherwise one vault ws.
on hit: full paper sell at the live tick, log, stash event for CLI watch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from archetype.execution.fills import sell
from archetype.execution.ledger import get_round_tpsl, set_round_tpsl
from archetype.execution.track import round_mark
from archetype.execution.wallet import get_position
from archetype.filters.helius import HeliusRpc
from archetype.price.live import LivePrices
from archetype.price.models import PriceTick
from archetype.price.stream import aiter_price_ticks_ws

log = logging.getLogger("archetype.tpsl")


@dataclass
class TpslLevels:
    """percent levels for an open round (both optional)."""

    mint: str
    round_id: int
    # take-profit: sell when aggregated pnl_pct >= this (e.g. 50 = +50%)
    profit_pct: float | None = None
    # stop-loss: sell when aggregated pnl_pct <= -this (e.g. 20 = -20%)
    loss_pct: float | None = None

    @property
    def active(self) -> bool:
        return self.profit_pct is not None or self.loss_pct is not None


@dataclass
class TpslHit:
    """last auto-exit for a mint (CLI watch can poll this)."""

    mint: str
    reason: str  # tp | sl
    pnl_pct: float
    pnl_sol: float
    sol: float
    price_sol: float
    fdv_usd: float
    round_id: int | None
    name: str | None
    symbol: str | None
    ts: str
    seq: int = 0


@dataclass
class TpslEngine:
    """
    background monitors keyed by mint.

    runs on the daemon event loop; sell() goes through to_thread.
    """

    rpc: HeliusRpc
    ledger_path: Path
    # same vault socket as peek / move alerts. none means we open our own.
    live: LivePrices | None = None
    _levels: dict[str, TpslLevels] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _live_hooks: dict[str, Any] = field(default_factory=dict)
    _hits: dict[str, TpslHit] = field(default_factory=dict)
    _seq: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # one tick at a time so the seed check and the socket dont both sell
    _gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    _started: bool = False

    async def start(self) -> None:
        """resume monitors for open positions that already have tp/sl."""
        self._started = True
        if not self.rpc.enabled:
            log.warning("tpsl engine: helius disabled — monitors off")
            return
        resumed = await asyncio.to_thread(self._load_open_levels)
        for levels in resumed:
            await self._ensure_monitor(levels)
        if resumed:
            log.info("tpsl engine: resumed %d monitor(s)", len(resumed))
        else:
            log.info("tpsl engine: ready (no open levels)")

    async def stop(self) -> None:
        """cancel all mint monitors."""
        self._started = False
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            hooks = dict(self._live_hooks)
            self._live_hooks.clear()
            self._levels.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.live is not None:
            for mint, hook in hooks.items():
                self.live.remove_listener(mint, hook)
                await self.live.release(mint)
        log.info("tpsl engine: stopped")

    def get_levels(self, mint: str) -> TpslLevels | None:
        """in-memory levels (falls back to sqlite if needed)."""
        mint = mint.strip()
        got = self._levels.get(mint)
        if got is not None:
            return got
        return self._load_one(mint)

    def get_hit(self, mint: str) -> TpslHit | None:
        return self._hits.get(mint.strip())

    def levels_dict(self, mint: str) -> dict[str, Any]:
        """json-friendly snapshot for /paper/tpsl + position."""
        lv = self.get_levels(mint)
        hit = self.get_hit(mint)
        return {
            "mint": mint.strip(),
            "profit_pct": lv.profit_pct if lv else None,
            "loss_pct": lv.loss_pct if lv else None,
            "round_id": lv.round_id if lv else None,
            "monitoring": self._is_monitoring(mint.strip()),
            "last_hit": _hit_dict(hit) if hit else None,
        }

    async def set_levels(
        self,
        mint: str,
        *,
        profit_pct: float | None | object = ...,
        loss_pct: float | None | object = ...,
    ) -> TpslLevels:
        """
        update tp and/or sl for an open position.

        pass Ellipsis to leave a side unchanged; None to clear it.
        percentages must be > 0 when set.
        """
        mint = mint.strip()
        pos = await asyncio.to_thread(get_position, self.ledger_path, mint)
        if pos is None or pos.round_id is None:
            raise ValueError(f"no open position for {mint}")

        cur = self.get_levels(mint) or TpslLevels(
            mint=mint, round_id=int(pos.round_id)
        )
        new_tp = cur.profit_pct if profit_pct is ... else profit_pct
        new_sl = cur.loss_pct if loss_pct is ... else loss_pct

        if new_tp is not None:
            new_tp = float(new_tp)
            if new_tp <= 0:
                raise ValueError("profit % must be > 0 (or clear)")
        if new_sl is not None:
            new_sl = float(new_sl)
            if new_sl <= 0:
                raise ValueError("loss % must be > 0 (or clear)")

        levels = TpslLevels(
            mint=mint,
            round_id=int(pos.round_id),
            profit_pct=new_tp,
            loss_pct=new_sl,
        )
        await asyncio.to_thread(
            set_round_tpsl,
            self.ledger_path,
            levels.round_id,
            tp=levels.profit_pct,
            sl=levels.loss_pct,
        )

        async with self._lock:
            if levels.active:
                self._levels[mint] = levels
            else:
                self._levels.pop(mint, None)

        if levels.active:
            await self._ensure_monitor(levels)
            log.info(
                "TPSL SET %s — TP=%s SL=%s (round #%s)",
                mint,
                _fmt_pct(levels.profit_pct),
                _fmt_pct(levels.loss_pct, loss=True),
                levels.round_id,
            )
        else:
            await self._cancel_monitor(mint)
            log.info("TPSL CLEARED %s — no levels left", mint)
        return levels

    async def on_position_closed(self, mint: str) -> None:
        """manual sell / external close — drop monitor."""
        mint = mint.strip()
        async with self._lock:
            self._levels.pop(mint, None)
        await self._cancel_monitor(mint)

    def _load_open_levels(self) -> list[TpslLevels]:
        """sqlite: open rounds with tp/sl + matching open position."""
        from archetype.execution.wallet import list_positions

        out: list[TpslLevels] = []
        for pos in list_positions(self.ledger_path):
            if pos.round_id is None:
                continue
            tp, sl = get_round_tpsl(self.ledger_path, int(pos.round_id))
            if tp is None and sl is None:
                continue
            out.append(
                TpslLevels(
                    mint=pos.mint,
                    round_id=int(pos.round_id),
                    profit_pct=tp,
                    loss_pct=sl,
                )
            )
        return out

    def _load_one(self, mint: str) -> TpslLevels | None:
        pos = get_position(self.ledger_path, mint)
        if pos is None or pos.round_id is None:
            return None
        tp, sl = get_round_tpsl(self.ledger_path, int(pos.round_id))
        if tp is None and sl is None:
            return None
        return TpslLevels(
            mint=mint,
            round_id=int(pos.round_id),
            profit_pct=tp,
            loss_pct=sl,
        )

    def _is_monitoring(self, mint: str) -> bool:
        if mint in self._live_hooks:
            return True
        task = self._tasks.get(mint)
        return task is not None and not task.done()

    async def _ensure_monitor(self, levels: TpslLevels) -> None:
        mint = levels.mint
        async with self._lock:
            self._levels[mint] = levels
            existing = self._tasks.get(mint)
            if existing is not None and not existing.done():
                return  # already watching; levels dict updated in place
            if mint in self._live_hooks:
                return
            # no shared socket (tests / helius off) — own vault ws
            if self.live is None:
                self._tasks[mint] = asyncio.create_task(
                    self._monitor_loop(mint), name=f"tpsl-{mint[:8]}"
                )
                return
            hook = self._make_hook(mint)
            self._live_hooks[mint] = hook
        pos = await asyncio.to_thread(get_position, self.ledger_path, mint)
        await self.live.acquire(
            mint,
            pool=pos.pool if pos else None,
            name=pos.name if pos else None,
            symbol=pos.symbol if pos else None,
        )
        self.live.add_listener(mint, hook)
        # check the price we already have, dont wait for the next trade
        tick = self.live.latest(mint)
        if tick is not None:
            await hook(tick)

    def _make_hook(self, mint: str):
        """tpsl listener on the shared socket."""

        async def _hook(tick: PriceTick) -> None:
            async with self._gate:
                stop = await self._apply_tick(mint, tick)
                if stop:
                    await self._detach_live(mint)

        return _hook

    async def _detach_live(self, mint: str) -> None:
        """drop our ref. the socket stays if peek/alerts still want it."""
        async with self._lock:
            hook = self._live_hooks.pop(mint, None)
        if hook is None or self.live is None:
            return
        self.live.remove_listener(mint, hook)
        await self.live.release(mint)

    async def _cancel_monitor(self, mint: str) -> None:
        async with self._lock:
            task = self._tasks.pop(mint, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._detach_live(mint)

    async def _monitor_loop(self, mint: str) -> None:
        """vault WS → mark pnl% → full sell on tp/sl hit."""
        try:
            pos0 = await asyncio.to_thread(get_position, self.ledger_path, mint)
            if pos0 is None:
                return
            log.info(
                "TPSL WATCH start %s pool=%s…",
                mint,
                (pos0.pool or "?")[:8],
            )
            async for tick in aiter_price_ticks_ws(
                self.rpc,
                mint,
                known_pool=pos0.pool,
                name=pos0.name,
                symbol=pos0.symbol,
                ledger_path=self.ledger_path,
            ):
                if await self._apply_tick(mint, tick):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("TPSL WATCH error %s: %s", mint, exc)
        finally:
            async with self._lock:
                self._tasks.pop(mint, None)
            log.info("TPSL WATCH stop %s", mint)

    async def _apply_tick(self, mint: str, tick: PriceTick) -> bool:
        """
        mark this tick. true means stop watching (sold, flat, or levels gone).

        false means keep going. a failed sell stays on so the next tick retries.
        """
        levels = self._levels.get(mint)
        if levels is None or not levels.active:
            return True

        pos = await asyncio.to_thread(get_position, self.ledger_path, mint)
        if pos is None:
            return True

        _value, pnl_sol, pnl_pct = round_mark(pos, tick.price_sol)
        reason: str | None = None
        if levels.profit_pct is not None and pnl_pct >= levels.profit_pct:
            reason = "tp"
        elif levels.loss_pct is not None and pnl_pct <= -levels.loss_pct:
            reason = "sl"
        if reason is None:
            return False

        try:
            fill = await asyncio.to_thread(
                sell,
                self.ledger_path,
                self.rpc,
                mint,
                tick=tick,
                known_pool=pos.pool,
                name=pos.name,
                symbol=pos.symbol,
                exit_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "TPSL %s sell failed for %s: %s",
                reason.upper(),
                mint,
                exc,
            )
            await asyncio.sleep(1.0)
            return False

        self._seq += 1
        hit = TpslHit(
            mint=mint,
            reason=reason,
            pnl_pct=float(fill.pnl_pct or pnl_pct),
            pnl_sol=float(fill.pnl_sol or pnl_sol),
            sol=float(fill.sol),
            price_sol=float(fill.price_sol),
            fdv_usd=float(fill.fdv_usd),
            round_id=fill.round_id,
            name=fill.name,
            symbol=fill.symbol,
            ts=datetime.now(timezone.utc).isoformat(),
            seq=self._seq,
        )
        self._hits[mint] = hit
        sign = "+" if hit.pnl_pct >= 0 else ""
        label = hit.symbol or hit.name or mint[:8]
        log.info(
            "TPSL HIT %s %s — sold %.4f SOL  PnL=%s%.4f SOL "
            "(%s%.2f%%)  FDV=$%.0f  round=#%s",
            reason.upper(),
            label,
            hit.sol,
            sign,
            hit.pnl_sol,
            sign,
            hit.pnl_pct,
            hit.fdv_usd,
            hit.round_id,
        )
        async with self._lock:
            self._levels.pop(mint, None)
            self._tasks.pop(mint, None)
        return True


def _fmt_pct(pct: float | None, *, loss: bool = False) -> str:
    if pct is None:
        return "—"
    return f"-{pct:g}%" if loss else f"+{pct:g}%"


def _hit_dict(hit: TpslHit) -> dict[str, Any]:
    return {
        "mint": hit.mint,
        "reason": hit.reason,
        "pnl_pct": hit.pnl_pct,
        "pnl_sol": hit.pnl_sol,
        "sol": hit.sol,
        "price_sol": hit.price_sol,
        "fdv_usd": hit.fdv_usd,
        "round_id": hit.round_id,
        "name": hit.name,
        "symbol": hit.symbol,
        "ts": hit.ts,
        "seq": hit.seq,
    }
