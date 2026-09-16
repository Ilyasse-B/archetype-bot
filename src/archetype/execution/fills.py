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
    record_closed_trade,
    set_balance,
    upsert_position,
)
from archetype.filters.helius import HeliusRpc
from archetype.price.models import PriceTick
from archetype.price.snapshot import fetch_price_tick


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


def buy(
    ledger_path: Path,
    rpc: HeliusRpc,
    mint: str,
    size_sol: float,
    *,
    tick: PriceTick | None = None,
) -> FillResult:
    """
    spend size_sol of mock cash at current price.

    tokens = size_sol / price_sol (simple, no slippage).
    rebuy on same mint averages cost basis.
    """
    if size_sol <= 0:
        raise ValueError("buy size must be > 0")

    bal = get_balance(ledger_path)
    if size_sol > bal + 1e-12:
        raise ValueError(
            f"insufficient balance: need {size_sol:.4f} SOL, have {bal:.4f} SOL"
        )

    tick = tick or fetch_price_tick(rpc, mint)
    if tick.price_sol <= 0:
        raise ValueError("invalid price")

    tokens_bought = size_sol / tick.price_sol
    existing = get_position(ledger_path, mint)

    if existing:
        new_tokens = existing.tokens + tokens_bought
        new_cost = existing.cost_sol + size_sol
        entry_price = new_cost / new_tokens if new_tokens > 0 else tick.price_sol
        name = tick.name or existing.name
        symbol = tick.symbol or existing.symbol
        # cost-weighted avg entry FDV across fills
        if existing.entry_fdv_usd is not None and existing.cost_sol > 0:
            entry_fdv = (
                existing.entry_fdv_usd * existing.cost_sol + tick.fdv_usd * size_sol
            ) / new_cost
        else:
            entry_fdv = tick.fdv_usd
    else:
        new_tokens = tokens_bought
        new_cost = size_sol
        entry_price = tick.price_sol
        name = tick.name
        symbol = tick.symbol
        entry_fdv = tick.fdv_usd

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
    )


def sell(
    ledger_path: Path,
    rpc: HeliusRpc,
    mint: str,
    *,
    tick: PriceTick | None = None,
) -> FillResult:
    """full exit of open position at current price."""
    pos = get_position(ledger_path, mint)
    if pos is None:
        raise ValueError(f"no open position for {mint}")

    tick = tick or fetch_price_tick(rpc, mint)
    if tick.price_sol <= 0:
        raise ValueError("invalid price")

    proceeds = pos.tokens * tick.price_sol
    pnl_sol = proceeds - pos.cost_sol
    pnl_pct = (pnl_sol / pos.cost_sol * 100.0) if pos.cost_sol > 0 else 0.0

    bal = get_balance(ledger_path)
    new_bal = set_balance(ledger_path, bal + proceeds)
    record_closed_trade(
        ledger_path,
        mint=mint,
        entry_price=pos.entry_price_sol,
        exit_price=tick.price_sol,
        size_sol=pos.cost_sol,
        pnl_sol=pnl_sol,
        opened_at=pos.opened_at,
    )
    delete_position(ledger_path, mint)

    return FillResult(
        side="sell",
        mint=mint,
        sol=proceeds,
        tokens=pos.tokens,
        price_sol=tick.price_sol,
        fdv_usd=tick.fdv_usd,
        position=None,
        wallet_sol=new_bal,
        pnl_sol=pnl_sol,
        pnl_pct=pnl_pct,
        name=tick.name or pos.name,
        symbol=tick.symbol or pos.symbol,
    )


def mark_position(
    rpc: HeliusRpc,
    pos: Position,
    *,
    tick: PriceTick | None = None,
) -> MarkedPosition:
    """one-shot mark-to-market for balance display."""
    tick = tick or fetch_price_tick(rpc, pos.mint)
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
