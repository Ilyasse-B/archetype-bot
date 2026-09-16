"""
cli client — talks to the daemon over localhost fastapi.

keeps interactive stuff out of the agent process.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx
import typer
from rich.console import Console
from rich.table import Table

from archetype import __version__
from archetype.config import get_settings
from archetype.process import pidfile_path, stop_daemon

app = typer.Typer(
    name="archetype",
    help="Archetype memecoin agent CLI (talks to local daemon).",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _client() -> tuple[httpx.Client, str]:
    """httpx client pointed at local daemon."""
    settings = get_settings()
    return httpx.Client(base_url=settings.base_url, timeout=10.0), settings.base_url


def _get(path: str, timeout: float = 5.0) -> httpx.Response:
    """GET against the local daemon; exit nicely if its down."""
    settings = get_settings()
    url = f"{settings.base_url}{path}"
    try:
        return httpx.get(url, timeout=timeout)
    except httpx.ConnectError:
        console.print(
            f"[red]daemon not reachable at {settings.base_url}[/red]\n"
            "start it with: [bold]uv run python -m archetype.daemon[/bold]"
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        console.print(f"[red]request failed:[/red] {exc}")
        raise typer.Exit(code=1) from None


def _print_migration_rows(migrations: list[dict], show_filter: bool = False) -> None:
    """pretty table for migration events."""
    if not migrations:
        console.print("[dim]no migrations in buffer yet[/dim]")
        return
    table = Table(title="Recent migrations")
    table.add_column("#", style="cyan")
    table.add_column("received")
    table.add_column("filter")
    table.add_column("name")
    table.add_column("socials")
    table.add_column("mcap$")
    table.add_column("feesSOL")
    table.add_column("mint")
    table.add_column("reasons")
    for m in migrations:
        received = m.get("received_at") or ""
        if isinstance(received, str) and "T" in received:
            received = received.replace("T", " ")[:19]
        passed = m.get("filter_passed")
        filt = "PASS" if passed is True else ("FAIL" if passed is False else "…")
        metrics = m.get("filter_metrics") or {}
        meta = m.get("metadata") or metrics.get("metadata") or {}
        name = (m.get("name") or meta.get("name") or "")[:20]
        socials = ",".join(meta.get("socials_present") or metrics.get("socials_present") or [])
        mcap = metrics.get("market_cap_usd")
        fees = metrics.get("estimated_global_fees_sol")
        mcap_s = f"{mcap:,.0f}" if isinstance(mcap, (int, float)) else ""
        fees_s = f"{fees:.2f}" if isinstance(fees, (int, float)) else ""
        reasons = "; ".join(m.get("filter_reasons") or [])[:60]
        table.add_row(
            str(m.get("id", "")),
            str(received),
            filt,
            name,
            socials,
            mcap_s,
            fees_s,
            str(m.get("mint") or ""),
            reasons,
        )
    console.print(table)


@app.callback()
def main_callback() -> None:
    """Archetype CLI."""
    return None


@app.command("version")
def version_cmd() -> None:
    """print cli package version."""
    console.print(f"archetype {__version__}")


@app.command("status")
def status_cmd(
    json_out: bool = typer.Option(
        False,
        "--json",
        help="raw json from the daemon",
    ),
) -> None:
    """ask the daemon for health / db / phase status."""
    resp = _get("/status")
    if resp.status_code != 200:
        console.print(f"[red]status failed:[/red] HTTP {resp.status_code}")
        raise typer.Exit(code=1)

    data = resp.json()
    if json_out:
        console.print_json(data=data)
        return

    table = Table(title="Archetype daemon status")
    table.add_column("field", style="cyan")
    table.add_column("value")

    rows = [
        ("ok", str(data.get("ok"))),
        ("version", data.get("version")),
        ("phase", data.get("phase")),
        ("agent_state", data.get("agent_state")),
        ("listen", f"{data.get('host')}:{data.get('port')}"),
        ("pumpportal_configured", str(data.get("pumpportal_configured"))),
        ("pumpportal_connected", str(data.get("pumpportal_connected"))),
        ("helius_configured", str(data.get("helius_configured"))),
        ("migrations_received", str(data.get("migrations_received"))),
        ("migrations_passed", str(data.get("migrations_passed"))),
        ("migrations_rejected", str(data.get("migrations_rejected"))),
        ("backlog_pending", str(data.get("backlog_pending"))),
        ("migrations_buffered", str(data.get("migrations_buffered"))),
        ("pumpportal_last_error", str(data.get("pumpportal_last_error"))),
        ("active_context_db", data.get("active_context_db")),
        ("active_context_ok", str(data.get("active_context_ok"))),
        ("trading_ledger_db", data.get("trading_ledger_db")),
        ("trading_ledger_ok", str(data.get("trading_ledger_ok"))),
        ("message", data.get("message")),
        ("server_time", data.get("server_time")),
    ]
    for field, value in rows:
        table.add_row(field, str(value))

    console.print(table)
    if not data.get("ok"):
        raise typer.Exit(code=2)


@app.command("health")
def health_cmd() -> None:
    """quick liveness check."""
    resp = _get("/health")
    if resp.status_code != 200:
        console.print(f"[red]unhealthy[/red] HTTP {resp.status_code}")
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/green] {resp.json()}")


@app.command("migrations")
def migrations_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="how many recent events"),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="keep polling and print new migrations as they arrive",
    ),
    filtered: bool = typer.Option(
        False,
        "--filtered",
        help="only show migrations that passed on-chain filters",
    ),
    json_out: bool = typer.Option(False, "--json", help="raw json"),
) -> None:
    """show recent PumpPortal migrations (raw or filtered)."""
    q = f"limit={limit}&filtered={'true' if filtered else 'false'}"
    if not follow:
        resp = _get(f"/migrations?{q}")
        if resp.status_code != 200:
            console.print(f"[red]failed:[/red] HTTP {resp.status_code}")
            raise typer.Exit(code=1)
        data = resp.json()
        if json_out:
            console.print_json(data=data)
            return
        mode = "FILTERED" if filtered else "RAW (unfiltered)"
        console.print(
            f"[bold]{mode}[/bold]  connected={data.get('connected')}  "
            f"showing {data.get('count', 0)} event(s)"
        )
        _print_migration_rows(data.get("migrations") or [], show_filter=True)
        return

    console.print(
        f"[dim]following {'filtered' if filtered else 'raw'} migrations "
        f"(ctrl+c to stop)…[/dim]"
    )
    last_id = 0
    try:
        while True:
            resp = _get(f"/migrations?after_id={last_id}&{q}")
            if resp.status_code != 200:
                console.print(f"[red]poll failed[/red] HTTP {resp.status_code}")
                time.sleep(2)
                continue
            data = resp.json()
            events = data.get("migrations") or []
            for m in events:
                last_id = max(last_id, int(m.get("id") or 0))
                ts = datetime.now().strftime("%H:%M:%S")
                mint = m.get("mint")
                passed = m.get("filter_passed")
                tag = (
                    "[green]PASS[/green]"
                    if passed is True
                    else ("[red]FAIL[/red]" if passed is False else "[dim]…[/dim]")
                )
                reasons = m.get("filter_reasons") or []
                reason_s = ("; ".join(reasons))[:80] if reasons else ""
                metrics = m.get("filter_metrics") or {}
                meta = m.get("metadata") or metrics.get("metadata") or {}
                mcap = metrics.get("market_cap_usd")
                fees = metrics.get("estimated_global_fees_sol")
                name = m.get("name") or meta.get("name")
                socials = ",".join(meta.get("socials_present") or metrics.get("socials_present") or [])
                extra = ""
                if name:
                    extra += f" name={name[:24]}"
                if socials:
                    extra += f" socials={socials}"
                if mcap is not None:
                    extra += f" mcap=${mcap:,.0f}" if isinstance(mcap, (int, float)) else ""
                if fees is not None:
                    extra += f" fees~{fees:.2f}SOL" if isinstance(fees, (int, float)) else ""
                console.print(
                    f"[green]{ts}[/green] #{m.get('id')} {tag} mint={mint}{extra}"
                    + (f" — {reason_s}" if reason_s and passed is False else "")
                )
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped following[/dim]")


@app.command("backlog")
def backlog_cmd(
    limit: int = typer.Option(50, "--limit", "-n", help="max pending items to show"),
    json_out: bool = typer.Option(False, "--json", help="raw json"),
) -> None:
    """list pending migration backlog (FIFO, oldest first)."""
    resp = _get(f"/backlog?limit={limit}")
    if resp.status_code != 200:
        console.print(f"[red]failed:[/red] HTTP {resp.status_code}")
        raise typer.Exit(code=1)
    data = resp.json()
    if json_out:
        console.print_json(data=data)
        return
    items = data.get("items") or []
    console.print(f"[bold]BACKLOG[/bold]  pending={data.get('count', 0)}")
    if not items:
        console.print("[dim]empty — waiting for filter PASSes[/dim]")
        return
    table = Table(title="Migration backlog (oldest → newest)")
    table.add_column("#", style="cyan")
    table.add_column("queued")
    table.add_column("name")
    table.add_column("mcap$")
    table.add_column("feesSOL")
    table.add_column("socials")
    table.add_column("mint")
    for it in items:
        info = it.get("coin_info") or {}
        ts = str(it.get("timestamp") or "")[:19]
        mcap = info.get("market_cap_usd")
        fees = info.get("estimated_global_fees_sol")
        socials = ",".join(info.get("socials_present") or [])
        table.add_row(
            str(it.get("id", "")),
            ts,
            str(info.get("name") or "")[:20],
            f"{mcap:,.0f}" if isinstance(mcap, (int, float)) else "",
            f"{fees:.2f}" if isinstance(fees, (int, float)) else "",
            socials,
            str(it.get("mint") or ""),
        )
    console.print(table)


@app.command("watch")
def watch_cmd(
    mint: str = typer.Argument(..., help="token mint address to watch"),
    once: bool = typer.Option(
        False,
        "--once",
        help="print one snapshot and exit",
    ),
) -> None:
    """
    live PumpSwap FDV for a mint (inline).

    if you hold an open paper position on that mint, also shows value + PnL.
    """
    import logging

    from archetype.execution import format_watch_line, get_position, run_inline_watch
    from archetype.filters.helius import HeliusRpc
    from archetype.price import fetch_price_tick

    settings = get_settings()
    if not settings.helius_api_key.strip():
        console.print("[red]ARCHETYPE_HELIUS_API_KEY not set[/red]")
        raise typer.Exit(code=1)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    rpc = HeliusRpc(settings.helius_api_key)
    mint = mint.strip()
    ledger = settings.resolved_trading_ledger_db()
    pos = get_position(ledger, mint)

    try:
        if once:
            tick = fetch_price_tick(rpc, mint)
            label = tick.name or tick.symbol or mint[:8]
            console.print(
                f"[bold]watching[/bold] {label}  mint={mint}"
                + ("  [position]" if pos else "")
            )
            if pos is not None:
                entry_fdv = (
                    f"${pos.entry_fdv_usd:,.0f}"
                    if pos.entry_fdv_usd is not None
                    else "?"
                )
                console.print(
                    f"[dim]cost={pos.cost_sol:.4f} SOL  "
                    f"avg entry FDV={entry_fdv}[/dim]"
                )
            console.print(format_watch_line(tick, pos))
            return

        run_inline_watch(rpc, mint, console, position=pos)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watching[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]watch failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _require_helius() -> tuple:
    """settings + rpc, or exit if no key."""
    import logging

    from archetype.filters.helius import HeliusRpc

    settings = get_settings()
    if not settings.helius_api_key.strip():
        console.print("[red]ARCHETYPE_HELIUS_API_KEY not set[/red]")
        raise typer.Exit(code=1)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return settings, HeliusRpc(settings.helius_api_key)


@app.command("buy")
def buy_cmd(
    mint: str = typer.Argument(..., help="token mint to buy"),
    sol: float = typer.Argument(..., help="SOL to spend (paper)"),
    no_track: bool = typer.Option(
        False,
        "--no-track",
        help="skip live watch after fill",
    ),
) -> None:
    """paper-buy at current PumpSwap price, then live inline FDV/PnL."""
    from archetype.execution import buy, run_inline_watch

    settings, rpc = _require_helius()
    ledger = settings.resolved_trading_ledger_db()
    mint = mint.strip()

    try:
        fill = buy(ledger, rpc, mint, sol)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]buy failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    label = fill.symbol or fill.name or mint[:8]
    console.print(
        f"[green]BOUGHT[/green] {label}  "
        f"spent={fill.sol:.4f} SOL  tokens={fill.tokens:,.4f}  "
        f"@ {fill.price_sol:.10g} SOL  FDV=${fill.fdv_usd:,.0f}"
    )
    entry_fdv = (
        f"${fill.position.entry_fdv_usd:,.0f}"
        if fill.position.entry_fdv_usd is not None
        else "?"
    )
    console.print(
        f"[dim]wallet={fill.wallet_sol:.4f} SOL  "
        f"position cost={fill.position.cost_sol:.4f} SOL  "
        f"avg entry FDV={entry_fdv}[/dim]"
    )
    if not no_track and fill.position is not None:
        run_inline_watch(rpc, mint, console, position=fill.position)


@app.command("sell")
def sell_cmd(
    mint: str = typer.Argument(..., help="token mint to sell (full position)"),
) -> None:
    """paper-sell full open position at current PumpSwap price."""
    from archetype.execution import sell

    settings, rpc = _require_helius()
    ledger = settings.resolved_trading_ledger_db()
    mint = mint.strip()

    try:
        fill = sell(ledger, rpc, mint)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]sell failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    label = fill.symbol or fill.name or mint[:8]
    pnl = fill.pnl_sol or 0.0
    pct = fill.pnl_pct or 0.0
    color = "green" if pnl >= 0 else "red"
    sign = "+" if pnl >= 0 else ""
    console.print(
        f"[{color}]SOLD[/{color}] {label}  "
        f"proceeds={fill.sol:.4f} SOL  "
        f"PnL={sign}{pnl:.4f} SOL ({sign}{pct:.2f}%)  "
        f"@ {fill.price_sol:.10g} SOL  FDV=${fill.fdv_usd:,.0f}"
    )
    console.print(f"[dim]wallet={fill.wallet_sol:.4f} SOL[/dim]")


balance_app = typer.Typer(
    help="mock SOL wallet + open positions",
    no_args_is_help=False,
)
app.add_typer(balance_app, name="balance")


@balance_app.callback(invoke_without_command=True)
def balance_cmd(ctx: typer.Context) -> None:
    """show cash SOL + open positions (one-shot price marks)."""
    if ctx.invoked_subcommand is not None:
        return

    from archetype.execution import get_balance, list_positions, mark_position

    settings, rpc = _require_helius()
    ledger = settings.resolved_trading_ledger_db()
    cash = get_balance(ledger)
    positions = list_positions(ledger)

    console.print(f"[bold]paper wallet[/bold]  cash=[cyan]{cash:.4f} SOL[/cyan]")
    if not positions:
        console.print("[dim]no open positions[/dim]")
        return

    table = Table(title="Open positions")
    table.add_column("symbol")
    table.add_column("name")
    table.add_column("mint")
    table.add_column("value SOL", justify="right")
    table.add_column("PnL %", justify="right")
    table.add_column("cost SOL", justify="right")
    table.add_column("FDV$", justify="right")

    for pos in positions:
        try:
            mark = mark_position(rpc, pos)
            pnl_s = f"{mark.pnl_pct:+.2f}%"
            table.add_row(
                mark.symbol or pos.symbol or "?",
                (mark.name or pos.name or "")[:20],
                pos.mint,
                f"{mark.value_sol:.4f}",
                pnl_s,
                f"{pos.cost_sol:.4f}",
                f"{mark.fdv_usd:,.0f}",
            )
        except Exception as exc:  # noqa: BLE001
            table.add_row(
                pos.symbol or "?",
                (pos.name or "")[:20],
                pos.mint,
                "?",
                f"[red]err[/red]",
                f"{pos.cost_sol:.4f}",
                "?",
            )
            console.print(f"[yellow]mark failed {pos.mint[:8]}…:[/yellow] {exc}")

    console.print(table)


@balance_app.command("add")
def balance_add_cmd(
    sol: float = typer.Argument(..., help="SOL to add to paper wallet"),
) -> None:
    """add mock SOL to the paper wallet."""
    from archetype.execution import add_sol

    settings = get_settings()
    ledger = settings.resolved_trading_ledger_db()
    try:
        new_bal = add_sol(ledger, sol)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]add failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]added[/green] {sol:.4f} SOL → balance={new_bal:.4f} SOL")


@balance_app.command("reset")
def balance_reset_cmd(
    start: float = typer.Option(
        100.0,
        "--start",
        help="SOL balance after reset (also clears open positions)",
    ),
) -> None:
    """reset paper wallet to starting SOL and clear open positions."""
    from archetype.execution import reset_wallet

    settings = get_settings()
    ledger = settings.resolved_trading_ledger_db()
    new_bal = reset_wallet(ledger, start_sol=start)
    console.print(
        f"[green]reset[/green] wallet → {new_bal:.4f} SOL "
        f"(open positions cleared)"
    )


@app.command("stop")
def stop_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="skip http shutdown and SIGTERM via pidfile only",
    ),
) -> None:
    """stop the local daemon and free the port."""
    settings = get_settings()
    pid_path = pidfile_path(settings.data_dir)

    if not force:
        try:
            resp = httpx.post(f"{settings.base_url}/shutdown", timeout=3.0)
            if resp.status_code == 200:
                console.print("[green]shutdown requested[/green] via HTTP")
                time.sleep(0.6)
                ok, msg = stop_daemon(pid_path, timeout_sec=3.0)
                if ok:
                    console.print(f"[green]{msg}[/green]")
                else:
                    console.print("[green]daemon stopped; port freed[/green]")
                return
        except httpx.HTTPError:
            pass

    ok, msg = stop_daemon(pid_path)
    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[yellow]{msg}[/yellow]")
        console.print(
            "if the port is still busy: "
            f"[bold]ss -tlnp | grep {settings.port}[/bold]"
        )
        raise typer.Exit(code=1)


def run() -> None:
    """entrypoint helper."""
    app()


if __name__ == "__main__":
    app()
