"""
fastapi control plane for the daemon.

cli hits these routes on localhost — keeps ui out of the hot path later.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from archetype import __version__
from archetype.config import Settings, get_settings
from archetype.db import db_exists_and_ok


class StatusResponse(BaseModel):
    """what `archetype status` shows."""

    ok: bool
    version: str
    agent_state: str
    host: str
    port: int
    active_context_db: str
    trading_ledger_db: str
    active_context_ok: bool
    trading_ledger_ok: bool
    phase: str
    message: str
    server_time: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """build the fastapi app (used by daemon + tests)."""
    settings = settings or get_settings()
    app = FastAPI(title="Archetype Agent", version=__version__)

    # stash settings on app state so routes can read them
    app.state.settings = settings
    app.state.started_at = datetime.now(timezone.utc)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """lightweight liveness probe."""
        return {"ok": True, "version": __version__}

    @app.get("/status", response_model=StatusResponse)
    def status() -> StatusResponse:
        """full status for the cli."""
        s: Settings = app.state.settings
        active = s.resolved_active_context_db()
        ledger = s.resolved_trading_ledger_db()
        active_ok = db_exists_and_ok(active)
        ledger_ok = db_exists_and_ok(ledger)
        ok = active_ok and ledger_ok
        return StatusResponse(
            ok=ok,
            version=__version__,
            agent_state=s.agent_state,
            host=s.host,
            port=s.port,
            active_context_db=str(active),
            trading_ledger_db=str(ledger),
            active_context_ok=active_ok,
            trading_ledger_ok=ledger_ok,
            phase="1-scaffold",
            message=(
                "daemon up; trading loop not wired yet (phase 1)"
                if ok
                else "daemon up but databases missing — run init or restart daemon"
            ),
            server_time=datetime.now(timezone.utc).isoformat(),
        )

    @app.post("/shutdown")
    async def shutdown() -> dict[str, Any]:
        """
        ask the daemon to exit gracefully (releases the port).

        used by `archetype stop` over http; pidfile SIGTERM is the fallback.
        """

        async def _exit_soon() -> None:
            await asyncio.sleep(0.15)
            # SIGTERM hits our daemon handler → uvicorn closes the socket
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_exit_soon())
        return {"ok": True, "message": "shutting down"}

    return app
