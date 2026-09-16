"""
paper wallet HTTP API — daemon owns fills + sqlite; CLI is the client.

live price WS stays in the CLI (no tick streaming over localhost).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from archetype.execution import (
    DEFAULT_START_SOL,
    Position,
    add_sol,
    buy,
    get_position,
    mark_all_positions,
    reset_wallet,
    sell,
)
from archetype.execution.wallet import get_balance
from archetype.filters.helius import HeliusRpc


class BuyRequest(BaseModel):
    mint: str
    sol: float = Field(..., gt=0)


class SellRequest(BaseModel):
    mint: str
    percent: float | None = Field(None, gt=0, le=100)
    sol_amount: float | None = Field(None, gt=0)
    initials: bool = False


class AddSolRequest(BaseModel):
    sol: float = Field(..., gt=0)


class ResetRequest(BaseModel):
    start: float = Field(DEFAULT_START_SOL, gt=0)


def _pos_dict(pos: Position | None) -> dict[str, Any] | None:
    if pos is None:
        return None
    return {
        "id": pos.id,
        "mint": pos.mint,
        "name": pos.name,
        "symbol": pos.symbol,
        "tokens": pos.tokens,
        "cost_sol": pos.cost_sol,
        "entry_price_sol": pos.entry_price_sol,
        "entry_fdv_usd": pos.entry_fdv_usd,
        "initial_cost_sol": pos.initial_cost_sol,
        "pool": pos.pool,
        "bought_sol": pos.bought_sol,
        "sold_sol": pos.sold_sol,
        "round_id": pos.round_id,
        "opened_at": pos.opened_at,
    }


def _fill_dict(fill: Any) -> dict[str, Any]:
    return {
        "side": fill.side,
        "mint": fill.mint,
        "sol": fill.sol,
        "tokens": fill.tokens,
        "price_sol": fill.price_sol,
        "fdv_usd": fill.fdv_usd,
        "wallet_sol": fill.wallet_sol,
        "pnl_sol": fill.pnl_sol,
        "pnl_pct": fill.pnl_pct,
        "name": fill.name,
        "symbol": fill.symbol,
        "mode": getattr(fill, "mode", "full"),
        "position": _pos_dict(fill.position),
        # resolved stays daemon-side; cli watch re-resolves via cache/pool hint
        "pool": fill.position.pool
        if fill.position is not None
        else (fill.resolved.pool.address if getattr(fill, "resolved", None) else None),
    }


def _rpc_ledger(request: Request) -> tuple[HeliusRpc, Path]:
    rpc: HeliusRpc = request.app.state.helius_rpc
    ledger: Path = request.app.state.settings.resolved_trading_ledger_db()
    if not rpc.enabled:
        raise HTTPException(status_code=503, detail="helius api key not configured")
    return rpc, ledger


def create_paper_router() -> APIRouter:
    """paper trading routes mounted on the daemon app."""
    router = APIRouter(prefix="/paper", tags=["paper"])

    @router.get("/balance")
    async def paper_balance(request: Request) -> dict[str, Any]:
        """cash + open positions with one-shot marks."""
        rpc, ledger = _rpc_ledger(request)

        def _run() -> dict[str, Any]:
            cash = get_balance(ledger)
            marks = mark_all_positions(ledger, rpc)
            return {
                "ok": True,
                "cash_sol": cash,
                "positions": [
                    {
                        **(_pos_dict(m.position) or {}),
                        "price_sol": m.price_sol,
                        "fdv_usd": m.fdv_usd,
                        "value_sol": m.value_sol,
                        "pnl_sol": m.pnl_sol,
                        "pnl_pct": m.pnl_pct,
                        "name": m.name,
                        "symbol": m.symbol,
                    }
                    for m in marks
                ],
            }

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/balance/add")
    async def paper_balance_add(
        body: AddSolRequest, request: Request
    ) -> dict[str, Any]:
        _, ledger = _rpc_ledger(request)

        def _run() -> float:
            return add_sol(ledger, body.sol)

        try:
            new_bal = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "added_sol": body.sol, "cash_sol": new_bal}

    @router.post("/balance/reset")
    async def paper_balance_reset(
        body: ResetRequest, request: Request
    ) -> dict[str, Any]:
        _, ledger = _rpc_ledger(request)

        def _run() -> float:
            return reset_wallet(ledger, start_sol=body.start)

        new_bal = await asyncio.to_thread(_run)
        return {
            "ok": True,
            "cash_sol": new_bal,
            "message": "wallet reset; open positions cleared",
        }

    @router.get("/position/{mint}")
    async def paper_position(mint: str, request: Request) -> dict[str, Any]:
        """one open position (for CLI watch PnL overlay)."""
        _, ledger = _rpc_ledger(request)
        pos = await asyncio.to_thread(get_position, ledger, mint.strip())
        if pos is None:
            return {"ok": True, "position": None}
        return {"ok": True, "position": _pos_dict(pos)}

    @router.post("/buy")
    async def paper_buy(body: BuyRequest, request: Request) -> dict[str, Any]:
        rpc, ledger = _rpc_ledger(request)
        mint = body.mint.strip()

        def _run() -> Any:
            return buy(ledger, rpc, mint, body.sol)

        try:
            fill = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "fill": _fill_dict(fill)}

    @router.post("/sell")
    async def paper_sell(body: SellRequest, request: Request) -> dict[str, Any]:
        rpc, ledger = _rpc_ledger(request)
        mint = body.mint.strip()
        modes = sum(
            [
                body.percent is not None,
                body.sol_amount is not None,
                body.initials,
            ]
        )
        if modes > 1:
            raise HTTPException(
                status_code=400,
                detail="use only one of percent / sol_amount / initials",
            )

        def _run() -> Any:
            return sell(
                ledger,
                rpc,
                mint,
                percent=body.percent,
                sol_amount=body.sol_amount,
                initials=body.initials,
            )

        try:
            fill = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "fill": _fill_dict(fill)}

    return router
