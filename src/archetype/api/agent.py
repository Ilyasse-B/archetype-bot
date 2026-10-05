"""
/agent/* — clean internal API the Phase 4 LLM will call.

wraps paper fills + backlog + peek + move-alerts into one catalog.
human CLI keeps using /paper/* and local watch; agent uses these.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from archetype.api.paper import _fill_dict, _pos_dict, _tick_from_body
from archetype.execution import (
    get_position,
    mark_all_positions,
)
from archetype.execution.wallet import get_balance
from archetype.filters.helius import HeliusRpc
from archetype.tools import (
    agent_reject,
    agent_sell,
    next_candidate,
    peek_backlog,
    peek_from_tick,
    peek_price,
)
from archetype.tools.alerts import MoveAlertEngine
from archetype.tools.trade_tools import agent_buy


class PeekRequest(BaseModel):
    mint: str
    known_pool: str | None = None
    name: str | None = None
    symbol: str | None = None


class AlertSubRequest(BaseModel):
    mint: str
    threshold_pct: float | None = Field(
        None, gt=0, description="abs %% move; default engine threshold"
    )


class AlertUnsubRequest(BaseModel):
    mint: str


class AgentBuyRequest(BaseModel):
    mint: str
    sol: float = Field(..., gt=0)
    profit_pct: float = Field(..., gt=0, description="take-profit %%")
    loss_pct: float = Field(..., gt=0, description="stop-loss %% magnitude")
    name: str | None = None
    symbol: str | None = None
    pool: str | None = None
    price_sol: float | None = Field(None, gt=0)
    fdv_usd: float | None = Field(None, gt=0)
    sol_usd: float | None = Field(None, gt=0)


class AgentSellRequest(BaseModel):
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


class AgentTpslRequest(BaseModel):
    mint: str
    profit_pct: float | None = Field(None, gt=0)
    loss_pct: float | None = Field(None, gt=0)
    clear_profit: bool = False
    clear_loss: bool = False


class RejectRequest(BaseModel):
    mint: str
    reasoning: str = "agent reject"


def _rpc_ledger(request: Request) -> tuple[HeliusRpc, Path]:
    rpc: HeliusRpc = request.app.state.helius_rpc
    ledger: Path = request.app.state.settings.resolved_trading_ledger_db()
    if not rpc.enabled:
        raise HTTPException(status_code=503, detail="helius api key not configured")
    return rpc, ledger


def _alerts(request: Request) -> MoveAlertEngine:
    eng = getattr(request.app.state, "move_alert_engine", None)
    if eng is None:
        raise HTTPException(status_code=503, detail="move-alert engine not ready")
    return eng


def _tpsl(request: Request):
    eng = getattr(request.app.state, "tpsl_engine", None)
    if eng is None:
        raise HTTPException(status_code=503, detail="tpsl engine not ready")
    return eng


def create_agent_router() -> APIRouter:
    """agent-facing routes — stable contract for LangGraph tools."""
    router = APIRouter(prefix="/agent", tags=["agent"])

    @router.post("/peek")
    async def agent_peek(body: PeekRequest, request: Request) -> dict[str, Any]:
        """market cap off the live socket when this mint is in focus."""
        rpc, ledger = _rpc_ledger(request)
        alerts = getattr(request.app.state, "move_alert_engine", None)
        live = getattr(alerts, "live", None)
        mint = body.mint.strip()
        if live is not None and alerts is not None and mint in alerts._subs:
            tick = await live.wait_latest(mint)
            if tick is None:
                raise HTTPException(status_code=503, detail="live price not ready")
            result = await asyncio.to_thread(peek_from_tick, ledger, tick)
            return {"ok": True, **result.model_dump()}

        def _run():
            return peek_price(
                rpc,
                ledger,
                body.mint,
                known_pool=body.known_pool,
                name=body.name,
                symbol=body.symbol,
            )

        try:
            result = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result.model_dump()}

    @router.post("/alerts/subscribe")
    async def alerts_subscribe(
        body: AlertSubRequest, request: Request
    ) -> dict[str, Any]:
        eng = _alerts(request)
        try:
            return await eng.subscribe(
                body.mint, threshold_pct=body.threshold_pct
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/alerts/unsubscribe")
    async def alerts_unsubscribe(
        body: AlertUnsubRequest, request: Request
    ) -> dict[str, Any]:
        eng = _alerts(request)
        return await eng.unsubscribe(body.mint)

    @router.get("/alerts/pending")
    async def alerts_pending(
        request: Request,
        limit: int = Query(20, ge=1, le=50),
    ) -> dict[str, Any]:
        eng = _alerts(request)
        alerts = eng.drain(limit=limit)
        return {
            "ok": True,
            "count": len(alerts),
            "alerts": [a.model_dump() for a in alerts],
            "status": eng.status(),
        }

    @router.get("/alerts/status")
    async def alerts_status(request: Request) -> dict[str, Any]:
        eng = _alerts(request)
        return {"ok": True, **eng.status()}

    @router.get("/backlog/peek")
    async def agent_backlog_peek(
        request: Request,
        limit: int = Query(10, ge=1, le=50),
    ) -> dict[str, Any]:
        _, ledger = _rpc_ledger(request)
        return await asyncio.to_thread(peek_backlog, ledger, limit=limit)

    @router.post("/backlog/next")
    async def agent_backlog_next(request: Request) -> dict[str, Any]:
        _, ledger = _rpc_ledger(request)
        return await asyncio.to_thread(next_candidate, ledger)

    @router.get("/wallet")
    async def agent_wallet(request: Request) -> dict[str, Any]:
        rpc, ledger = _rpc_ledger(request)
        tpsl = getattr(request.app.state, "tpsl_engine", None)

        def _run() -> dict[str, Any]:
            cash = get_balance(ledger)
            marks = mark_all_positions(ledger, rpc)
            positions = []
            for m in marks:
                tpsl_d = tpsl.levels_dict(m.position.mint) if tpsl else None
                positions.append(
                    {
                        **(_pos_dict(m.position, tpsl=tpsl_d) or {}),
                        "price_sol": m.price_sol,
                        "fdv_usd": m.fdv_usd,
                        "value_sol": m.value_sol,
                        "pnl_sol": m.pnl_sol,
                        "pnl_pct": m.pnl_pct,
                    }
                )
            return {"ok": True, "cash_sol": cash, "positions": positions}

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/position/{mint}")
    async def agent_position(mint: str, request: Request) -> dict[str, Any]:
        _, ledger = _rpc_ledger(request)
        tpsl = getattr(request.app.state, "tpsl_engine", None)
        pos = await asyncio.to_thread(get_position, ledger, mint.strip())
        tpsl_d = tpsl.levels_dict(mint.strip()) if tpsl else None
        return {
            "ok": True,
            "position": _pos_dict(pos, tpsl=tpsl_d),
            "tpsl": tpsl_d,
            "last_hit": (tpsl_d or {}).get("last_hit"),
        }

    @router.post("/buy")
    async def agent_buy_route(
        body: AgentBuyRequest, request: Request
    ) -> dict[str, Any]:
        """buy + required TP/SL arm in one call."""
        rpc, ledger = _rpc_ledger(request)
        tpsl = _tpsl(request)
        mint = body.mint.strip()
        tick = _tick_from_body(body, mint)

        def _run():
            return agent_buy(
                ledger,
                rpc,
                mint,
                body.sol,
                profit_pct=body.profit_pct,
                loss_pct=body.loss_pct,
                tick=tick,
                name=body.name,
                symbol=body.symbol,
                known_pool=body.pool,
            )

        try:
            payload = await asyncio.to_thread(_run)
            await tpsl.set_levels(
                mint,
                profit_pct=body.profit_pct,
                loss_pct=body.loss_pct,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        fill = payload["fill"]
        out = _fill_dict(fill)
        levels = tpsl.levels_dict(mint)
        if out.get("position") is not None:
            out["position"]["profit_pct"] = levels.get("profit_pct")
            out["position"]["loss_pct"] = levels.get("loss_pct")
        return {"ok": True, "fill": out, "tpsl": levels}

    @router.post("/sell")
    async def agent_sell_route(
        body: AgentSellRequest, request: Request
    ) -> dict[str, Any]:
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
        tpsl = getattr(request.app.state, "tpsl_engine", None)
        alerts = getattr(request.app.state, "move_alert_engine", None)

        def _run():
            return agent_sell(
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

        if fill.position is None:
            if tpsl is not None:
                await tpsl.on_position_closed(mint)
            if alerts is not None:
                await alerts.unsubscribe(mint)
        return {"ok": True, "fill": _fill_dict(fill)}

    @router.post("/tpsl")
    async def agent_tpsl_route(
        body: AgentTpslRequest, request: Request
    ) -> dict[str, Any]:
        tpsl = _tpsl(request)
        mint = body.mint.strip()
        kw: dict[str, Any] = {}
        if body.clear_profit:
            kw["profit_pct"] = None
        elif "profit_pct" in body.model_fields_set:
            kw["profit_pct"] = body.profit_pct
        if body.clear_loss:
            kw["loss_pct"] = None
        elif "loss_pct" in body.model_fields_set:
            kw["loss_pct"] = body.loss_pct
        if not kw:
            raise HTTPException(
                status_code=400,
                detail="pass profit_pct / loss_pct or clear flags",
            )
        try:
            levels = await tpsl.set_levels(mint, **kw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **tpsl.levels_dict(mint), "active": levels.active}

    @router.post("/reject")
    async def agent_reject_route(
        body: RejectRequest, request: Request
    ) -> dict[str, Any]:
        _, ledger = _rpc_ledger(request)
        return await asyncio.to_thread(
            agent_reject, ledger, body.mint, reasoning=body.reasoning
        )

    @router.get("/events")
    async def agent_events(
        request: Request,
        after_id: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=300),
    ) -> dict[str, Any]:
        """live thinking lines for migrations --follow."""
        loop = getattr(request.app.state, "agent_loop", None)
        feed = getattr(loop, "feed", None) if loop is not None else None
        if feed is None:
            return {"ok": True, "running": False, "events": []}
        return {
            "ok": True,
            "running": bool(loop.running),
            "events": feed.since(after_id, limit),
        }

    @router.get("/loop")
    async def agent_loop_status(request: Request) -> dict[str, Any]:
        """phase, mint, last decision. running stays false until started."""
        loop = getattr(request.app.state, "agent_loop", None)
        if loop is None:
            return {"ok": True, "running": False, "phase": "idle"}
        return loop.status()

    @router.post("/loop/start")
    async def agent_loop_start(request: Request) -> dict[str, Any]:
        """start the background lock loop. needs ollama already up."""
        loop = getattr(request.app.state, "agent_loop", None)
        if loop is None:
            raise HTTPException(status_code=503, detail="agent loop not ready")
        return await loop.start()

    @router.post("/loop/stop")
    async def agent_loop_stop(request: Request) -> dict[str, Any]:
        """stop the lock loop. filters and TP/SL keep running."""
        loop = getattr(request.app.state, "agent_loop", None)
        if loop is None:
            raise HTTPException(status_code=503, detail="agent loop not ready")
        return await loop.stop()

    return router
