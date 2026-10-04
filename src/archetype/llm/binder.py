"""
in-process commands the loop calls after the model picks an action.

the model does not see these. it replies with json, then the loop
calls buy / reject / sell / set_tpsl itself.

if a focus mint is set, a call on any other mint is refused.
peek_backlog is the exception — other-coin context, no second trade.
drain_for_step() runs before each think, so alerts are not stuck.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from archetype.execution.fills import mark_all_positions
from archetype.execution.tpsl import TpslEngine
from archetype.execution.wallet import get_balance, get_position
from archetype.filters.helius import HeliusRpc
from archetype.tools.alerts import MoveAlertEngine
from archetype.tools.backlog_tools import next_candidate, peek_backlog
from archetype.tools.peek import peek_price
from archetype.tools.trade_tools import agent_buy, agent_reject, agent_sell

log = logging.getLogger("archetype.binder")

AgentMode = Literal["free", "locked"]


def _deny(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


class AgentToolkit:
    """commands the loop calls by name. the model never sees this list."""

    def __init__(
        self,
        *,
        rpc: HeliusRpc,
        ledger_path: Path,
        tpsl: TpslEngine | None = None,
        alerts: MoveAlertEngine | None = None,
        mode: AgentMode = "free",
        focus_mint: str | None = None,
    ) -> None:
        self.rpc = rpc
        self.ledger_path = ledger_path
        self.tpsl = tpsl
        self.alerts = alerts
        self.mode: AgentMode = mode
        self.focus_mint = (focus_mint or "").strip() or None
        self._handlers = self._build_handlers()

    def set_focus(self, mode: AgentMode, focus_mint: str | None = None) -> None:
        """searching (free) or locked to one mint."""
        self.mode = mode
        self.focus_mint = (focus_mint or "").strip() or None

    async def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """run one command by name. mode and focus still apply."""
        handler = self._handlers.get(name)
        if handler is None:
            return _deny(f"unknown tool {name}")
        try:
            result = await handler(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s failed: %s", name, exc)
            return _deny(str(exc))
        if not isinstance(result, dict):
            return {"ok": True, "result": result}
        return result

    def drain_for_step(self, *, limit: int = 20) -> dict[str, Any]:
        """
        pop pending move alerts before the model runs.

        same queue as the drain_move_alerts tool.
        """
        if self.alerts is None:
            return {"ok": True, "count": 0, "alerts": []}
        alerts = self.alerts.drain(limit=limit)
        return {
            "ok": True,
            "count": len(alerts),
            "alerts": [a.model_dump() for a in alerts],
        }

    def _need(self, when: str) -> dict[str, Any] | None:
        if when == "always" or when == self.mode:
            return None
        return _deny(f"not available while {self.mode}")

    def _resolve_mint(self, mint: str | None) -> tuple[str | None, dict[str, Any] | None]:
        """focus mint wins. a different mint is a refuse, not a trade."""
        asked = (mint or "").strip()
        focus = self.focus_mint or ""
        if focus:
            if asked and asked != focus:
                return None, _deny(
                    f"focused on {focus} — cannot act on another mint"
                )
            return focus, None
        if not asked:
            return None, _deny("mint required")
        return asked, None

    def _build_handlers(self) -> dict[str, Any]:
        kit = self

        async def peek_price_tool(mint: str = "") -> dict[str, Any]:
            denied = kit._need("always")
            if denied:
                return denied
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            result = await asyncio.to_thread(
                peek_price, kit.rpc, kit.ledger_path, use
            )
            return {"ok": True, **result.model_dump()}

        async def subscribe_move_alerts(
            mint: str = "", threshold_pct: float | None = None
        ) -> dict[str, Any]:
            denied = kit._need("locked")
            if denied:
                return denied
            if kit.alerts is None:
                return _deny("move-alert engine not ready")
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            return await kit.alerts.subscribe(use, threshold_pct=threshold_pct)

        async def unsubscribe_move_alerts(mint: str = "") -> dict[str, Any]:
            denied = kit._need("locked")
            if denied:
                return denied
            if kit.alerts is None:
                return _deny("move-alert engine not ready")
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            return await kit.alerts.unsubscribe(use)

        async def drain_move_alerts() -> dict[str, Any]:
            denied = kit._need("locked")
            if denied:
                return denied
            return kit.drain_for_step()

        async def peek_backlog_tool(limit: int = 10) -> dict[str, Any]:
            # other coins are context only — no mint lock here
            limit = min(50, max(1, int(limit)))
            return await asyncio.to_thread(
                peek_backlog, kit.ledger_path, limit=limit
            )

        async def next_candidate_tool() -> dict[str, Any]:
            denied = kit._need("free")
            if denied:
                return denied
            if kit.focus_mint:
                return _deny(
                    "already focused — finish this coin before popping another"
                )
            return await asyncio.to_thread(next_candidate, kit.ledger_path)

        async def get_wallet() -> dict[str, Any]:
            # lazy — api package imports the daemon app
            from archetype.api.paper import _pos_dict

            def _run() -> dict[str, Any]:
                cash = get_balance(kit.ledger_path)
                marks = mark_all_positions(kit.ledger_path, kit.rpc)
                positions = []
                for m in marks:
                    tpsl_d = (
                        kit.tpsl.levels_dict(m.position.mint) if kit.tpsl else None
                    )
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

            return await asyncio.to_thread(_run)

        async def get_position_tool(mint: str = "") -> dict[str, Any]:
            from archetype.api.paper import _pos_dict

            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            pos = await asyncio.to_thread(get_position, kit.ledger_path, use)
            tpsl_d = kit.tpsl.levels_dict(use) if kit.tpsl else None
            return {
                "ok": True,
                "position": _pos_dict(pos, tpsl=tpsl_d),
                "tpsl": tpsl_d,
            }

        async def buy(
            mint: str = "",
            sol: float = 0,
            profit_pct: float = 0,
            loss_pct: float = 0,
        ) -> dict[str, Any]:
            denied = kit._need("free")
            if denied:
                return denied
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            if sol <= 0 or profit_pct <= 0 or loss_pct <= 0:
                return _deny("buy needs sol, profit_pct, and loss_pct > 0")
            from archetype.api.paper import _fill_dict

            raw = await asyncio.to_thread(
                agent_buy,
                kit.ledger_path,
                kit.rpc,
                use,
                sol,
                profit_pct=profit_pct,
                loss_pct=loss_pct,
            )
            if kit.tpsl is not None:
                await kit.tpsl.set_levels(
                    use, profit_pct=profit_pct, loss_pct=loss_pct
                )
            return {
                "ok": True,
                "fill": _fill_dict(raw["fill"]),
                "profit_pct": profit_pct,
                "loss_pct": loss_pct,
            }

        async def sell(
            mint: str = "",
            percent: float | None = None,
            sol_amount: float | None = None,
            initials: bool = False,
        ) -> dict[str, Any]:
            denied = kit._need("locked")
            if denied:
                return denied
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            modes = sum(
                [percent is not None, sol_amount is not None, bool(initials)]
            )
            if modes > 1:
                return _deny("use only one of percent / sol_amount / initials")
            from archetype.api.paper import _fill_dict

            fill = await asyncio.to_thread(
                agent_sell,
                kit.ledger_path,
                kit.rpc,
                use,
                percent=percent,
                sol_amount=sol_amount,
                initials=initials,
            )
            if fill.position is None:
                if kit.tpsl is not None:
                    await kit.tpsl.on_position_closed(use)
                if kit.alerts is not None:
                    await kit.alerts.unsubscribe(use)
            return {"ok": True, "fill": _fill_dict(fill)}

        async def set_tpsl(
            mint: str = "",
            profit_pct: float | None = None,
            loss_pct: float | None = None,
            clear_profit: bool = False,
            clear_loss: bool = False,
        ) -> dict[str, Any]:
            denied = kit._need("locked")
            if denied:
                return denied
            if kit.tpsl is None:
                return _deny("tpsl engine not ready")
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            kw: dict[str, Any] = {}
            if clear_profit:
                kw["profit_pct"] = None
            elif profit_pct is not None:
                kw["profit_pct"] = profit_pct
            if clear_loss:
                kw["loss_pct"] = None
            elif loss_pct is not None:
                kw["loss_pct"] = loss_pct
            if not kw:
                return _deny("pass profit_pct / loss_pct or clear flags")
            levels = await kit.tpsl.set_levels(use, **kw)
            return {"ok": True, **kit.tpsl.levels_dict(use), "active": levels.active}

        async def reject(mint: str = "", reasoning: str = "") -> dict[str, Any]:
            denied = kit._need("free")
            if denied:
                return denied
            use, err = kit._resolve_mint(mint)
            if err or use is None:
                return err or _deny("mint required")
            text = reasoning.strip() or "agent reject"
            return await asyncio.to_thread(
                agent_reject, kit.ledger_path, use, reasoning=text
            )

        return {
            "peek_price": peek_price_tool,
            "subscribe_move_alerts": subscribe_move_alerts,
            "unsubscribe_move_alerts": unsubscribe_move_alerts,
            "drain_move_alerts": drain_move_alerts,
            "peek_backlog": peek_backlog_tool,
            "next_candidate": next_candidate_tool,
            "get_wallet": get_wallet,
            "get_position": get_position_tool,
            "buy": buy,
            "sell": sell,
            "set_tpsl": set_tpsl,
            "reject": reject,
        }

