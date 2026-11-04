"""
fastapi control plane for the daemon.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from archetype import __version__
from archetype.api.agent import create_agent_router
from archetype.api.live import LiveHub, sse_data
from archetype.api.paper import create_paper_router
from archetype.backlog import (
    clear_pending,
    dequeue_fifo,
    enqueue_passed,
    list_pending,
    pending_count,
)
from archetype.config import Settings, get_settings
from archetype.db import db_exists_and_ok
from archetype.filters import run_full_filters
from archetype.filters.helius import HeliusRpc
from archetype.ingest import MigrationBuffer, MigrationEvent, PumpPortalMigrationFeed
from archetype.execution import ensure_wallet
from archetype.execution.tpsl import TpslEngine
from archetype.price.live import LivePrices
from archetype.llm.runner import AgentLoop

log = logging.getLogger("archetype.app")


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
    backlog_pending: int = 0
    pumpportal_last_error: str | None = None


class MigrationsResponse(BaseModel):
    connected: bool
    count: int
    filtered: bool = False
    migrations: list[MigrationEvent] = Field(default_factory=list)


class BacklogItemResponse(BaseModel):
    id: int
    mint: str
    coin_info: dict[str, Any] = Field(default_factory=dict)
    timestamp: str
    consumed: bool = False


class BacklogResponse(BaseModel):
    count: int
    items: list[BacklogItemResponse] = Field(default_factory=list)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    hub = LiveHub()

    def _publish_migration(event: MigrationEvent) -> None:
        hub.publish(
            {"stream": "migration", "migration": event.model_dump(mode="json")}
        )

    buffer = MigrationBuffer(maxlen=200, on_change=_publish_migration)
    rpc = HeliusRpc(settings.helius_api_key)
    ledger = settings.resolved_trading_ledger_db()

    async def _filter_hook(event: MigrationEvent) -> MigrationEvent:
        """repair mint → run deterministic filters off the event loop; enqueue PASS."""
        if not rpc.enabled:
            event.filter_passed = False
            event.filter_reasons = ["helius api key missing — cannot filter"]
            return event

        def _run() -> MigrationEvent:
            from archetype.ingest.repair import repair_migration_event

            # fix quote-as-mint frames BEFORE blacklist / on-chain gates
            ev = event
            outcome = repair_migration_event(ev, rpc)
            ev = outcome.event
            if outcome.reject_reason:
                ev.filter_passed = False
                ev.filter_reasons = [outcome.reject_reason]
                ev.filter_metrics = {
                    "repair_rejected": True,
                    "venue": ev.venue,
                    "quote_mint": ev.quote_mint,
                }
                log.info("FILTER REJECT %s — %s", ev.mint, outcome.reject_reason)
                return ev

            result = run_full_filters(ev, rpc, ledger)
            ev.filter_passed = result.passed
            ev.filter_reasons = list(result.reasons)
            ev.filter_metrics = dict(result.metrics)
            if result.passed:
                enqueue_passed(ledger, ev)
            return ev

        return await asyncio.to_thread(_run)

    feed = PumpPortalMigrationFeed(
        api_key=settings.pumpportal_api_key,
        buffer=buffer,
        on_migration=_filter_hook if rpc.enabled else None,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # old passes must not be the first coin the agent sees
        dropped = await asyncio.to_thread(clear_pending, ledger)
        log.info("cleared %s pending backlog rows from the last run", dropped)
        # ensure paper wallet tables exist
        await asyncio.to_thread(ensure_wallet, ledger)
        # one vault socket per coin. peek and tp/sl read it, they dont open another.
        live_prices = LivePrices(rpc=rpc, ledger_path=ledger)
        tpsl = TpslEngine(rpc=rpc, ledger_path=ledger, live=live_prices)
        agent_loop = AgentLoop(settings, rpc, tpsl=tpsl, live=live_prices)
        agent_loop.feed.on_push = lambda item: hub.publish(
            {"stream": "agent", "event": item}
        )
        app.state.tpsl_engine = tpsl
        app.state.live_prices = live_prices
        app.state.agent_loop = agent_loop
        await tpsl.start()
        await feed.start()
        if settings.agent_loop_enabled:
            await agent_loop.start()
        yield
        await agent_loop.stop()
        await feed.stop()
        await tpsl.stop()
        rpc.close()

    app = FastAPI(title="Archetype Agent", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.started_at = datetime.now(timezone.utc)
    app.state.migration_buffer = buffer
    app.state.pumpportal_feed = feed
    app.state.helius_rpc = rpc
    app.state.live = hub

    @app.get("/live")
    async def live(
        filtered: bool = Query(
            False,
            description="if true, only push migrations that passed filters",
        ),
    ) -> StreamingResponse:
        """push migrations and agent lines as they happen. one connection."""

        async def _gen():
            loop = getattr(app.state, "agent_loop", None)
            running = bool(loop.running) if loop is not None else False
            q = hub.subscribe()
            try:
                yield sse_data({"stream": "hello", "agent_running": running})
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    except TimeoutError:
                        # comment frame keeps the socket up. not an http request.
                        yield ": ping\n\n"
                        continue
                    if filtered and msg.get("stream") == "migration":
                        mig = msg.get("migration") or {}
                        if mig.get("filter_passed") is not True:
                            continue
                    yield sse_data(msg)
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(create_paper_router())
    app.include_router(create_agent_router())

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
        loop = getattr(app.state, "agent_loop", None)
        loop_status = loop.status() if loop is not None else {}
        agent_phase = (
            str(loop_status.get("phase") or "idle")
            if loop_status.get("running")
            else "stopped"
        )
        ok = active_ok and ledger_ok
        msg = "daemon up; migrations + filters + backlog + paper + agent API"
        if not pp_ok:
            msg = "daemon up; pumpportal key missing"
        elif not helius_ok:
            msg = "daemon up; helius key missing — raw feed only (no filters/paper)"
        elif not feed_status.get("connected"):
            msg = "daemon up; connecting to pumpportal…"
        return StatusResponse(
            ok=ok,
            version=__version__,
            agent_state=agent_phase,
            host=s.host,
            port=s.port,
            active_context_db=str(active),
            trading_ledger_db=str(ledger_p),
            active_context_ok=active_ok,
            trading_ledger_ok=ledger_ok,
            phase="3-agent-api",
            message=msg,
            server_time=datetime.now(timezone.utc).isoformat(),
            pumpportal_configured=pp_ok,
            pumpportal_connected=bool(feed_status.get("connected")),
            helius_configured=helius_ok,
            migrations_received=int(feed_status.get("received_count") or 0),
            migrations_buffered=int(feed_status.get("buffered") or 0),
            migrations_passed=int(feed_status.get("filtered_pass_count") or 0),
            migrations_rejected=int(feed_status.get("filtered_reject_count") or 0),
            backlog_pending=pending_count(ledger_p),
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

    @app.get("/backlog", response_model=BacklogResponse)
    def backlog(
        limit: int = Query(50, ge=1, le=200),
    ) -> BacklogResponse:
        """list pending (unconsumed) backlog items, oldest first."""
        ledger_p: Path = app.state.settings.resolved_trading_ledger_db()
        items = list_pending(ledger_p, limit=limit)
        return BacklogResponse(
            count=len(items),
            items=[
                BacklogItemResponse(
                    id=i.id,
                    mint=i.mint,
                    coin_info=i.coin_info,
                    timestamp=i.timestamp,
                    consumed=i.consumed,
                )
                for i in items
            ],
        )

    @app.post("/backlog/pop")
    def backlog_pop() -> dict[str, Any]:
        """manual FIFO pop (for testing / later agent). marks consumed."""
        ledger_p: Path = app.state.settings.resolved_trading_ledger_db()
        item = dequeue_fifo(ledger_p)
        if item is None:
            return {"ok": True, "item": None, "message": "backlog empty"}
        return {"ok": True, "item": item.to_dict()}

    @app.post("/shutdown")
    async def shutdown() -> dict[str, Any]:
        async def _exit_soon() -> None:
            await asyncio.sleep(0.15)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_exit_soon())
        return {"ok": True, "message": "shutting down"}

    return app
