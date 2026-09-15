"""
daemon entrypoint — one process: fastapi + (later) agent/engines.

phase 1: init dbs, pidfile, serve control api, clean shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import logging
import signal
import sys

import uvicorn

from archetype.api import create_app
from archetype.config import get_settings
from archetype.db import init_databases
from archetype.process import (
    already_running,
    pidfile_path,
    remove_pidfile,
    write_pidfile,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("archetype.daemon")


def main() -> None:
    """boot databases then run uvicorn on localhost with clean teardown."""
    settings = get_settings()
    active = settings.resolved_active_context_db()
    ledger = settings.resolved_trading_ledger_db()
    pid_path = pidfile_path(settings.data_dir)

    # dont start a second copy — thats what ate the port last time
    existing = already_running(pid_path)
    if existing is not None:
        log.error(
            "daemon already running (pid %s). stop it first:\n"
            "  uv run archetype stop\n"
            "or: kill %s",
            existing,
            existing,
        )
        sys.exit(1)

    log.info("initializing databases under %s", settings.data_dir)
    init_databases(active, ledger)
    log.info("active_context -> %s", active)
    log.info("trading_ledger -> %s", ledger)

    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        # so a quick restart after clean exit can rebind
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    def _shutdown(signum: int, _frame: object) -> None:
        # uvicorn picks this up and closes the listening socket
        name = signal.Signals(signum).name
        log.info("got %s — shutting down (releasing %s:%s)", name, settings.host, settings.port)
        server.should_exit = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    write_pidfile(pid_path)
    log.info("pidfile -> %s (pid %s)", pid_path, pid_path.read_text().strip())
    log.info("starting fastapi on %s:%s", settings.host, settings.port)
    if settings.pumpportal_api_key.strip():
        log.info("pumpportal migration feed: enabled")
    else:
        log.warning("pumpportal migration feed: DISABLED (set ARCHETYPE_PUMPPORTAL_API_KEY)")

    try:
        server.run()
    except SystemExit:
        raise
    except OSError as exc:
        # errno 98 = address already in use
        if getattr(exc, "errno", None) == 98:
            log.error(
                "port %s already in use. something else is bound there.\n"
                "  uv run archetype stop\n"
                "  ss -tlnp | grep %s",
                settings.port,
                settings.port,
            )
            sys.exit(1)
        raise
    finally:
        # always drop pidfile so the next start is clean
        remove_pidfile(pid_path)
        log.info("shutdown complete; port should be free")


if __name__ == "__main__":
    main()
