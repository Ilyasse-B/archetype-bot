"""
interactive filtered trade stream — human stands in for the agent.

polls migration_backlog via sqlite (daemon already writes there; no extra
daemon work). for each coin: watch | reject; during watch: buy / sell /
reject / leave.
"""

from __future__ import annotations

import asyncio
import logging
import select
import sys
import termios
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

import httpx
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from archetype.agent.store import ensure_agent_tables, record_reject, record_review
from archetype.backlog import BacklogItem, consume_id, peek_fifo, pending_count
from archetype.config import Settings
from archetype.execution.track import (
    format_position_header,
    format_watch_line,
)
from archetype.execution.wallet import Position
from archetype.filters.helius import HeliusRpc
from archetype.price.stream import aiter_price_ticks_ws

log = logging.getLogger("archetype.agent.trade_stream")

SessionAction = Literal["leave", "reject", "quit"]


@dataclass
class WatchSessionResult:
    """how an interactive watch ended."""

    action: SessionAction
    bought: bool = False


def _position_from_api(data: dict | None) -> Position | None:
    """rebuild Position from daemon JSON."""
    if not data:
        return None
    return Position(
        id=int(data.get("id") or 0),
        mint=str(data["mint"]),
        name=data.get("name"),
        symbol=data.get("symbol"),
        tokens=float(data["tokens"]),
        cost_sol=float(data["cost_sol"]),
        entry_price_sol=float(data["entry_price_sol"]),
        entry_fdv_usd=(
            float(data["entry_fdv_usd"])
            if data.get("entry_fdv_usd") is not None
            else None
        ),
        opened_at=str(data.get("opened_at") or ""),
        initial_cost_sol=float(data.get("initial_cost_sol") or 0),
        pool=data.get("pool"),
        bought_sol=float(data.get("bought_sol") or data.get("cost_sol") or 0),
        sold_sol=float(data.get("sold_sol") or 0),
        round_id=(
            int(data["round_id"]) if data.get("round_id") is not None else None
        ),
        profit_pct=(
            float(data["profit_pct"])
            if data.get("profit_pct") is not None
            else None
        ),
        loss_pct=(
            float(data["loss_pct"]) if data.get("loss_pct") is not None else None
        ),
    )


def _coin_label(item: BacklogItem) -> str:
    info = item.coin_info or {}
    name = info.get("name") or ""
    symbol = info.get("symbol") or ""
    if name and symbol:
        return f"{name} ({symbol})"
    return name or symbol or item.mint[:8]


def _print_candidate(console: Console, item: BacklogItem, pending: int) -> None:
    """show the next backlog coin before watch/reject."""
    info = item.coin_info or {}
    mcap = info.get("market_cap_usd")
    fees = info.get("estimated_global_fees_sol")
    warn_bar = info.get("fees_warn_below_sol")
    warn_bar_f = float(warn_bar) if isinstance(warn_bar, (int, float)) else 2.25
    # prefer explicit flag from filter; else infer for older backlog rows
    fees_check = bool(info.get("fees_check_needed"))
    if not fees_check and isinstance(fees, (int, float)):
        fees_check = fees < warn_bar_f
    socials = ",".join(info.get("socials_present") or []) or "—"
    bits = [
        f"[bold]#{item.id}[/bold] {_coin_label(item)}",
        f"mint={item.mint}",
        f"socials={socials}",
    ]
    if isinstance(mcap, (int, float)):
        bits.append(f"mcap=${mcap:,.0f}")
    if isinstance(fees, (int, float)):
        if fees_check:
            bits.append(f"[yellow bold]fees~{fees:.2f} SOL · CHECK[/yellow bold]")
        else:
            bits.append(f"fees~{fees:.2f} SOL")
    elif fees_check:
        bits.append("[yellow bold]fees=? · CHECK[/yellow bold]")
    console.print()
    console.rule(f"[cyan]candidate[/cyan]  backlog pending={pending}")
    console.print("  ".join(bits))
    if fees_check:
        console.print(
            f"[yellow]⚠ fees estimate under {warn_bar_f:.2f} SOL "
            f"(vault proxy can under-read vs Axiom) — verify before buying[/yellow]"
        )
    if info.get("twitter"):
        console.print(f"[dim]twitter={info['twitter']}[/dim]")
    if info.get("telegram"):
        console.print(f"[dim]telegram={info['telegram']}[/dim]")
    if info.get("website"):
        console.print(f"[dim]website={info['website']}[/dim]")
    console.print(
        "[bold]watch[/bold] / [bold]reject[/bold]  "
        "[dim](or quit to leave the stream)[/dim]"
    )


def _read_line(prompt: str = "> ") -> str:
    """blocking stdin line (stripped, lowercased for verbs)."""
    try:
        return input(prompt).strip()
    except EOFError:
        return "quit"


def _parse_decision(raw: str) -> str | None:
    """map first prompt to watch|reject|quit, else None."""
    s = raw.strip().lower()
    if not s:
        return None
    if s in ("w", "watch"):
        return "watch"
    if s in ("r", "reject"):
        return "reject"
    if s in ("q", "quit", "exit"):
        return "quit"
    return None


def _parse_watch_cmd(raw: str) -> tuple[str, list[str]] | None:
    """
    parse in-watch commands.

    returns (verb, args) or None if empty.
    """
    s = raw.strip()
    if not s:
        return None
    parts = s.split()
    verb = parts[0].lower()
    args = parts[1:]
    # "set profit 50" / "set loss 20" / "set profit clear"
    if verb == "set" and args:
        side = args[0].lower()
        if side in ("profit", "tp"):
            return "set_profit", args[1:]
        if side in ("loss", "sl", "stop"):
            return "set_loss", args[1:]
    # aliases
    if verb in ("w",):
        verb = "watch"  # noop if already watching
    if verb in ("leave", "exit", "stop", "done", "back"):
        verb = "leave"
    if verb in ("q", "quit"):
        verb = "quit"
    if verb in ("r",):
        verb = "reject"
    if verb in ("b",):
        verb = "buy"
    if verb in ("s",):
        verb = "sell"
    return verb, args


def _post_paper(
    settings: Settings, path: str, body: dict, console: Console
) -> dict | None:
    """POST /paper/*; print error and return None on failure."""
    url = f"{settings.base_url}{path}"
    try:
        resp = httpx.post(url, json=body, timeout=90.0)
    except httpx.ConnectError:
        console.print(
            f"[red]daemon not reachable at {settings.base_url}[/red] "
            "(buy/sell need the daemon)"
        )
        return None
    except httpx.HTTPError as exc:
        console.print(f"[red]request failed:[/red] {exc}")
        return None
    if resp.status_code != 200:
        detail = ""
        try:
            detail = str(resp.json().get("detail") or resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        console.print(f"[red]failed:[/red] {detail}")
        return None
    return resp.json() or {}


def _tick_body(tick, *, name: str | None = None, symbol: str | None = None) -> dict:
    """snapshot fields so daemon fills at the live watch price."""
    if tick is None:
        out: dict = {}
        if name:
            out["name"] = name
        if symbol:
            out["symbol"] = symbol
        return out
    return {
        "name": tick.name or name,
        "symbol": tick.symbol or symbol,
        "pool": tick.pool or None,
        "price_sol": float(tick.price_sol),
        "fdv_usd": float(tick.fdv_usd),
        "sol_usd": float(tick.sol_usd),
    }


def _apply_tpsl_to_pos(pos, tpsl: dict | None):
    """overlay profit/loss % onto a Position (mutates)."""
    if pos is None or not tpsl:
        return pos
    if "profit_pct" in tpsl:
        pos.profit_pct = (
            float(tpsl["profit_pct"])
            if tpsl.get("profit_pct") is not None
            else None
        )
    if "loss_pct" in tpsl:
        pos.loss_pct = (
            float(tpsl["loss_pct"]) if tpsl.get("loss_pct") is not None else None
        )
    return pos


def _fetch_position_bundle(
    settings: Settings, mint: str
) -> tuple[Position | None, dict | None, dict | None]:
    """GET position + tpsl + last_hit in one call."""
    try:
        resp = httpx.get(
            f"{settings.base_url}/paper/position/{mint}", timeout=10.0
        )
        if resp.status_code != 200:
            return None, None, None
        data = resp.json() or {}
        pos = _position_from_api(data.get("position"))
        tpsl = data.get("tpsl")
        _apply_tpsl_to_pos(pos, tpsl)
        return pos, tpsl, data.get("last_hit")
    except httpx.HTTPError:
        return None, None, None


def _fetch_position(settings: Settings, mint: str) -> Position | None:
    """GET open paper position if any."""
    pos, _tpsl, _hit = _fetch_position_bundle(settings, mint)
    return pos


def _format_tpsl_hit(hit: dict) -> str:
    """rich markup for an auto-exit event."""
    reason = str(hit.get("reason") or "?").upper()
    tag = "green" if reason == "TP" else "red"
    pnl = float(hit.get("pnl_sol") or 0)
    pct = float(hit.get("pnl_pct") or 0)
    sign = "+" if pct >= 0 else ""
    label = hit.get("symbol") or hit.get("name") or hit.get("mint", "")[:8]
    return (
        f"[{tag} bold]{reason} HIT[/] {label}  "
        f"sold={float(hit.get('sol') or 0):.4f} SOL  "
        f"PnL={sign}{pnl:.4f} SOL ({sign}{pct:.2f}%)  "
        f"FDV=${float(hit.get('fdv_usd') or 0):,.0f}"
    )


@contextmanager
def _cbreak_stdin() -> Iterator[None]:
    """
    char-at-a-time stdin (no line buffering / no OS echo).

    we paint the typed buffer ourselves inside Live so price redraws
    dont wipe what the user is typing.
    """
    fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        yield
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _watch_panel(
    price_line: Text | None,
    input_buf: str,
) -> RenderableType:
    """price on top, visible `> …` prompt underneath."""
    price = price_line or Text("connecting…", style="dim")
    prompt = Text()
    prompt.append("> ", style="bold cyan")
    prompt.append(input_buf)
    prompt.append("█", style="dim")  # fake cursor so u see where u are
    return Group(price, prompt)


async def _interactive_watch(
    rpc: HeliusRpc,
    mint: str,
    console: Console,
    settings: Settings,
    *,
    ledger_path: Path,
    name: str | None = None,
    symbol: str | None = None,
    known_pool: str | None = None,
) -> WatchSessionResult:
    """
    live FDV line + stdin commands (buy/sell/reject/leave).

    ticks and keystrokes race; typed text is drawn inside Live so it
    stays visible while the price line refreshes.
    """
    pos = _fetch_position(settings, mint)
    bought = pos is not None and pos.tokens > 0

    help_bits = [
        "buy <sol>",
        "set profit <pct>|clear",
        "set loss <pct>|clear",
        "reject",
        "leave/exit/stop",
    ]
    if bought:
        help_bits.insert(1, "sell [pct|init|<n> sol]")
    console.print(f"[dim]watching — cmds: {' | '.join(help_bits)}[/dim]")
    if pos is not None:
        console.print(format_position_header(pos))

    # key events: ("eof"|"key", payload)
    key_q: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    stop_stdin = asyncio.Event()
    input_buf = ""
    price_line: Text | None = None
    last_tick = None  # PriceTick | None — for redraw after buy/sell
    last_tpsl_poll = 0.0
    seen_hit_seq = 0

    async def _stdin_reader() -> None:
        # cbreak: read 1 char, we echo via Live panel
        while not stop_stdin.is_set():
            ready = await loop.run_in_executor(
                None,
                lambda: select.select([sys.stdin], [], [], 0.25)[0],
            )
            if stop_stdin.is_set():
                return
            if not ready:
                continue
            ch = await loop.run_in_executor(None, sys.stdin.read, 1)
            if ch == "":
                await key_q.put(("eof", ""))
                return
            await key_q.put(("key", ch))

    reader_task = asyncio.create_task(_stdin_reader(), name="trade-stdin")
    tick_iter = aiter_price_ticks_ws(
        rpc,
        mint,
        known_pool=known_pool or (pos.pool if pos else None),
        name=name or (pos.name if pos else None),
        symbol=symbol or (pos.symbol if pos else None),
        ledger_path=ledger_path,
    ).__aiter__()

    next_tick = asyncio.create_task(tick_iter.__anext__(), name="next-tick")
    next_key = asyncio.create_task(key_q.get(), name="next-key")
    first = True
    result = WatchSessionResult(action="leave", bought=bought)

    def _refresh(live: Live) -> None:
        live.update(_watch_panel(price_line, input_buf))

    try:
        with _cbreak_stdin():
            with Live(
                _watch_panel(None, ""),
                console=console,
                refresh_per_second=12,
                transient=False,
            ) as live:
                while True:
                    done, _pending = await asyncio.wait(
                        {next_tick, next_key},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if next_tick in done:
                        try:
                            tick = next_tick.result()
                        except StopAsyncIteration:
                            console.print("[red]price stream ended[/red]")
                            result.action = "leave"
                            break
                        except Exception as exc:  # noqa: BLE001
                            console.print(f"[red]price error:[/red] {exc}")
                            result.action = "leave"
                            break

                        if first:
                            live.console.print(
                                f"[bold]watching[/bold] "
                                f"{tick.name or tick.symbol or mint[:8]}  "
                                f"mint={mint}  [ws]"
                            )
                            first = False
                        last_tick = tick

                        # poll daemon ~1s for TP/SL hit (daemon owns the exit)
                        now = time.monotonic()
                        if now - last_tpsl_poll >= 1.0:
                            last_tpsl_poll = now
                            new_pos, _tpsl, hit = await asyncio.to_thread(
                                _fetch_position_bundle, settings, mint
                            )
                            if (
                                hit
                                and int(hit.get("seq") or 0) > seen_hit_seq
                            ):
                                seen_hit_seq = int(hit.get("seq") or 0)
                                live.console.print(_format_tpsl_hit(hit))
                                pos = None
                                bought = True  # still counts as traded
                                result.bought = True
                                if last_tick is not None:
                                    price_line = format_watch_line(
                                        last_tick, None
                                    )
                                _refresh(live)
                                next_tick = asyncio.create_task(
                                    tick_iter.__anext__(), name="next-tick"
                                )
                                continue
                            if new_pos is not None:
                                pos = new_pos
                            elif pos is not None:
                                # closed manually elsewhere
                                pos = None

                        price_line = format_watch_line(tick, pos)
                        _refresh(live)
                        next_tick = asyncio.create_task(
                            tick_iter.__anext__(), name="next-tick"
                        )

                    if next_key in done:
                        kind, payload = next_key.result()
                        next_key = asyncio.create_task(
                            key_q.get(), name="next-key"
                        )

                        if kind == "eof":
                            result.action = "quit"
                            result.bought = bought
                            break

                        ch = payload
                        # enter → submit command
                        if ch in ("\n", "\r"):
                            raw = input_buf
                            input_buf = ""
                            _refresh(live)
                            parsed = _parse_watch_cmd(raw)
                            if parsed is None:
                                continue
                            verb, args = parsed

                            if verb in ("leave", "quit"):
                                result.action = (
                                    "quit" if verb == "quit" else "leave"
                                )
                                result.bought = bought
                                break

                            if verb == "reject":
                                result.action = "reject"
                                result.bought = bought
                                break

                            if verb == "buy":
                                if not args:
                                    live.console.print(
                                        "[yellow]usage:[/yellow] buy <sol>"
                                    )
                                    continue
                                try:
                                    sol = float(args[0])
                                except ValueError:
                                    live.console.print(
                                        f"[yellow]bad sol amount:[/yellow] "
                                        f"{args[0]}"
                                    )
                                    continue
                                data = _post_paper(
                                    settings,
                                    "/paper/buy",
                                    {
                                        "mint": mint,
                                        "sol": sol,
                                        **_tick_body(
                                            last_tick,
                                            name=name,
                                            symbol=symbol,
                                        ),
                                    },
                                    live.console,
                                )
                                if data is None:
                                    continue
                                fill = data.get("fill") or {}
                                pos = _position_from_api(fill.get("position"))
                                bought = True
                                result.bought = True
                                label = (
                                    fill.get("symbol")
                                    or fill.get("name")
                                    or mint[:8]
                                )
                                live.console.print(
                                    f"[green]BOUGHT[/green] {label}  "
                                    f"spent={float(fill.get('sol') or 0):.4f} "
                                    f"SOL  "
                                    f"tokens="
                                    f"{float(fill.get('tokens') or 0):,.4f}  "
                                    f"FDV="
                                    f"${float(fill.get('fdv_usd') or 0):,.0f}"
                                )
                                if pos is not None:
                                    live.console.print(
                                        format_position_header(pos)
                                    )
                                live.console.print(
                                    "[dim]cmds: buy <sol> | "
                                    "sell [pct|init|<n> sol] | "
                                    "set profit/loss <pct>|clear | "
                                    "reject | leave[/dim]"
                                )
                                if last_tick is not None:
                                    price_line = format_watch_line(
                                        last_tick, pos
                                    )
                                _refresh(live)
                                continue

                            if verb in ("set_profit", "set_loss"):
                                if pos is None or pos.tokens <= 0:
                                    live.console.print(
                                        "[yellow]buy first, then set "
                                        "profit/loss[/yellow]"
                                    )
                                    continue
                                side = (
                                    "profit"
                                    if verb == "set_profit"
                                    else "loss"
                                )
                                if not args:
                                    live.console.print(
                                        f"[yellow]usage:[/yellow] "
                                        f"set {side} <pct>|clear"
                                    )
                                    continue
                                body_tpsl: dict[str, Any] = {"mint": mint}
                                token = args[0].lower()
                                if token in ("clear", "off", "none", "0"):
                                    body_tpsl[
                                        "clear_profit"
                                        if side == "profit"
                                        else "clear_loss"
                                    ] = True
                                else:
                                    try:
                                        pct = float(args[0])
                                    except ValueError:
                                        live.console.print(
                                            f"[yellow]bad %:[/yellow] {args[0]}"
                                        )
                                        continue
                                    if pct <= 0:
                                        live.console.print(
                                            "[yellow]% must be > 0 "
                                            "(or clear)[/yellow]"
                                        )
                                        continue
                                    body_tpsl[
                                        "profit_pct"
                                        if side == "profit"
                                        else "loss_pct"
                                    ] = pct
                                data = _post_paper(
                                    settings,
                                    "/paper/tpsl",
                                    body_tpsl,
                                    live.console,
                                )
                                if data is None:
                                    continue
                                _apply_tpsl_to_pos(pos, data)
                                tp = data.get("profit_pct")
                                sl = data.get("loss_pct")
                                tp_s = (
                                    f"+{float(tp):g}%"
                                    if tp is not None
                                    else "—"
                                )
                                sl_s = (
                                    f"-{float(sl):g}%"
                                    if sl is not None
                                    else "—"
                                )
                                mon = (
                                    "monitoring"
                                    if data.get("monitoring")
                                    else "idle"
                                )
                                live.console.print(
                                    f"[yellow]TPSL[/yellow] TP={tp_s}  "
                                    f"SL={sl_s}  [{mon}]"
                                )
                                if last_tick is not None:
                                    price_line = format_watch_line(
                                        last_tick, pos
                                    )
                                _refresh(live)
                                continue

                            if verb == "sell":
                                if pos is None or pos.tokens <= 0:
                                    live.console.print(
                                        "[yellow]no open position to sell"
                                        "[/yellow]"
                                    )
                                    continue
                                body: dict[str, Any] = {
                                    "mint": mint,
                                    "initials": False,
                                }
                                if not args:
                                    pass  # full
                                elif (
                                    len(args) == 1
                                    and args[0].lower() == "init"
                                ):
                                    body["initials"] = True
                                elif (
                                    len(args) >= 2
                                    and args[1].lower() in ("sol", "sols")
                                ):
                                    try:
                                        body["sol_amount"] = float(args[0])
                                    except ValueError:
                                        live.console.print(
                                            "[yellow]bad sol amount:[/yellow] "
                                            f"{args[0]}"
                                        )
                                        continue
                                else:
                                    try:
                                        body["percent"] = float(args[0])
                                    except ValueError:
                                        live.console.print(
                                            "[yellow]usage:[/yellow] "
                                            "sell | sell <pct> | sell init | "
                                            "sell <n> sol"
                                        )
                                        continue
                                data = _post_paper(
                                    settings,
                                    "/paper/sell",
                                    {
                                        **body,
                                        **_tick_body(
                                            last_tick,
                                            name=name,
                                            symbol=symbol,
                                        ),
                                    },
                                    live.console,
                                )
                                if data is None:
                                    continue
                                fill = data.get("fill") or {}
                                pnl = float(fill.get("pnl_sol") or 0.0)
                                pct = float(fill.get("pnl_pct") or 0.0)
                                color = "green" if pnl >= 0 else "red"
                                sign = "+" if pnl >= 0 else ""
                                live.console.print(
                                    f"[{color}]SOLD[/{color}]  "
                                    f"proceeds="
                                    f"{float(fill.get('sol') or 0):.4f} SOL  "
                                    f"PnL={sign}{pnl:.4f} SOL "
                                    f"({sign}{pct:.2f}%)"
                                )
                                pos = _position_from_api(
                                    fill.get("position")
                                )
                                if pos is not None:
                                    live.console.print(
                                        format_position_header(pos)
                                    )
                                else:
                                    live.console.print(
                                        "[dim]flat — still watching; "
                                        "buy / reject / leave[/dim]"
                                    )
                                if last_tick is not None:
                                    price_line = format_watch_line(
                                        last_tick, pos
                                    )
                                _refresh(live)
                                continue

                            live.console.print(
                                f"[yellow]unknown cmd:[/yellow] {raw}  "
                                "(buy / sell / set profit|loss / reject / leave)"
                            )
                            continue

                        # backspace / delete
                        if ch in ("\x7f", "\b"):
                            if input_buf:
                                input_buf = input_buf[:-1]
                                _refresh(live)
                            continue

                        # ctrl+c
                        if ch == "\x03":
                            result.action = "quit"
                            result.bought = bought
                            break

                        # ctrl+d on empty → quit
                        if ch == "\x04":
                            if not input_buf:
                                result.action = "quit"
                                result.bought = bought
                                break
                            continue

                        # ignore other control chars / escape sequences (skip)
                        if ch == "\x1b":
                            # drain short escape seq if any
                            await asyncio.sleep(0.02)
                            while True:
                                ready = select.select(
                                    [sys.stdin], [], [], 0
                                )[0]
                                if not ready:
                                    break
                                sys.stdin.read(1)
                            continue

                        if ch.isprintable():
                            input_buf += ch
                            _refresh(live)
    finally:
        stop_stdin.set()
        next_tick.cancel()
        next_key.cancel()
        reader_task.cancel()
        for t in (next_tick, next_key, reader_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    return result


def _wait_for_backlog_item(
    ledger_path: Path,
    console: Console,
    *,
    poll_sec: float = 0.75,
) -> BacklogItem | None:
    """
    block until a pending backlog row appears (sqlite only).

    returns None if user hits ctrl+c.
    """
    shown_wait = False
    try:
        while True:
            item = peek_fifo(ledger_path)
            if item is not None:
                return item
            if not shown_wait:
                console.print(
                    "[dim]waiting for filter-passed migrations in backlog "
                    "(sqlite poll — daemon unchanged)…[/dim]"
                )
                shown_wait = True
            time.sleep(poll_sec)
    except KeyboardInterrupt:
        return None


def run_watch_trade(
    settings: Settings,
    console: Console | None,
    mint: str,
) -> None:
    """
    live watch one mint with the same buy / sell / set profit|loss cmds
    as the filtered trade stream. does not touch the backlog.
    """
    console = console or Console()
    mint = mint.strip()
    if not settings.helius_api_key.strip():
        console.print("[red]ARCHETYPE_HELIUS_API_KEY not set[/red]")
        return
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    rpc = HeliusRpc(settings.helius_api_key)
    ledger = settings.resolved_trading_ledger_db()
    ensure_agent_tables(ledger)
    console.print(
        f"[bold]watch trade[/bold] {mint}  "
        "[dim]buy · sell · set profit/loss · reject · leave[/dim]"
    )
    try:
        session = asyncio.run(
            _interactive_watch(
                rpc,
                mint,
                console,
                settings,
                ledger_path=ledger,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watching[/dim]")
        return
    if session.action == "reject":
        record_reject(
            ledger,
            mint,
            reasoning="manual reject (watch --trade)",
            decision="reject",
        )
        console.print(f"[red]REJECTED[/red] {mint} → agent_rejects")
    else:
        console.print("[dim]stopped watching[/dim]")


def run_filtered_trade_stream(
    settings: Settings,
    console: Console | None = None,
) -> None:
    """
    main loop: backlog → watch|reject → next.

    daemon just keeps writing PASSes to migration_backlog like always.
    """
    console = console or Console()
    ledger = settings.resolved_trading_ledger_db()
    ensure_agent_tables(ledger)

    if not settings.helius_api_key.strip():
        console.print("[red]ARCHETYPE_HELIUS_API_KEY not set[/red]")
        return

    # quiet httpx noise during long sessions
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    rpc = HeliusRpc(settings.helius_api_key)
    try:
        resp = httpx.get(f"{settings.base_url}/agent/loop", timeout=3.0)
        if resp.status_code == 200 and (resp.json() or {}).get("running"):
            console.print(
                "[yellow]agent loop is running and owns the backlog. "
                "POST /agent/loop/stop before this stream.[/yellow]"
            )
            return
    except httpx.HTTPError:
        pass
    console.print(
        "[bold]filtered trade stream[/bold]  "
        "[dim]watch / reject · during watch: buy · sell · set profit/loss · reject · leave[/dim]"
    )

    try:
        while True:
            item = _wait_for_backlog_item(ledger, console)
            if item is None:
                console.print("\n[dim]stopped trade stream[/dim]")
                return

            pending = pending_count(ledger)
            _print_candidate(console, item, pending)

            # first decision prompt
            decision: str | None = None
            while decision is None:
                raw = _read_line("> ")
                decision = _parse_decision(raw)
                if decision is None:
                    console.print(
                        "[yellow]say watch, reject, or quit[/yellow]"
                    )

            if decision == "quit":
                console.print("[dim]left trade stream (backlog untouched)[/dim]")
                return

            # first decision → off backlog
            taken = consume_id(ledger, item.id)
            if taken is None:
                console.print(
                    "[yellow]that backlog row was already consumed — "
                    "grabbing next[/yellow]"
                )
                continue
            item = taken
            info = item.coin_info or {}

            if decision == "reject":
                record_reject(
                    ledger,
                    item.mint,
                    reasoning="manual reject (cli trade stream)",
                    decision="reject",
                )
                console.print(
                    f"[red]REJECTED[/red] {_coin_label(item)} → agent_rejects"
                )
                continue

            # watch path
            session = asyncio.run(
                _interactive_watch(
                    rpc,
                    item.mint,
                    console,
                    settings,
                    ledger_path=ledger,
                    name=info.get("name"),
                    symbol=info.get("symbol"),
                    known_pool=info.get("pool") or info.get("pool_address"),
                )
            )

            if session.action == "reject":
                record_reject(
                    ledger,
                    item.mint,
                    reasoning="manual reject after watch (cli trade stream)",
                    decision="reject",
                )
                console.print(
                    f"[red]REJECTED[/red] {_coin_label(item)} → agent_rejects"
                )
            elif session.bought:
                record_review(
                    ledger,
                    item.mint,
                    "traded",
                    backlog_id=item.id,
                    coin_info=info,
                    notes="bought during cli trade stream",
                )
                console.print(
                    f"[green]TRADED[/green] {_coin_label(item)} → agent_reviews"
                )
            else:
                record_review(
                    ledger,
                    item.mint,
                    "exited",
                    backlog_id=item.id,
                    coin_info=info,
                    notes="watched then left (not rejected)",
                )
                console.print(
                    f"[dim]EXITED[/dim] {_coin_label(item)} → agent_reviews "
                    "(aside, not rejected)"
                )

            if session.action == "quit":
                console.print("[dim]left trade stream[/dim]")
                return

    except KeyboardInterrupt:
        console.print("\n[dim]stopped trade stream[/dim]")
