"""
agent trade helpers — buy requires TP+SL; thin wrappers over paper fills.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archetype.agent.store import record_reject
from archetype.execution.fills import buy, sell
from archetype.filters.helius import HeliusRpc
from archetype.price.models import PriceTick


def agent_buy(
    ledger_path: Path,
    rpc: HeliusRpc,
    mint: str,
    size_sol: float,
    *,
    profit_pct: float,
    loss_pct: float,
    tick: PriceTick | None = None,
    name: str | None = None,
    symbol: str | None = None,
    known_pool: str | None = None,
) -> dict[str, Any]:
    """
    paper buy that refuses without TP+SL.

    caller should arm tpsl engine after this returns.
    """
    mint = mint.strip()
    if profit_pct <= 0 or loss_pct <= 0:
        raise ValueError("profit_pct and loss_pct must both be > 0")

    fill = buy(
        ledger_path,
        rpc,
        mint,
        size_sol,
        tick=tick,
        name=name,
        symbol=symbol,
        known_pool=known_pool,
    )
    return {
        "fill": fill,
        "profit_pct": float(profit_pct),
        "loss_pct": float(loss_pct),
    }


def agent_sell(
    ledger_path: Path,
    rpc: HeliusRpc,
    mint: str,
    *,
    percent: float | None = None,
    sol_amount: float | None = None,
    initials: bool = False,
    tick: PriceTick | None = None,
    name: str | None = None,
    symbol: str | None = None,
    known_pool: str | None = None,
) -> Any:
    """paper sell — same modes as CLI."""
    return sell(
        ledger_path,
        rpc,
        mint,
        percent=percent,
        sol_amount=sol_amount,
        initials=initials,
        tick=tick,
        name=name,
        symbol=symbol,
        known_pool=known_pool,
    )


def agent_reject(
    ledger_path: Path,
    mint: str,
    *,
    reasoning: str = "agent reject",
) -> dict[str, Any]:
    """log reject for training / never-revisit policy."""
    rid = record_reject(
        ledger_path,
        mint.strip(),
        reasoning=reasoning,
        decision="reject",
    )
    return {"ok": True, "id": rid, "mint": mint.strip(), "reasoning": reasoning}
