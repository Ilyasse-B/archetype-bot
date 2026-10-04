"""print agent feed lines in the migrations follow view."""

from __future__ import annotations

from typing import Any

from rich.console import Console


def print_agent_event(console: Console, ev: dict[str, Any]) -> None:
    """one agent line. kind decides the colour and the words."""
    ts = str(ev.get("ts") or "")
    kind = str(ev.get("kind") or "")
    name = str(ev.get("name") or ev.get("symbol") or "")
    mint = str(ev.get("mint") or "")
    label = name or (mint[:8] + "…") if mint else "coin"
    head = f"[green]{ts}[/green] [cyan]AGENT[/cyan]"

    if kind == "focus":
        extra = _mcap_fees(ev)
        console.print(f"{head} looking at [bold]{label}[/bold]{extra} mint={mint}")
        return
    if kind == "thinking":
        bits = ""
        price = ev.get("price_sol")
        fdv = ev.get("fdv_usd")
        if isinstance(price, (int, float)):
            bits += f" price={price:.8g} SOL"
        if isinstance(fdv, (int, float)):
            bits += f" fdv=${fdv:,.0f}"
        console.print(f"{head} thinking about [bold]{label}[/bold]{bits}…")
        return
    if kind == "decision":
        action = str(ev.get("action") or "")
        narrative = str(ev.get("narrative") or "").strip()
        reasoning = str(ev.get("reasoning") or "").strip()
        colour = {
            "reject": "red",
            "buy": "green",
            "sell": "yellow",
            "hold": "dim",
            "wait": "dim",
        }.get(action, "white")
        console.print(f"{head} [{colour}]{action}[/{colour}] {label}")
        if narrative:
            console.print(f"         {narrative}")
        if reasoning and reasoning != narrative:
            console.print(f"         [dim]reason:[/dim] {reasoning}")
        return
    if kind == "buy":
        console.print(
            f"{head} [green]bought[/green] {label} "
            f"{_num(ev.get('sol'))} SOL  tp={_num(ev.get('profit_pct'))}%  "
            f"sl={_num(ev.get('loss_pct'))}%"
        )
        return
    if kind == "tpsl":
        reason = str(ev.get("reason") or "?").upper()
        colour = "green" if reason == "TP" else "red"
        pnl = ev.get("pnl_sol")
        pct = ev.get("pnl_pct")
        console.print(
            f"{head} [{colour}]{reason} HIT[/{colour}] {label}  "
            f"sold={_num(ev.get('sol'))} SOL  pnl={pnl} ({pct}%)"
        )
        return
    if kind == "sell":
        flat = ev.get("flat")
        tail = "flat" if flat else "still holding"
        console.print(f"{head} [yellow]sold[/yellow] {label} ({tail})")
        return
    if kind == "revise_tpsl":
        reason = str(ev.get("reasoning") or ev.get("narrative") or "").strip()
        tp, sl = ev.get("profit_pct"), ev.get("loss_pct")
        levels = ""
        if tp is not None or sl is not None:
            levels = f"  tp={_num(tp) if tp is not None else '—'}%  sl={_num(sl) if sl is not None else '—'}%"
        console.print(
            f"{head} updated tp/sl on {label}{levels}" + (f" — {reason}" if reason else "")
        )
        return
    if kind == "reject":
        console.print(
            f"{head} [red]rejected[/red] {label} — {ev.get('reasoning') or ''}"
        )
        return
    if kind == "thesis":
        text = str(ev.get("thesis") or "").strip()
        pnl = ev.get("pnl_sol")
        held = ev.get("held_sec")
        console.print(
            f"{head} [magenta]thesis[/magenta] {label}  pnl={pnl}  held={held}s"
        )
        if text:
            console.print(f"         {text}")
        return
    if kind == "error":
        console.print(f"{head} [red]error[/red] {label} — {ev.get('error') or ''}")
        return
    if kind == "hold":
        reason = str(ev.get("reasoning") or "").strip()
        waiting = str(ev.get("phase") or "") != "locked"
        word = "waiting on" if waiting else "holding"
        tp, sl = ev.get("profit_pct"), ev.get("loss_pct")
        levels = ""
        if not waiting and (tp is not None or sl is not None):
            levels = f"  tp={_num(tp) if tp is not None else '—'}%  sl={_num(sl) if sl is not None else '—'}%"
        console.print(f"{head} {word} {label}{levels}" + (f" — {reason}" if reason else ""))
        return
    console.print(f"{head} {kind} {label}")


def _mcap_fees(ev: dict[str, Any]) -> str:
    bits = ""
    mcap = ev.get("mcap_usd")
    if isinstance(mcap, (int, float)):
        bits += f" mcap=${mcap:,.0f}"
    fees = ev.get("fees_sol")
    if isinstance(fees, (int, float)):
        if ev.get("fees_check_needed"):
            bits += f" [yellow]fees~{fees:.2f}SOL CHECK[/yellow]"
        else:
            bits += f" fees~{fees:.2f}SOL"
    sol = ev.get("buy_sol")
    if isinstance(sol, (int, float)):
        bits += f" buy={_num(sol)}SOL"
    return bits


def _num(val: Any) -> str:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return str(val if val is not None else "")
    if float(val).is_integer():
        return str(int(val))
    return f"{float(val):.4g}"
