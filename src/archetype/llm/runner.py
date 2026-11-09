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

import httpx

from archetype.config import Settings
from archetype.filters.helius import HeliusRpc
from archetype.llm.binder import AgentToolkit
from archetype.llm.client import OLLAMA_NUM_CTX, exc_text, make_chat, ollama_num_thread
from archetype.llm.feed import AgentFeed
from archetype.llm.graph import (
    LockLoop,
    LoopDeps,
    fresh_state,
    load_system_prompt,
    load_thesis_prompt,
)
from archetype.llm.memory import FocusMemory
from archetype.tools.vamp import (
    VampBuffer,
    VampHit,
    format_vamp_alert,
    match_vamp,
)

log = logging.getLogger("archetype.agent.loop")


class AgentLoop:
    """owns the LockLoop task. start/stop from the daemon lifespan."""

    def __init__(
        self,
        settings: Settings,
        rpc: HeliusRpc,
        *,
        tpsl: Any = None,
        live: Any = None,
        lock: LockLoop | None = None,
        idle_sec: float = 2.0,
        think_sec: float = 12.0,
    ) -> None:
        self.settings = settings
        self.rpc = rpc
        self.tpsl = tpsl
        self.live = live
        self.idle_sec = idle_sec
        self.think_sec = think_sec
        self._lock = lock
        # only warm ollama when we own the chat client (tests inject a fake lock)
        self._warm_model = lock is None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.state: dict[str, Any] = dict(fresh_state())
        self.last_tick_sec: float | None = None
        self.ticks = 0
        # migrations --follow polls this. survives across start/stop.
        self.feed = AgentFeed()
        self._seen_hit_seqs: set[int] = set()
        # in-memory vamp backlog for the current focus (not sqlite)
        self.vamp = VampBuffer()
        # live focus for the filter thread (updated mid-tick via focus_hook)
        self._focus_snap: dict[str, Any] = {
            "phase": "idle",
            "mint": None,
            "coin": None,
        }

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
            live=self.live,
        )
        memory = FocusMemory(active, ledger)
        feed = self.feed

        def _emit(kind: str, **fields: Any) -> None:
            feed.push(kind, **fields)

        return LockLoop(
            LoopDeps(
                chat=make_chat(self.settings),
                tools=tools,
                memory=memory,
                prompt=load_system_prompt(self.settings),
                thesis_prompt=load_thesis_prompt(),
                emit=_emit,
                buy_sol=self.settings.buy_sol_for_mcap,
                abort=asyncio.Event(),
                abort_reason=None,
                vamp=self.vamp,
                focus_hook=self._on_focus_change,
            )
        )

    def _on_focus_change(
        self,
        phase: str,
        mint: str | None,
        coin: dict[str, Any] | None,
    ) -> None:
        """graph calls this when focus starts/locks/ends — mid-tick safe."""
        self._focus_snap = {
            "phase": str(phase or "idle"),
            "mint": (mint or "").strip() or None,
            "coin": dict(coin) if isinstance(coin, dict) else None,
        }

    def focus_snapshot(self) -> dict[str, Any] | None:
        """
        phase + focus coin for the vamp matcher.

        prefers the mid-tick focus_hook snapshot; falls back to last tick state.
        safe to call from the filter worker thread.
        """
        snap = self._focus_snap
        phase = str(snap.get("phase") or "idle")
        mint = str(snap.get("mint") or "").strip()
        coin = snap.get("coin")
        if phase == "idle" or not mint or not isinstance(coin, dict):
            # fallback: last completed tick (e.g. before first focus_hook)
            state = self.state
            phase = str(state.get("phase") or "idle")
            mint = str(state.get("mint") or "").strip()
            coin = state.get("coin")
            if phase == "idle" or not mint or not isinstance(coin, dict):
                return None
        return {
            "phase": phase,
            "mint": mint,
            "coin": dict(coin),
        }

    def on_passed_migration(self, candidate: dict[str, Any]) -> None:
        """
        filter PASS hook. only while focused; searching buffers, locked alerts.

        called from the filter thread after enqueue_passed — keep it sync.
        """
        if not isinstance(candidate, dict):
            return
        snap = self.focus_snapshot()
        if snap is None:
            return
        hit = match_vamp(snap["coin"], candidate)
        if hit is None:
            return
        phase = snap["phase"]
        if phase == "searching":
            self.vamp.buffer_add(hit)
            return
        if phase == "locked":
            self._alert_locked_vamp(hit)
            return
        # idle / unknown — ignore
        log.debug("VAMP ignore phase=%s mint=%s", phase, hit.mint[:8])

    def _alert_locked_vamp(self, hit: VampHit) -> None:
        """write event, live emit, cancel in-flight think with vamp reason."""
        deps = getattr(self._lock, "deps", None) if self._lock is not None else None
        focus = str(self.state.get("mint") or "").strip()
        if not focus or deps is None:
            # loop not armed yet — keep it so a later flush can still fire
            self.vamp.buffer_add(hit)
            return
        # locked path — already holding
        text = format_vamp_alert(hit, holding=True, explain=True)
        payload = hit.to_payload(text=text)
        try:
            deps.memory.append_event(focus, "vamp", payload)
            self.vamp.note_presented(hit.mint)
        except Exception:  # noqa: BLE001
            log.warning("VAMP event not stored", exc_info=True)
        emit = getattr(deps, "emit", None)
        if emit is not None:
            try:
                emit(
                    "vamp",
                    mint=hit.mint,
                    name=hit.name or "",
                    symbol=hit.symbol or "",
                    text=text,
                    matched_on=list(hit.matched_on),
                    why=hit.why,
                    focus_mint=focus,
                )
            except Exception:  # noqa: BLE001
                log.debug("VAMP live emit failed", exc_info=True)
        # cancel in-flight think; bag stays open (not tpsl close)
        deps.abort_reason = "vamp"
        if deps.abort is not None:
            deps.abort.set()
        log.info(
            "VAMP interrupt focus=%s cand=%s on=%s",
            focus[:8],
            hit.mint[:8],
            ",".join(hit.matched_on),
        )

    async def _warm_ollama(self) -> None:
        """load the weights before the first coin. cold start ate the 30s timeout."""
        url = self.settings.ollama_base_url.rstrip("/") + "/api/chat"
        payload = {
            "model": self.settings.ollama_model,
            "messages": [{"role": "user", "content": "Reply with JSON {\"ok\": true}"}],
            "format": "json",
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0,
                "num_predict": 8,
                "num_ctx": OLLAMA_NUM_CTX,
                "num_thread": ollama_num_thread(self.settings),
            },
        }
        timeout = max(float(self.settings.ollama_timeout_sec), 150.0)
        last = "ollama warm failed"
        for attempt in (1, 2):
            try:
                t0 = time.perf_counter()
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                log.info(
                    "ollama warm in %.1fs (%s threads)",
                    time.perf_counter() - t0,
                    ollama_num_thread(self.settings),
                )
                return
            except Exception as exc:  # noqa: BLE001
                last = exc_text(exc)
                log.warning("ollama warm attempt %s failed: %s", attempt, last)
        log.warning("starting anyway after warm failed: %s", last)

    def _drain_hits(self) -> None:
        """tp/sl sells happen off to the side. push them to the live feed."""
        tpsl = self.tpsl
        hits = getattr(tpsl, "_hits", None) if tpsl is not None else None
        if not hits:
            return
        focus = str(self.state.get("mint") or "")
        memory = getattr(getattr(self._lock, "deps", None), "memory", None)
        for hit in list(hits.values()):
            seq = int(getattr(hit, "seq", 0) or 0)
            if seq and seq in self._seen_hit_seqs:
                continue
            if seq:
                self._seen_hit_seqs.add(seq)
            mint = str(getattr(hit, "mint", "") or "")
            self.feed.push(
                "tpsl",
                mint=mint,
                name=getattr(hit, "name", "") or "",
                symbol=getattr(hit, "symbol", "") or "",
                reason=getattr(hit, "reason", "") or "",
                sol=getattr(hit, "sol", None),
                pnl_sol=getattr(hit, "pnl_sol", None),
                pnl_pct=getattr(hit, "pnl_pct", None),
            )
            # so the close summary can see the stop or the target
            if memory is not None and focus and mint == focus:
                try:
                    memory.append_event(
                        mint,
                        "tpsl",
                        {
                            "reason": getattr(hit, "reason", ""),
                            "sol": getattr(hit, "sol", None),
                            "pnl_sol": getattr(hit, "pnl_sol", None),
                            "pnl_pct": getattr(hit, "pnl_pct", None),
                            "sell_mcap_usd": getattr(hit, "fdv_usd", None),
                        },
                    )
                except Exception:  # noqa: BLE001
                    log.debug("could not store tpsl event", exc_info=True)
                # the coin is flat. drop the think that is still running.
                deps = getattr(self._lock, "deps", None) if self._lock else None
                if deps is not None:
                    deps.abort_reason = "tpsl"
                    if deps.abort is not None:
                        deps.abort.set()

    async def _watch_hits(self) -> None:
        """check tp/sl about once a second so the cli is not stuck waiting on a think."""
        while not self._stop.is_set():
            self._drain_hits()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def _run(self) -> None:
        assert self._lock is not None
        hit_task = asyncio.create_task(self._watch_hits(), name="agent-hits")
        try:
            if self._warm_model:
                await self._warm_ollama()
            while not self._stop.is_set():
                phase_before = self.state.get("phase") or "idle"
                t0 = time.perf_counter()
                try:
                    self.state = await self._lock.tick(self.state)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("agent tick failed: %s", exc_text(exc))
                    self.state["last_error"] = exc_text(exc)
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
                delay = self.idle_sec if phase == "idle" else self.think_sec
                # bare wait/hold asks for a shorter/explicit gap (default 10s)
                deps = getattr(self._lock, "deps", None) if self._lock else None
                override = getattr(deps, "next_delay_sec", None) if deps else None
                if isinstance(override, (int, float)) and override > 0:
                    delay = float(override)
                    deps.next_delay_sec = None
                await self._sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            hit_task.cancel()
            try:
                await hit_task
            except asyncio.CancelledError:
                pass

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sec)
        except TimeoutError:
            return
