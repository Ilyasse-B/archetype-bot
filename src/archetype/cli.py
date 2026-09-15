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


def _print_migration_rows(migrations: list[dict]) -> None:
    """pretty table for migration events."""
    if not migrations:
        console.print("[dim]no migrations in buffer yet[/dim]")
        return
    table = Table(title="Recent migrations")
    table.add_column("#", style="cyan")
    table.add_column("received")
    table.add_column("symbol")
    table.add_column("name")
    table.add_column("mint")
    table.add_column("pool")
    for m in migrations:
        received = m.get("received_at") or ""
        if isinstance(received, str) and "T" in received:
            # trim to local-ish short form
            received = received.replace("T", " ")[:19]
        table.add_row(
            str(m.get("id", "")),
            str(received),
            str(m.get("symbol") or ""),
            str(m.get("name") or "")[:24],
            str(m.get("mint") or ""),
            str(m.get("pool") or "")[:20],
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
        ("migrations_received", str(data.get("migrations_received"))),
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
    json_out: bool = typer.Option(False, "--json", help="raw json"),
) -> None:
    """show recent PumpPortal migrations (from daemon buffer)."""
    if not follow:
        resp = _get(f"/migrations?limit={limit}")
        if resp.status_code != 200:
            console.print(f"[red]failed:[/red] HTTP {resp.status_code}")
            raise typer.Exit(code=1)
        data = resp.json()
        if json_out:
            console.print_json(data=data)
            return
        connected = data.get("connected")
        console.print(
            f"pumpportal connected={connected}  showing {data.get('count', 0)} event(s)"
        )
        _print_migration_rows(data.get("migrations") or [])
        return

    # follow mode — poll every second for id > last
    console.print("[dim]following migrations (ctrl+c to stop)…[/dim]")
    last_id = 0
    try:
        while True:
            resp = _get(f"/migrations?after_id={last_id}&limit={limit}")
            if resp.status_code != 200:
                console.print(f"[red]poll failed[/red] HTTP {resp.status_code}")
                time.sleep(2)
                continue
            data = resp.json()
            events = data.get("migrations") or []
            for m in events:
                last_id = max(last_id, int(m.get("id") or 0))
                ts = datetime.now().strftime("%H:%M:%S")
                symbol = m.get("symbol") or "?"
                name = m.get("name") or ""
                mint = m.get("mint")
                pool = m.get("pool") or ""
                console.print(
                    f"[green]{ts}[/green] #{m.get('id')} "
                    f"[bold]{symbol}[/bold] {name} "
                    f"mint={mint} pool={pool}"
                )
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped following[/dim]")


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
