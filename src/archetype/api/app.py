"""
fastapi control plane for the daemon.
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
from archetype.filters import run_onchain_filters
from archetype.filters.helius import HeliusRpc
from archetype.ingest import MigrationBuffer, MigrationEvent, PumpPortalMigrationFeed


class StatusResponse(BaseModel):
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
    helius_configured: bool
    migrations_received: int
    migrations_buffered: int
    migrations_passed: int
    migrations_rejected: int
    pumpportal_last_error: str | None = None


class MigrationsResponse(BaseModel):
    connected: bool
    count: int
    filtered: bool = False
    migrations: list[MigrationEvent] = Field(default_factory=list)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    buffer = MigrationBuffer(maxlen=200)
    rpc = HeliusRpc(settings.helius_api_key)
    ledger = settings.resolved_trading_ledger_db()

    async def _filter_hook(event: MigrationEvent) -> MigrationEvent:
        """run deterministic filters off the event loop."""
        if not rpc.enabled:
            event.filter_passed = False
            event.filter_reasons = ["helius api key missing — cannot filter"]
            return event

        def _run() -> MigrationEvent:
            result = run_onchain_filters(event, rpc, ledger)
            event.filter_passed = result.passed
            event.filter_reasons = list(result.reasons)
            event.filter_metrics = dict(result.metrics)
            return event

        return await asyncio.to_thread(_run)

    feed = PumpPortalMigrationFeed(
        api_key=settings.pumpportal_api_key,
        buffer=buffer,
        on_migration=_filter_hook if rpc.enabled else None,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await feed.start()
        yield
        await feed.stop()

    app = FastAPI(title="Archetype Agent", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.started_at = datetime.now(timezone.utc)
    app.state.migration_buffer = buffer
    app.state.pumpportal_feed = feed
    app.state.helius_rpc = rpc

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/status", response_model=StatusResponse)
    def status() -> StatusResponse:
        s: Settings = app.state.settings
        buf: MigrationBuffer = app.state.migration_buffer
        feed_status = buf.snapshot_status()
        active = s.resolved_active_context_db()
        ledger_p = s.resolved_trading_ledger_db()
        active_ok = db_exists_and_ok(active)
        ledger_ok = db_exists_and_ok(ledger_p)
        pp_ok = bool(s.pumpportal_api_key.strip())
        helius_ok = bool(s.helius_api_key.strip())
        ok = active_ok and ledger_ok
        msg = "daemon up; migrations + on-chain filters active"
        if not pp_ok:
            msg = "daemon up; pumpportal key missing"
        elif not helius_ok:
            msg = "daemon up; helius key missing — raw feed only (no filters)"
        elif not feed_status.get("connected"):
            msg = "daemon up; connecting to pumpportal…"
        return StatusResponse(
            ok=ok,
            version=__version__,
            agent_state=s.agent_state,
            host=s.host,
            port=s.port,
            active_context_db=str(active),
            trading_ledger_db=str(ledger_p),
            active_context_ok=active_ok,
            trading_ledger_ok=ledger_ok,
            phase="2-filters",
            message=msg,
            server_time=datetime.now(timezone.utc).isoformat(),
            pumpportal_configured=pp_ok,
            pumpportal_connected=bool(feed_status.get("connected")),
            helius_configured=helius_ok,
            migrations_received=int(feed_status.get("received_count") or 0),
            migrations_buffered=int(feed_status.get("buffered") or 0),
            migrations_passed=int(feed_status.get("filtered_pass_count") or 0),
            migrations_rejected=int(feed_status.get("filtered_reject_count") or 0),
            pumpportal_last_error=(
                str(feed_status["last_error"]) if feed_status.get("last_error") else None
            ),
        )

    @app.get("/migrations", response_model=MigrationsResponse)
    async def migrations(
        limit: int = Query(20, ge=1, le=200),
        after_id: int = Query(0, ge=0),
        filtered: bool = Query(
            False,
            description="if true, only migrations that passed on-chain filters",
        ),
    ) -> MigrationsResponse:
        buf: MigrationBuffer = app.state.migration_buffer
        if after_id > 0:
            items = await buf.since(after_id, filtered_only=filtered)
        else:
            items = await buf.recent(limit, filtered_only=filtered)
        return MigrationsResponse(
            connected=buf.connected,
            count=len(items),
            filtered=filtered,
            migrations=items,
        )

    @app.post("/shutdown")
    async def shutdown() -> dict[str, Any]:
        async def _exit_soon() -> None:
            await asyncio.sleep(0.15)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_exit_soon())
        return {"ok": True, "message": "shutting down"}

    return app
