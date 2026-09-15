"""
fastapi control plane for the daemon.

cli hits these routes on localhost — keeps ui out of the hot path later.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from archetype import __version__
from archetype.config import Settings, get_settings
from archetype.db import db_exists_and_ok
from archetype.ingest import MigrationBuffer, MigrationEvent, PumpPortalMigrationFeed


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
    pumpportal_configured: bool
    pumpportal_connected: bool
    migrations_received: int
    migrations_buffered: int
    pumpportal_last_error: str | None = None


class MigrationsResponse(BaseModel):
    """recent migrations from the in-memory buffer."""

    connected: bool
    count: int
    migrations: list[MigrationEvent] = Field(default_factory=list)


def create_app(settings: Settings | None = None) -> FastAPI:
    """build the fastapi app (used by daemon + tests)."""
    settings = settings or get_settings()
    buffer = MigrationBuffer(maxlen=200)
    feed = PumpPortalMigrationFeed(
        api_key=settings.pumpportal_api_key,
        buffer=buffer,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # start pumpportal in the background
        await feed.start()
        yield
        await feed.stop()

    app = FastAPI(title="Archetype Agent", version=__version__, lifespan=lifespan)

    # stash on app state for routes
    app.state.settings = settings
    app.state.started_at = datetime.now(timezone.utc)
    app.state.migration_buffer = buffer
    app.state.pumpportal_feed = feed

    @app.get("/health")
    def health() -> dict[str, Any]:
        """lightweight liveness probe."""
        return {"ok": True, "version": __version__}

    @app.get("/status", response_model=StatusResponse)
    def status() -> StatusResponse:
        """full status for the cli."""
        s: Settings = app.state.settings
        buf: MigrationBuffer = app.state.migration_buffer
        feed_status = buf.snapshot_status()
        active = s.resolved_active_context_db()
        ledger = s.resolved_trading_ledger_db()
        active_ok = db_exists_and_ok(active)
        ledger_ok = db_exists_and_ok(ledger)
        configured = bool(s.pumpportal_api_key.strip())
        ok = active_ok and ledger_ok
        msg = "daemon up; pumpportal migration feed active"
        if not configured:
            msg = "daemon up; pumpportal key missing — feed disabled"
        elif not feed_status.get("connected"):
            msg = "daemon up; connecting to pumpportal…"
            if feed_status.get("last_error"):
                msg = f"daemon up; pumpportal error: {feed_status['last_error']}"
        elif feed_status.get("last_error"):
            # connected but had a non-fatal notice earlier
            msg = "daemon up; pumpportal connected (see last_error if any)"
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
            phase="2-migrations",
            message=msg,
            server_time=datetime.now(timezone.utc).isoformat(),
            pumpportal_configured=configured,
            pumpportal_connected=bool(feed_status.get("connected")),
            migrations_received=int(feed_status.get("received_count") or 0),
            migrations_buffered=int(feed_status.get("buffered") or 0),
            pumpportal_last_error=(
                str(feed_status["last_error"]) if feed_status.get("last_error") else None
            ),
        )

    @app.get("/migrations", response_model=MigrationsResponse)
    async def migrations(
        limit: int = Query(20, ge=1, le=200),
        after_id: int = Query(0, ge=0, description="only events with id > after_id"),
    ) -> MigrationsResponse:
        """recent migrations (or only new ones if after_id set)."""
        buf: MigrationBuffer = app.state.migration_buffer
        if after_id > 0:
            items = await buf.since(after_id)
        else:
            items = await buf.recent(limit)
        return MigrationsResponse(
            connected=buf.connected,
            count=len(items),
            migrations=items,
        )

    @app.post("/shutdown")
    async def shutdown() -> dict[str, Any]:
        """
        ask the daemon to exit gracefully (releases the port).

        used by `archetype stop` over http; pidfile SIGTERM is the fallback.
        """

        async def _exit_soon() -> None:
            await asyncio.sleep(0.15)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_exit_soon())
        return {"ok": True, "message": "shutting down"}

    return app
