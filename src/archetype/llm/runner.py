"""
background lock loop.

one asyncio task beside the filter feed and TP/SL monitors.
off unless config says so, or POST /agent/loop/start.
idle ticks do not call the model. a think tick logs mint, action, seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from archetype.config import Settings
from archetype.filters.helius import HeliusRpc
from archetype.llm.binder import AgentToolkit
from archetype.llm.client import make_chat
from archetype.llm.graph import LockLoop, LoopDeps, fresh_state, load_system_prompt
from archetype.llm.memory import FocusMemory

log = logging.getLogger("archetype.agent.loop")


class AgentLoop:
    """owns the LockLoop task. start/stop from the daemon lifespan."""

    def __init__(
        self,
        settings: Settings,
        rpc: HeliusRpc,
        *,
        tpsl: Any = None,
        alerts: Any = None,
        lock: LockLoop | None = None,
        idle_sec: float = 2.0,
        think_sec: float = 12.0,
    ) -> None:
        self.settings = settings
        self.rpc = rpc
        self.tpsl = tpsl
        self.alerts = alerts
        self.idle_sec = idle_sec
        self.think_sec = think_sec
        self._lock = lock
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.state: dict[str, Any] = dict(fresh_state())
        self.last_tick_sec: float | None = None
        self.ticks = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """what GET /agent/loop returns."""
        return {
            "ok": True,
            "running": self.running,
            "phase": self.state.get("phase") or "idle",
            "mint": self.state.get("mint"),
            "last_action": self.state.get("last_action"),
            "last_narrative": self.state.get("last_narrative"),
            "last_error": self.state.get("last_error"),
            "steps": self.state.get("steps") or 0,
            "last_tick_sec": self.last_tick_sec,
            "ticks": self.ticks,
        }

    async def start(self) -> dict[str, Any]:
        """spawn the task if it isn't already running."""
        if self.running:
            return self.status()
        self._stop = asyncio.Event()
        if self._lock is None:
            self._lock = self._build_lock()
        self._task = asyncio.create_task(self._run(), name="agent-loop")
        log.info("agent loop started (ollama %s)", self.settings.ollama_model)
        log.info(
            "agent loop is the backlog consumer — "
            "don't also run migrations --trade"
        )
        return self.status()

    async def stop(self) -> dict[str, Any]:
        """cancel the task. filters and TP/SL keep going."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        log.info("agent loop stopped")
        return self.status()

    def _build_lock(self) -> LockLoop:
        ledger = self.settings.resolved_trading_ledger_db()
        active = self.settings.resolved_active_context_db()
        tools = AgentToolkit(
            rpc=self.rpc,
            ledger_path=ledger,
            tpsl=self.tpsl,
            alerts=self.alerts,
        )
        memory = FocusMemory(active, ledger)
        return LockLoop(
            LoopDeps(
                chat=make_chat(self.settings),
                tools=tools,
                memory=memory,
                prompt=load_system_prompt(),
            )
        )

    async def _run(self) -> None:
        assert self._lock is not None
        try:
            while not self._stop.is_set():
                phase_before = self.state.get("phase") or "idle"
                t0 = time.perf_counter()
                try:
                    self.state = await self._lock.tick(self.state)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("agent tick failed: %s", exc)
                    self.state["last_error"] = str(exc)
                    self.last_tick_sec = time.perf_counter() - t0
                    await self._sleep(self.idle_sec)
                    continue
                self.ticks += 1
                self.last_tick_sec = time.perf_counter() - t0
                phase = self.state.get("phase") or "idle"
                # empty backlog never called the model — keep that log quiet
                if phase != "idle" or phase_before != "idle" or self.state.get("last_action"):
                    log.info(
                        "AGENT phase=%s mint=%s action=%s %.2fs",
                        phase,
                        self.state.get("mint") or "-",
                        self.state.get("last_action") or "-",
                        self.last_tick_sec,
                    )
                delay = self.idle_sec if phase == "idle" else self._think_delay()
                await self._sleep(delay)
        except asyncio.CancelledError:
            return

    def _think_delay(self) -> float:
        """shorter wait when a move alert is already sitting in the queue."""
        pending = 0
        if self.alerts is not None:
            try:
                pending = int(self.alerts.status().get("pending_count") or 0)
            except Exception:  # noqa: BLE001
                pending = 0
        if pending:
            return min(1.0, self.think_sec)
        return self.think_sec

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sec)
        except TimeoutError:
            return
