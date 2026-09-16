"""
paper buy/sell fills at the current PumpSwap spot (no fees yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from archetype.execution.wallet import (
    Position,
    delete_position,
    get_balance,
    get_position,
    list_positions,
    record_closed_trade,
    set_balance,
    upsert_position,
)
from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.price.models import PriceTick
from archetype.price.resolve import ResolvedPool
from archetype.price.snapshot import fetch_price_tick, fetch_price_tick_resolved

# dust leftover → treat as full close
_DUST_TOKENS = 1e-9


@dataclass
class FillResult:
    """outcome of a paper buy or sell."""

    side: str  # buy | sell
    mint: str
    sol: float
    tokens: float
    price_sol: float
    fdv_usd: float
    position: Position | None
    wallet_sol: float
    pnl_sol: float | None = None
    pnl_pct: float | None = None
    name: str | None = None
    symbol: str | None = None
    mode: str = "full"  # full | percent | sol | init
    resolved: ResolvedPool | None = None


@dataclass
class MarkedPosition:
    """open position + one-shot mark-to-market."""

    position: Position
    price_sol: float
    fdv_usd: float
    value_sol: float
    pnl_sol: float
    pnl_pct: float
    name: str | None
    symbol: str | None


def _price_kwargs(ledger_path: Path, pos: Position | None = None) -> dict:
    """shared resolve hints from an open position / ledger cache."""
    return {
        "ledger_path": ledger_path,
        "known_pool": pos.pool if pos else None,
        "name": pos.name if pos else None,
        "symbol": pos.symbol if pos else None,
    }


def buy(
    ledger_path: Path,
    rpc: HeliusRpc,
    mint: str,
    size_sol: float,
    *,
    tick: PriceTick | None = None,
    resolved: ResolvedPool | None = None,
) -> FillResult:
    """
    spend size_sol of mock cash at current price.

    tokens = size_sol / price_sol (simple, no slippage).
    rebuy on same mint averages cost basis; initial_cost_sol stays.
    """
    if size_sol <= 0:
        raise ValueError("buy size must be > 0")

    bal = get_balance(ledger_path)
    if size_sol > bal + 1e-12:
        raise ValueError(
            f"insufficient balance: need {size_sol:.4f} SOL, have {bal:.4f} SOL"
        )

    existing = get_position(ledger_path, mint)
    if tick is None:
        tick, resolved = fetch_price_tick_resolved(
            rpc,
            mint,
            resolved=resolved,
            **_price_kwargs(ledger_path, existing),
        )
    if tick.price_sol <= 0:
        raise ValueError("invalid price")

    tokens_bought = size_sol / tick.price_sol

    if existing:
        new_tokens = existing.tokens + tokens_bought
        new_cost = existing.cost_sol + size_sol
        entry_price = new_cost / new_tokens if new_tokens > 0 else tick.price_sol
        name = tick.name or existing.name
        symbol = tick.symbol or existing.symbol
        if existing.entry_fdv_usd is not None and existing.cost_sol > 0:
            entry_fdv = (
                existing.entry_fdv_usd * existing.cost_sol + tick.fdv_usd * size_sol
            ) / new_cost
        else:
            entry_fdv = tick.fdv_usd
        initial_cost = existing.initial_cost_sol
        bought_sol = existing.bought_sol + size_sol
        sold_sol = existing.sold_sol
    else:
        new_tokens = tokens_bought
        new_cost = size_sol
        entry_price = tick.price_sol
        name = tick.name
        symbol = tick.symbol
        entry_fdv = tick.fdv_usd
        initial_cost = size_sol
        bought_sol = size_sol
        sold_sol = 0.0

    new_bal = set_balance(ledger_path, bal - size_sol)
    pos = upsert_position(
        ledger_path,
        mint=mint,
        name=name,
        symbol=symbol,
        tokens=new_tokens,
        cost_sol=new_cost,
        entry_price_sol=entry_price,
        entry_fdv_usd=entry_fdv,
        initial_cost_sol=initial_cost,
        pool=tick.pool,
        bought_sol=bought_sol,
        sold_sol=sold_sol,
    )
    return FillResult(
        side="buy",
        mint=mint,
        sol=size_sol,
        tokens=tokens_bought,
        price_sol=tick.price_sol,
        fdv_usd=tick.fdv_usd,
        position=pos,
        wallet_sol=new_bal,
        name=name,
        symbol=symbol,
        resolved=resolved,
    )


def sell(
    ledger_path: Path,
    rpc: HeliusRpc,
    mint: str,
    *,
    percent: float | None = None,
    sol_amount: float | None = None,
    initials: bool = False,
    tick: PriceTick | None = None,
    resolved: ResolvedPool | None = None,
) -> FillResult:
    """
    sell open position (full / % / SOL out / initials).

    - no args → 100%
    - percent=25 → 25% of tokens
    - sol_amount=5 → sell ~5 SOL worth of tokens
    - initials=True → sell enough to recover first-buy SOL; fails if underwater
    """
    modes = sum(
        [
            percent is not None,
            sol_amount is not None,
            initials,
        ]
    )
    if modes > 1:
        raise ValueError("use only one of percent / sol amount / init")

    pos = get_position(ledger_path, mint)
    if pos is None:
        raise ValueError(f"no open position for {mint}")

    if tick is None:
        tick, resolved = fetch_price_tick_resolved(
            rpc,
            mint,
            resolved=resolved,
            **_price_kwargs(ledger_path, pos),
        )
    if tick.price_sol <= 0:
        raise ValueError("invalid price")

    value_sol = pos.tokens * tick.price_sol
    mode = "full"

    if initials:
        mode = "init"
        init = float(pos.initial_cost_sol or 0.0)
        if init <= 0:
            raise ValueError(
                "no initials left to sell "
                "(already recovered, or initial size unknown)"
            )
        if value_sol + 1e-9 < init:
            raise ValueError(
                f"cannot sell initials at a loss: "
                f"position value={value_sol:.4f} SOL < "
                f"initials={init:.4f} SOL"
            )
        target_sol = init
        tokens_sold = target_sol / tick.price_sol
        if tokens_sold > pos.tokens:
            tokens_sold = pos.tokens
        proceeds = tokens_sold * tick.price_sol
        cost_sold = min(init, pos.cost_sol)
        new_initial = 0.0
    elif sol_amount is not None:
        mode = "sol"
        if sol_amount <= 0:
            raise ValueError("SOL amount must be > 0")
        if sol_amount > value_sol + 1e-9:
            raise ValueError(
                f"not enough position value: want {sol_amount:.4f} SOL, "
                f"have {value_sol:.4f} SOL"
            )
        tokens_sold = sol_amount / tick.price_sol
        if tokens_sold > pos.tokens:
            tokens_sold = pos.tokens
        proceeds = tokens_sold * tick.price_sol
        frac = tokens_sold / pos.tokens if pos.tokens > 0 else 1.0
        cost_sold = pos.cost_sol * frac
        new_initial = pos.initial_cost_sol * (1.0 - frac)
    elif percent is not None:
        mode = "percent"
        if percent <= 0 or percent > 100:
            raise ValueError("percent must be in (0, 100]")
        frac = percent / 100.0
        tokens_sold = pos.tokens * frac
        proceeds = tokens_sold * tick.price_sol
        cost_sold = pos.cost_sol * frac
        new_initial = pos.initial_cost_sol * (1.0 - frac)
    else:
        mode = "full"
        tokens_sold = pos.tokens
        proceeds = value_sol
        cost_sold = pos.cost_sol
        new_initial = 0.0

    pnl_sol = proceeds - cost_sold
    pnl_pct = (pnl_sol / cost_sold * 100.0) if cost_sold > 0 else 0.0

    bal = get_balance(ledger_path)
    new_bal = set_balance(ledger_path, bal + proceeds)

    remaining_tokens = pos.tokens - tokens_sold
    remaining_cost = max(0.0, pos.cost_sol - cost_sold)

    if remaining_tokens <= _DUST_TOKENS or mode == "full":
        record_closed_trade(
            ledger_path,
            mint=mint,
            entry_price=pos.entry_price_sol,
            exit_price=tick.price_sol,
            size_sol=cost_sold,
            pnl_sol=pnl_sol,
            opened_at=pos.opened_at,
            status="closed",
        )
        delete_position(ledger_path, mint)
        remaining_pos: Position | None = None
    else:
        entry_price = (
            remaining_cost / remaining_tokens
            if remaining_tokens > 0
            else pos.entry_price_sol
        )
        remaining_pos = upsert_position(
            ledger_path,
            mint=mint,
            name=tick.name or pos.name,
            symbol=tick.symbol or pos.symbol,
            tokens=remaining_tokens,
            cost_sol=remaining_cost,
            entry_price_sol=entry_price,
            entry_fdv_usd=pos.entry_fdv_usd,
            initial_cost_sol=max(0.0, new_initial),
            pool=tick.pool or pos.pool,
            bought_sol=pos.bought_sol,  # never shrinks on sell
            sold_sol=pos.sold_sol + proceeds,
        )
        record_closed_trade(
            ledger_path,
            mint=mint,
            entry_price=pos.entry_price_sol,
            exit_price=tick.price_sol,
            size_sol=cost_sold,
            pnl_sol=pnl_sol,
            opened_at=pos.opened_at,
            status=f"partial:{mode}",
        )

    return FillResult(
        side="sell",
        mint=mint,
        sol=proceeds,
        tokens=tokens_sold,
        price_sol=tick.price_sol,
        fdv_usd=tick.fdv_usd,
        position=remaining_pos,
        wallet_sol=new_bal,
        pnl_sol=pnl_sol,
        pnl_pct=pnl_pct,
        name=tick.name or pos.name,
        symbol=tick.symbol or pos.symbol,
        mode=mode,
        resolved=resolved,
    )


def mark_position(
    rpc: HeliusRpc,
    pos: Position,
    *,
    tick: PriceTick | None = None,
    ledger_path: Path | None = None,
) -> MarkedPosition:
    """one-shot mark-to-market for balance display."""
    if tick is None:
        tick = fetch_price_tick(
            rpc,
            pos.mint,
            known_pool=pos.pool,
            name=pos.name,
            symbol=pos.symbol,
            ledger_path=ledger_path,
        )
    value_sol = pos.tokens * tick.price_sol
    pnl_sol = value_sol - pos.cost_sol
    pnl_pct = (pnl_sol / pos.cost_sol * 100.0) if pos.cost_sol > 0 else 0.0
    return MarkedPosition(
        position=pos,
        price_sol=tick.price_sol,
        fdv_usd=tick.fdv_usd,
        value_sol=value_sol,
        pnl_sol=pnl_sol,
        pnl_pct=pnl_pct,
        name=tick.name or pos.name,
        symbol=tick.symbol or pos.symbol,
    )


def mark_all_positions(
    ledger_path: Path,
    rpc: HeliusRpc,
) -> list[MarkedPosition]:
    """
    mark every open position — shared SOL/USD + cached resolves.

    cheaper than N independent mark_position calls.
    """
    positions = list_positions(ledger_path)
    if not positions:
        return []
    sol_usd = get_sol_usd(rpc)
    out: list[MarkedPosition] = []
    for pos in positions:
        tick = fetch_price_tick(
            rpc,
            pos.mint,
            sol_usd=sol_usd,
            known_pool=pos.pool,
            name=pos.name,
            symbol=pos.symbol,
            ledger_path=ledger_path,
        )
        out.append(mark_position(rpc, pos, tick=tick, ledger_path=ledger_path))
    return out
