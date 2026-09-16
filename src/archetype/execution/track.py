"""
inline (single-line) live FDV / optional paper PnL on Helius vault WS ticks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.text import Text

from archetype.execution.wallet import Position
from archetype.filters.helius import HeliusRpc
from archetype.price.models import PriceTick
from archetype.price.resolve import ResolvedPool
from archetype.price.stream import aiter_price_ticks_ws


def format_position_header(pos: Position) -> str:
    """watch header: original bought + sold-so-far (omit sold if none)."""
    entry_fdv = (
        f"${pos.entry_fdv_usd:,.0f}" if pos.entry_fdv_usd is not None else "?"
    )
    bought = pos.bought_sol if pos.bought_sol > 0 else pos.cost_sol
    bits = [f"bought={bought:.4f} SOL"]
    if pos.sold_sol > 1e-9:
        bits.append(f"sold={pos.sold_sol:.4f} SOL")
    bits.append(f"avg entry FDV={entry_fdv}")
    bits.append(f"tokens left={pos.tokens:,.4f}")
    return "[dim]" + "  ".join(bits) + "[/dim]"


def format_watch_line(tick: PriceTick, pos: Position | None = None) -> Text:
    """one rich line — FDV always; PnL when we hold a paper position."""
    ts = datetime.now().strftime("%H:%M:%S")
    label = tick.symbol or tick.name or (pos.label if pos else tick.mint[:8])
    t = Text()
    t.append(f"{ts}  ", style="green")
    t.append(f"{label}  ", style="bold")
    t.append(f"FDV=${tick.fdv_usd:,.0f}  ", style="cyan bold")
    t.append(f"price={tick.price_sol:.10g} SOL  ")
    if pos is not None:
        value_sol = pos.tokens * tick.price_sol
        # unrealized on leftover bags (remaining cost basis)
        basis = pos.cost_sol
        pnl_sol = value_sol - basis
        pnl_pct = (pnl_sol / basis * 100.0) if basis > 0 else 0.0
        color = "green" if pnl_sol >= 0 else "red"
        sign = "+" if pnl_sol >= 0 else ""
        t.append(f"value={value_sol:.4f} SOL  ")
        t.append(
            f"PnL={sign}{pnl_sol:.4f} SOL ({sign}{pnl_pct:.2f}%)",
            style=color,
        )
    else:
        t.append(
            f"(${tick.price_usd:.8g})  "
            f"pool={tick.pool_sol:.2f} SOL / {tick.pool_base_ui:,.0f} tok"
        )
    return t


async def _watch_loop(
    rpc: HeliusRpc,
    mint: str,
    console: Console,
    pos: Position | None,
    *,
    resolved: ResolvedPool | None,
    ledger_path: Path | None,
) -> None:
    """ws loop — overwrite one Live line each tick."""
    first = True
    with Live(
        Text("connecting…", style="dim"),
        console=console,
        refresh_per_second=8,
        transient=False,
    ) as live:
        async for tick in aiter_price_ticks_ws(
            rpc,
            mint,
            resolved=resolved,
            known_pool=pos.pool if pos else None,
            name=pos.name if pos else None,
            symbol=pos.symbol if pos else None,
            ledger_path=ledger_path,
        ):
            if first:
                bits = [
                    f"[bold]watching[/bold] "
                    f"{tick.name or tick.symbol or mint[:8]}  "
                    f"mint={mint}  [ws]"
                ]
                if pos is not None:
                    bits.append(format_position_header(pos))
                else:
                    bits.append(
                        f"[dim]pool={tick.pool}  "
                        f"FDV supply={tick.fdv_supply:,.0f}  "
                        f"SOL=${tick.sol_usd:,.2f}[/dim]"
                    )
                live.console.print("\n".join(bits))
                first = False
            live.update(format_watch_line(tick, pos))


def run_inline_watch(
    rpc: HeliusRpc,
    mint: str,
    console: Console,
    *,
    position: Position | None = None,
    resolved: ResolvedPool | None = None,
    ledger_path: Path | None = None,
) -> None:
    """
    block and update FDV (and PnL if position) in-place until ctrl+c.

    does not open/close trades — just stops watching.
    """
    hint = "live position PnL" if position else "live FDV"
    console.print(f"[dim]{hint} (ctrl+c to stop)…[/dim]")
    try:
        asyncio.run(
            _watch_loop(
                rpc,
                mint,
                console,
                position,
                resolved=resolved,
                ledger_path=ledger_path,
            )
        )
    except KeyboardInterrupt:
        msg = (
            "stopped watching (position still open)"
            if position
            else "stopped watching"
        )
        console.print(f"\n[dim]{msg}[/dim]")


def run_inline_pnl(rpc: HeliusRpc, pos: Position, console: Console) -> None:
    """inline live PnL for an open position."""
    run_inline_watch(rpc, pos.mint, console, position=pos)
