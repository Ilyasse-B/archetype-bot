"""
inline (single-line) live FDV / optional paper PnL on Helius vault WS ticks.
"""

from __future__ import annotations

import asyncio
import time
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


def round_mark(pos: Position, price_sol: float) -> tuple[float, float, float]:
    """
    aggregated round mark-to-market.

    pnl = (sold_proceeds + remaining_value) - bought
    """
    bought = pos.bought_sol if pos.bought_sol > 0 else pos.cost_sol
    value_sol = pos.tokens * price_sol
    total = pos.sold_sol + value_sol
    pnl_sol = total - bought
    pnl_pct = (pnl_sol / bought * 100.0) if bought > 0 else 0.0
    return value_sol, pnl_sol, pnl_pct


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
    tpsl = format_tpsl_bits(pos)
    if tpsl:
        bits.append(tpsl)
    return "[dim]" + "  ".join(bits) + "[/dim]"


def format_tpsl_bits(pos: Position | None) -> str:
    """TP=+50% SL=-20% fragment (empty if unset)."""
    if pos is None:
        return ""
    parts: list[str] = []
    if pos.profit_pct is not None:
        parts.append(f"TP=+{pos.profit_pct:g}%")
    if pos.loss_pct is not None:
        parts.append(f"SL=-{pos.loss_pct:g}%")
    return " ".join(parts)


def format_watch_line(tick: PriceTick, pos: Position | None = None) -> Text:
    """one rich line — FDV always; aggregated round PnL when holding."""
    ts = datetime.now().strftime("%H:%M:%S")
    label = tick.symbol or tick.name or (pos.label if pos else tick.mint[:8])
    t = Text()
    t.append(f"{ts}  ", style="green")
    t.append(f"{label}  ", style="bold")
    t.append(f"FDV=${tick.fdv_usd:,.0f}  ", style="cyan bold")
    t.append(f"price={tick.price_sol:.10g} SOL  ")
    if pos is not None:
        value_sol, pnl_sol, pnl_pct = round_mark(pos, tick.price_sol)
        color = "green" if pnl_sol >= 0 else "red"
        sign = "+" if pnl_sol >= 0 else ""
        t.append(f"left={value_sol:.4f} SOL  ")
        t.append(
            f"PnL={sign}{pnl_sol:.4f} SOL ({sign}{pnl_pct:.2f}%)",
            style=color,
        )
        tpsl = format_tpsl_bits(pos)
        if tpsl:
            t.append("  ")
            t.append(tpsl, style="yellow")
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
    daemon_base_url: str | None = None,
) -> None:
    """ws loop — overwrite one Live line each tick."""
    import httpx

    first = True
    last_poll = 0.0
    seen_hit_seq = 0
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

            # ~1s poll: refresh TP/SL levels + detect daemon auto-exit
            now = time.monotonic()
            if daemon_base_url and now - last_poll >= 1.0:
                last_poll = now
                try:
                    url = f"{daemon_base_url}/paper/position/{mint}"

                    def _poll() -> httpx.Response:
                        return httpx.get(url, timeout=5.0)

                    resp = await asyncio.to_thread(_poll)
                    if resp.status_code == 200:
                        data = resp.json() or {}
                        hit = data.get("last_hit")
                        if hit and int(hit.get("seq") or 0) > seen_hit_seq:
                            seen_hit_seq = int(hit.get("seq") or 0)
                            reason = str(hit.get("reason") or "?").upper()
                            tag = "green" if reason == "TP" else "red"
                            pct = float(hit.get("pnl_pct") or 0)
                            pnl = float(hit.get("pnl_sol") or 0)
                            sign = "+" if pct >= 0 else ""
                            live.console.print(
                                f"[{tag} bold]{reason} HIT[/]  "
                                f"sold={float(hit.get('sol') or 0):.4f} SOL  "
                                f"PnL={sign}{pnl:.4f} SOL ({sign}{pct:.2f}%)"
                            )
                            pos = None
                        else:
                            pdata = data.get("position")
                            if pdata:
                                from archetype.execution.wallet import Position

                                pos = Position(
                                    id=int(pdata.get("id") or 0),
                                    mint=str(pdata["mint"]),
                                    name=pdata.get("name"),
                                    symbol=pdata.get("symbol"),
                                    tokens=float(pdata["tokens"]),
                                    cost_sol=float(pdata["cost_sol"]),
                                    entry_price_sol=float(
                                        pdata["entry_price_sol"]
                                    ),
                                    entry_fdv_usd=(
                                        float(pdata["entry_fdv_usd"])
                                        if pdata.get("entry_fdv_usd") is not None
                                        else None
                                    ),
                                    opened_at=str(pdata.get("opened_at") or ""),
                                    initial_cost_sol=float(
                                        pdata.get("initial_cost_sol") or 0
                                    ),
                                    pool=pdata.get("pool"),
                                    bought_sol=float(
                                        pdata.get("bought_sol")
                                        or pdata.get("cost_sol")
                                        or 0
                                    ),
                                    sold_sol=float(pdata.get("sold_sol") or 0),
                                    round_id=(
                                        int(pdata["round_id"])
                                        if pdata.get("round_id") is not None
                                        else None
                                    ),
                                    profit_pct=(
                                        float(pdata["profit_pct"])
                                        if pdata.get("profit_pct") is not None
                                        else None
                                    ),
                                    loss_pct=(
                                        float(pdata["loss_pct"])
                                        if pdata.get("loss_pct") is not None
                                        else None
                                    ),
                                )
                            elif pos is not None:
                                pos = None
                except Exception:  # noqa: BLE001
                    pass

            live.update(format_watch_line(tick, pos))


def run_inline_watch(
    rpc: HeliusRpc,
    mint: str,
    console: Console,
    *,
    position: Position | None = None,
    resolved: ResolvedPool | None = None,
    ledger_path: Path | None = None,
    daemon_base_url: str | None = None,
) -> None:
    """
    block and update FDV (and PnL if position) in-place until ctrl+c.

    does not open/close trades — just stops watching.
    pass daemon_base_url to show TP/SL + auto-exit hits from the daemon.
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
                daemon_base_url=daemon_base_url,
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
