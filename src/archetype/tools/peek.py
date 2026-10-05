"""
peek — market cap for the agent.

while a coin is in focus this reads the live vault socket we already have.
the rpc snapshot is only for a mint nobody is watching.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from archetype.execution.track import round_mark
from archetype.execution.wallet import get_position
from archetype.filters.helius import HeliusRpc
from archetype.price.models import PriceTick
from archetype.price.snapshot import fetch_price_tick
from archetype.tools.models import PeekResult


def peek_from_tick(ledger_path: Path, tick: PriceTick) -> PeekResult:
    """build a peek from a tick we already have. no helius call."""
    return _from_tick(ledger_path, tick.mint, tick)


def peek_price(
    rpc: HeliusRpc,
    ledger_path: Path,
    mint: str,
    *,
    known_pool: str | None = None,
    name: str | None = None,
    symbol: str | None = None,
) -> PeekResult:
    """
    one rpc snapshot. only when there is no live socket for this mint.

    if we hold the mint, attach aggregated round PnL + TP/SL levels.
    """
    mint = mint.strip()
    pos = get_position(ledger_path, mint)
    hint_pool = known_pool or (pos.pool if pos else None)
    hint_name = name or (pos.name if pos else None)
    hint_symbol = symbol or (pos.symbol if pos else None)

    tick = fetch_price_tick(
        rpc,
        mint,
        known_pool=hint_pool,
        name=hint_name,
        symbol=hint_symbol,
        ledger_path=ledger_path,
        source="peek",
    )
    return _from_tick(ledger_path, mint, tick, pos=pos)


def _from_tick(
    ledger_path: Path,
    mint: str,
    tick: PriceTick,
    *,
    pos=None,
) -> PeekResult:
    """pnl + levels on top of a tick. local sqlite only."""
    if pos is None:
        pos = get_position(ledger_path, mint)

    profit_pct = None
    loss_pct = None
    if pos is not None and pos.round_id is not None:
        from archetype.execution.ledger import get_round_tpsl

        profit_pct, loss_pct = get_round_tpsl(ledger_path, int(pos.round_id))

    value_sol = pnl_sol = pnl_pct = held_sec = None
    holding = pos is not None and pos.tokens > 0
    if holding and pos is not None:
        value_sol, pnl_sol, pnl_pct = round_mark(pos, tick.price_sol)
        held_sec = _held_sec(pos.opened_at)

    return PeekResult(
        mint=mint,
        name=tick.name or (pos.name if pos else None),
        symbol=tick.symbol or (pos.symbol if pos else None),
        price_sol=float(tick.price_sol),
        price_usd=float(tick.price_usd),
        fdv_usd=float(tick.fdv_usd),
        pool_sol=float(tick.pool_sol),
        pool=tick.pool or (pos.pool if pos else None),
        sol_usd=float(tick.sol_usd),
        holding=holding,
        value_sol=value_sol,
        pnl_sol=pnl_sol,
        pnl_pct=pnl_pct,
        held_sec=held_sec,
        profit_pct=profit_pct,
        loss_pct=loss_pct,
        source=tick.source or "ws",
    )


def _held_sec(opened_at: str | None) -> int | None:
    """seconds since the position opened. none if the timestamp is junk."""
    if not opened_at:
        return None
    try:
        start = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
