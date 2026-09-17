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
from archetype.price.models import PUMP_FDV_SUPPLY, PriceTick, build_tick


def _tick_from_body(
    body: BuyRequest | SellRequest,
    mint: str,
) -> PriceTick | None:
    """rebuild a PriceTick from optional CLI watch snapshot fields."""
    if body.price_sol is None:
        return None
    price = float(body.price_sol)
    # prefer fdv→sol_usd so stored fdv_usd matches what the CLI showed
    if body.fdv_usd is not None and price > 0:
        sol_usd = float(body.fdv_usd) / (price * PUMP_FDV_SUPPLY)
    elif body.sol_usd is not None and body.sol_usd > 0:
        sol_usd = float(body.sol_usd)
    else:
        sol_usd = 150.0
    # synthesize reserves consistent with price (price = quote/base)
    base_ui = PUMP_FDV_SUPPLY
    quote_sol = price * base_ui
    return build_tick(
        mint=mint,
        pool=body.pool or "",
        base_ui=base_ui,
        quote_sol=quote_sol,
        supply_ui=base_ui,
        decimals=6,
        sol_usd=sol_usd,
        name=body.name,
        symbol=body.symbol,
        source="cli",
    )


class BuyRequest(BaseModel):
    mint: str
    sol: float = Field(..., gt=0)
    # optional hints / last watch tick so fills match what the CLI showed
    name: str | None = None
    symbol: str | None = None
    pool: str | None = None
    price_sol: float | None = Field(None, gt=0)
    fdv_usd: float | None = Field(None, gt=0)
    sol_usd: float | None = Field(None, gt=0)


class SellRequest(BaseModel):
    mint: str
    percent: float | None = Field(None, gt=0, le=100)
    sol_amount: float | None = Field(None, gt=0)
    initials: bool = False
    name: str | None = None
    symbol: str | None = None
    pool: str | None = None
    price_sol: float | None = Field(None, gt=0)
    fdv_usd: float | None = Field(None, gt=0)
    sol_usd: float | None = Field(None, gt=0)


class TpslRequest(BaseModel):
    """
    set / clear take-profit and stop-loss percentages.

    omit a field to leave it unchanged; send null to clear that side.
    """

    mint: str
    profit_pct: float | None = Field(None, description="+% pnl to take profit")
    loss_pct: float | None = Field(None, description="−% pnl stop (positive)")
    clear_profit: bool = False
    clear_loss: bool = False


class AddSolRequest(BaseModel):
    sol: float = Field(..., gt=0)


class ResetRequest(BaseModel):
    start: float = Field(DEFAULT_START_SOL, gt=0)


def _pos_dict(
    pos: Position | None,
    *,
    tpsl: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if pos is None:
        return None
    out: dict[str, Any] = {
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
    if tpsl:
        out["profit_pct"] = tpsl.get("profit_pct")
        out["loss_pct"] = tpsl.get("loss_pct")
        out["tpsl_monitoring"] = tpsl.get("monitoring")
    return out


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


def _tpsl_engine(request: Request):
    engine = getattr(request.app.state, "tpsl_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="tpsl engine not ready")
    return engine


def create_paper_router() -> APIRouter:
    """paper trading routes mounted on the daemon app."""
    router = APIRouter(prefix="/paper", tags=["paper"])

    @router.get("/balance")
    async def paper_balance(request: Request) -> dict[str, Any]:
        """cash + open positions with one-shot marks."""
        rpc, ledger = _rpc_ledger(request)
        engine = getattr(request.app.state, "tpsl_engine", None)

        def _run() -> dict[str, Any]:
            cash = get_balance(ledger)
            marks = mark_all_positions(ledger, rpc)
            positions = []
            for m in marks:
                tpsl = engine.levels_dict(m.position.mint) if engine else None
                positions.append(
                    {
                        **(_pos_dict(m.position, tpsl=tpsl) or {}),
                        "price_sol": m.price_sol,
                        "fdv_usd": m.fdv_usd,
                        "value_sol": m.value_sol,
                        "pnl_sol": m.pnl_sol,
                        "pnl_pct": m.pnl_pct,
                        "name": m.name,
                        "symbol": m.symbol,
                    }
                )
            return {"ok": True, "cash_sol": cash, "positions": positions}

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
        engine = getattr(request.app.state, "tpsl_engine", None)

        def _run() -> float:
            return reset_wallet(ledger, start_sol=body.start)

        new_bal = await asyncio.to_thread(_run)
        if engine is not None:
            # wipe monitors — wallet reset clears positions
            for mint in list(getattr(engine, "_tasks", {})):
                await engine.on_position_closed(mint)
        return {
            "ok": True,
            "cash_sol": new_bal,
            "message": "wallet reset; open positions cleared",
        }

    @router.get("/position/{mint}")
    async def paper_position(mint: str, request: Request) -> dict[str, Any]:
        """one open position (for CLI watch PnL overlay) + tpsl levels."""
        _, ledger = _rpc_ledger(request)
        engine = getattr(request.app.state, "tpsl_engine", None)
        pos = await asyncio.to_thread(get_position, ledger, mint.strip())
        tpsl = engine.levels_dict(mint.strip()) if engine else None
        hit = tpsl.get("last_hit") if tpsl else None
        return {
            "ok": True,
            "position": _pos_dict(pos, tpsl=tpsl),
            "tpsl": tpsl,
            "last_hit": hit,
        }

    @router.get("/tpsl/{mint}")
    async def paper_tpsl_get(mint: str, request: Request) -> dict[str, Any]:
        """current tp/sl + whether daemon is monitoring + last hit."""
        engine = _tpsl_engine(request)
        data = engine.levels_dict(mint.strip())
        return {"ok": True, **data}

    @router.post("/tpsl")
    async def paper_tpsl_set(
        body: TpslRequest, request: Request
    ) -> dict[str, Any]:
        """configure take-profit / stop-loss % on an open position."""
        engine = _tpsl_engine(request)
        mint = body.mint.strip()

        kw: dict[str, Any] = {}
        if body.clear_profit:
            kw["profit_pct"] = None
        elif "profit_pct" in body.model_fields_set:
            if body.profit_pct is None:
                raise HTTPException(
                    status_code=400,
                    detail="profit_pct null — use clear_profit=true",
                )
            kw["profit_pct"] = body.profit_pct
        if body.clear_loss:
            kw["loss_pct"] = None
        elif "loss_pct" in body.model_fields_set:
            if body.loss_pct is None:
                raise HTTPException(
                    status_code=400,
                    detail="loss_pct null — use clear_loss=true",
                )
            kw["loss_pct"] = body.loss_pct

        if not kw:
            raise HTTPException(
                status_code=400,
                detail="pass profit_pct / loss_pct or clear_profit / clear_loss",
            )
        try:
            levels = await engine.set_levels(mint, **kw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **engine.levels_dict(mint), "active": levels.active}

    @router.post("/buy")
    async def paper_buy(body: BuyRequest, request: Request) -> dict[str, Any]:
        rpc, ledger = _rpc_ledger(request)
        mint = body.mint.strip()
        tick = _tick_from_body(body, mint)
        engine = getattr(request.app.state, "tpsl_engine", None)

        def _run() -> Any:
            return buy(
                ledger,
                rpc,
                mint,
                body.sol,
                tick=tick,
                name=body.name,
                symbol=body.symbol,
                known_pool=body.pool,
            )

        try:
            fill = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        tpsl = engine.levels_dict(mint) if engine else None
        out = _fill_dict(fill)
        if out.get("position") is not None and tpsl:
            out["position"]["profit_pct"] = tpsl.get("profit_pct")
            out["position"]["loss_pct"] = tpsl.get("loss_pct")
        return {"ok": True, "fill": out}

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
        tick = _tick_from_body(body, mint)
        engine = getattr(request.app.state, "tpsl_engine", None)

        def _run() -> Any:
            return sell(
                ledger,
                rpc,
                mint,
                percent=body.percent,
                sol_amount=body.sol_amount,
                initials=body.initials,
                tick=tick,
                name=body.name,
                symbol=body.symbol,
                known_pool=body.pool,
            )

        try:
            fill = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # flat → stop tpsl monitor
        if fill.position is None and engine is not None:
            await engine.on_position_closed(mint)
        return {"ok": True, "fill": _fill_dict(fill)}

    return router
