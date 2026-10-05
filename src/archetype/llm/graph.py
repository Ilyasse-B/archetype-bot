"""
lock-loop state machine.

one tick = one transition, then stop. the daemon (later) calls tick again.
no model call when the backlog is empty.
searching: pop one coin, one decision (reject / hold / buy).
locked: one decision (hold / revise_tpsl / sell) from the first prompt plus the last 5 events.
flat bag: one thesis call over the full history, then unlock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from archetype.execution.wallet import get_position
from archetype.llm.choices import decision_format, decision_from_message
from archetype.llm.client import exc_text
from archetype.llm.decision import DecisionInvalid, ThinkDecision, coach_text
from archetype.llm.memory import FocusMemory
from archetype.llm.transcript import flush_transcript, render_thesis, render_think
from archetype.tools.price_change import alert_line, since_last_line, since_last_peek

log = logging.getLogger("archetype.graph")

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "trader.md"
THESIS_PATH = PROMPT_PATH.with_name("thesis.md")
REJECT_THESIS_PATH = PROMPT_PATH.with_name("thesis_reject.md")

Phase = Literal["idle", "searching", "locked"]
_FREE_ACTIONS = frozenset({"reject", "hold", "buy"})
_LOCKED_ACTIONS = frozenset({"hold", "sell", "revise_tpsl"})
# first look counts. a 4th think while still searching is a reject.
_MAX_SEARCH_LOOKS = 3


class LoopState(TypedDict, total=False):
    """serializable loop state. deps live on the runtime, not in here."""

    phase: Phase
    mint: str | None
    coin: dict[str, Any] | None
    backlog_id: int | None
    round_id: int | None
    steps: int
    route: str
    last_action: str | None
    last_narrative: str | None
    last_error: str | None
    pending_decision: dict[str, Any] | None
    # previous think's market cap, so the next packet can say how it moved
    last_peek_mcap: float | None
    last_peek_at: float | None
    # what the last prompt already told the model, so the next one can skip it
    told_phase: str | None
    told_flat: bool | None
    told_summary_empty: bool | None


@dataclass
class LoopDeps:
    """what a tick is allowed to touch."""

    chat: Any
    tools: Any
    memory: FocusMemory
    prompt: str
    # tests swap this so we don't need a live position row
    position_open: Callable[[str], bool] | None = None
    # live lines for migrations --follow. None in tests.
    emit: Callable[..., None] | None = None
    # fixed buy size from mcap. None keeps whatever the model sent.
    buy_sol: Callable[[float | None], float] | None = None
    thesis_prompt: str = ""
    # set when take-profit or stop-loss sells the coin mid-think
    abort: asyncio.Event | None = None


def _fmt_num(val: float) -> str:
    if float(val).is_integer():
        return str(int(val))
    return str(val)


def load_system_prompt(settings: Any | None = None) -> str:
    """standing trader instructions, with the buy-size settings filled in."""
    raw = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if settings is None:
        from archetype.config import get_settings

        settings = get_settings()
    repl = {
        "{{MCAP_SMALL}}": _fmt_num(settings.buy_mcap_small_usd),
        "{{MCAP_MID}}": _fmt_num(settings.buy_mcap_mid_usd),
        "{{BUY_SOL_SMALL}}": _fmt_num(settings.buy_sol_small),
        "{{BUY_SOL_MID}}": _fmt_num(settings.buy_sol_mid),
        "{{BUY_SOL_LARGE}}": _fmt_num(settings.buy_sol_large),
    }
    for token, val in repl.items():
        raw = raw.replace(token, val)
    return raw


def load_thesis_prompt() -> str:
    """close-out after a buy that actually filled. take-profit, stop-loss, or a sell."""
    return THESIS_PATH.read_text(encoding="utf-8").strip()


def load_reject_thesis_prompt() -> str:
    """close-out after a reject. no trade happened."""
    return REJECT_THESIS_PATH.read_text(encoding="utf-8").strip()


def _queue_reject(deps: LoopDeps, mint: str, steps: int, reason: str) -> LoopState:
    """skip the model and reject. used when the search looks are used up."""
    log.info("SEARCH CAP %s — %s", mint, reason)
    deps.memory.append_event(
        mint,
        "decision",
        {
            "action": "reject",
            "reasoning": reason,
            "narrative": None,
            "narrative_updated": False,
        },
    )
    decision = ThinkDecision(action="reject", reasoning=reason, narrative="")
    return {
        "steps": steps,
        "pending_decision": decision.model_dump(),
        "last_error": reason,
        "route": "apply",
    }


def _label_action(action: str, phase: str) -> str:
    """before a buy, say wait. hold is only once the coin is owned."""
    if action == "hold" and phase != "locked":
        return "wait"
    return action


def _timed_out(exc: BaseException) -> bool:
    """read timeouts and httpx timeouts. a bad json reply is not one of these."""
    if "timeout" in type(exc).__name__.lower():
        return True
    text = exc_text(exc).lower()
    return "timeout" in text or "timed out" in text


def _pool_pubkey(coin: dict[str, Any]) -> str | None:
    """backlog pool_address is the pumpswap pool. the word pump-amm is not."""
    raw = coin.get("pool_address") or ""
    if isinstance(raw, str) and len(raw) >= 32:
        return raw
    return None


async def _arm_price_watch(deps: LoopDeps, mint: str, coin: dict[str, Any]) -> None:
    """start the watcher once the agent is on this coin. already-on is a no-op."""
    alerts = getattr(deps.tools, "alerts", None)
    if alerts is None or not mint:
        return
    try:
        await alerts.subscribe(
            mint,
            name=coin.get("name") or None,
            symbol=coin.get("symbol") or None,
            pool=_pool_pubkey(coin),
        )
    except Exception as exc:  # noqa: BLE001
        log.info("price watch not armed %s %s", mint, exc)


async def _stop_price_watch(deps: LoopDeps, mint: str) -> None:
    """drop the watcher when we leave the coin."""
    alerts = getattr(deps.tools, "alerts", None)
    if alerts is None or not mint:
        return
    try:
        await alerts.unsubscribe(mint)
    except Exception as exc:  # noqa: BLE001
        log.info("price watch stop failed %s %s", mint, exc)


def _with_peek(update: dict[str, Any], peek_state: dict[str, Any]) -> dict[str, Any]:
    """keep the last peek on every return so the next think can diff it."""
    if not peek_state:
        return update
    return {**peek_state, **update}


def _clear_abort(deps: LoopDeps) -> None:
    """the flat signal is for this coin only."""
    if deps.abort is not None and deps.abort.is_set():
        deps.abort.clear()


def _bag_flat(deps: LoopDeps, phase: str, mint: str) -> bool:
    """true when take-profit or stop-loss already sold this coin."""
    if phase != "locked" or not mint:
        return False
    if deps.abort is not None and deps.abort.is_set():
        return True
    return not _is_open(deps, mint)


async def _call_model(chat: Any, messages: list[dict[str, str]], abort: asyncio.Event | None):
    """
    one model call. if the bag goes flat mid-call, cancel it.

    returns (message, aborted).
    """
    task = asyncio.create_task(chat.ainvoke(messages))
    if abort is None:
        return await task, False
    stop = asyncio.create_task(abort.wait())
    try:
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        stop.cancel()
        raise
    if abort.is_set():
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        stop.cancel()
        return None, True
    stop.cancel()
    return task.result(), False


def _emit(deps: LoopDeps, kind: str, **fields: Any) -> None:
    """push a live line. a bad subscriber must not kill the trade."""
    fn = deps.emit
    if fn is None:
        return
    try:
        fn(kind, **fields)
    except Exception:  # noqa: BLE001
        log.debug("agent feed emit failed", exc_info=True)


def _save_think(
    deps: LoopDeps,
    mint: str,
    messages: list[dict[str, str]],
    reply: str | None,
    *,
    error: str | None = None,
) -> None:
    """keep this call. the file is written when we leave the coin."""
    if not mint:
        return
    try:
        n = len(deps.memory.turns(mint)) + 1
        deps.memory.add_turn(
            mint, render_think(n, messages, reply, error=error)
        )
    except Exception:  # noqa: BLE001
        log.info("transcript think not stored %s", mint, exc_info=True)


def _flush_transcript(deps: LoopDeps, mint: str, symbol: str, reason: str) -> None:
    """append the saved chat to transcripts/<mint>.txt. misses stay in the log."""
    if not mint:
        return
    try:
        flush_transcript(deps.memory, mint, symbol, reason)
    except Exception:  # noqa: BLE001
        log.info("transcript not written %s", mint, exc_info=True)


def _coin_mcap(state: LoopState) -> float | None:
    coin = state.get("coin") or {}
    mcap = coin.get("market_cap_usd")
    if isinstance(mcap, (int, float)):
        return float(mcap)
    return None


def _required_sol(deps: LoopDeps, state: LoopState) -> float | None:
    if deps.buy_sol is None:
        return None
    try:
        return float(deps.buy_sol(_coin_mcap(state)))
    except (TypeError, ValueError):
        return None


def fresh_state() -> LoopState:
    """idle, nothing focused."""
    return {"phase": "idle", "steps": 0, "mint": None}


class LockLoop:
    """compiled graph. tick() once, keep the returned state, tick again later."""

    def __init__(self, deps: LoopDeps) -> None:
        self.deps = deps
        self.graph = _build()

    async def tick(self, state: LoopState | None = None) -> LoopState:
        """run one transition. does not wait or loop inside."""
        current = dict(state or fresh_state())
        out = await self.graph.ainvoke(current, context=self.deps)
        return out  # type: ignore[return-value]


def _build():
    g = StateGraph(LoopState, context_schema=LoopDeps)
    g.add_node("ingress", _ingress)
    g.add_node("pop", _pop)
    g.add_node("think", _think)
    g.add_node("apply", _apply)
    g.add_node("release", _release)
    g.add_node("close", _close)
    g.add_edge(START, "ingress")
    g.add_conditional_edges(
        "ingress",
        _route,
        {"pop": "pop", "think": "think", "close": "close", "stop": END},
    )
    g.add_conditional_edges(
        "pop",
        _route,
        {"think": "think", "stop": END},
    )
    g.add_conditional_edges(
        "think",
        _route,
        {"apply": "apply", "close": "close", "stop": END},
    )
    g.add_conditional_edges(
        "apply",
        _route,
        {"release": "release", "close": "close", "stop": END},
    )
    g.add_edge("release", END)
    g.add_edge("close", END)
    return g.compile()


def _route(state: LoopState) -> str:
    return state.get("route") or "stop"


async def _ingress(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    phase = state.get("phase") or "idle"
    mint = (state.get("mint") or "").strip()
    if phase == "locked" and mint:
        if _is_open(runtime.context, mint):
            return {"route": "think"}
        log.info("FLAT %s — position gone, writing thesis", mint)
        return {"route": "close"}
    if phase == "searching" and mint:
        return {"route": "think"}
    return {"route": "pop"}


async def _pop(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    deps = runtime.context
    # clear focus so next_candidate is allowed
    deps.tools.set_focus("free", None)
    _clear_abort(deps)
    result = await deps.tools.call("next_candidate")
    item = (result or {}).get("item") if isinstance(result, dict) else None
    if not item:
        log.debug("idle — backlog empty")
        return {
            "phase": "idle",
            "mint": None,
            "coin": None,
            "backlog_id": None,
            "round_id": None,
            "route": "stop",
            "last_action": None,
        }
    mint = str(item.get("mint") or "").strip()
    if not mint:
        return {"phase": "idle", "route": "stop", "last_error": "backlog item had no mint"}
    coin = item.get("coin_info") or {}
    if not isinstance(coin, dict):
        coin = {}
    # backlog mcap is from migrate time. price often dies while it waits.
    # one read now, and if it is under the floor the agent never sees it.
    peeked = await deps.tools.call("peek_price", mint=mint)
    live_mcap = None
    if isinstance(peeked, dict) and peeked.get("ok"):
        live_mcap = peeked.get("fdv_usd")
    skip = _handoff_block(live_mcap)
    if skip:
        log.info("SKIP LOW MCAP %s %s", mint, skip)
        await deps.tools.call("reject", mint=mint, reasoning=skip)
        _emit(
            deps,
            "reject",
            mint=mint,
            name=coin.get("name") or "",
            reasoning=skip,
        )
        deps.tools.set_focus("free", None)
        return {
            "phase": "idle",
            "mint": None,
            "coin": None,
            "backlog_id": None,
            "round_id": None,
            "route": "stop",
            "last_action": "reject",
            "last_error": skip,
        }
    # a crashed pass may still have turns. keep them before we wipe the coin.
    _flush_transcript(deps, mint, "", "unfinished")
    deps.memory.begin_focus(mint)
    deps.tools.set_focus("free", mint)
    name = coin.get("name") or coin.get("symbol") or ""
    log.info("FOCUS %s %s", mint, name)
    _emit(
        deps,
        "focus",
        mint=mint,
        name=coin.get("name") or "",
        symbol=coin.get("symbol") or "",
        mcap_usd=(
            float(live_mcap)
            if isinstance(live_mcap, (int, float))
            else coin.get("market_cap_usd")
        ),
        fees_sol=coin.get("estimated_global_fees_sol"),
        fees_check_needed=bool(coin.get("fees_check_needed")),
        buy_sol=_required_sol(deps, {"coin": coin}),
    )
    return {
        "phase": "searching",
        "mint": mint,
        "coin": coin if isinstance(coin, dict) else {},
        "backlog_id": item.get("id"),
        "round_id": None,
        "last_error": None,
        "steps": 0,
        "last_peek_mcap": None,
        "last_peek_at": None,
        "told_phase": None,
        "told_flat": None,
        "told_summary_empty": None,
        "route": "think",
    }


async def _think(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    deps = runtime.context
    mint = str(state.get("mint") or "")
    phase: Phase = state.get("phase") or "searching"  # type: ignore[assignment]
    mode = "locked" if phase == "locked" else "free"
    deps.tools.set_focus(mode, mint)
    # socket starts here, once the agent has the coin. not while it sat in the backlog.
    coin = state.get("coin") or {}
    await _arm_price_watch(deps, mint, coin if isinstance(coin, dict) else {})

    drained = deps.tools.drain_for_step()
    for alert in (drained or {}).get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        if str(alert.get("mint") or mint) != mint:
            continue
        # keep it small. the note is only on the first fast move for this coin.
        payload = {
            "direction": alert.get("direction"),
            "change_pct": alert.get("change_pct"),
            "elapsed_sec": alert.get("elapsed_sec"),
            "from_mcap_usd": alert.get("from_fdv_usd"),
            "to_mcap_usd": alert.get("to_fdv_usd"),
            "note": alert.get("note") or "",
        }
        deps.memory.append_event(mint, "alert", payload)
        _emit(
            deps,
            "price_alert",
            mint=mint,
            name=(state.get("coin") or {}).get("name") or "",
            symbol=(state.get("coin") or {}).get("symbol") or "",
            text=alert_line(payload),
        )

    looks_done = int(state.get("steps") or 0)
    if phase != "locked" and looks_done >= _MAX_SEARCH_LOOKS:
        return _queue_reject(
            deps,
            mint,
            looks_done,
            "looked 3 times without a buy or a reject",
        )

    peek = await deps.tools.call("peek_price", mint=mint)
    window = deps.memory.prompt_window(mint)
    coin = state.get("coin") or {}
    mcap = None
    if isinstance(peek, dict) and peek.get("ok"):
        mcap = peek.get("fdv_usd")
    now = time.time()
    since = since_last_peek(
        state.get("last_peek_mcap"),
        state.get("last_peek_at"),
        float(mcap) if isinstance(mcap, (int, float)) else 0.0,
        now,
    )
    peek_state: dict[str, Any] = {}
    if isinstance(mcap, (int, float)):
        peek_state = {"last_peek_mcap": float(mcap), "last_peek_at": now}
    _emit(
        deps,
        "thinking",
        mint=mint,
        name=coin.get("name") or "",
        symbol=coin.get("symbol") or "",
        mcap_usd=mcap,
        since_last_pct=since.get("since_last_pct"),
        since_last_sec=since.get("since_last_sec"),
    )
    required = _required_sol(deps, state)
    opening = (getattr(window, "opening", None) or "").strip()
    if not opening:
        # first look at this coin. later thinks replay this card as-is.
        opening = _static_card(state, required)
        if mint:
            deps.memory.set_opening(mint, opening)
    messages, seen = _messages(
        deps.prompt,
        state,
        peek,
        window,
        drained,
        buy_sol=required,
        since=since,
        opening=opening,
    )
    steps = int(state.get("steps") or 0) + 1
    if _bag_flat(deps, phase, mint):
        log.info("FLAT %s — sold while preparing the think, writing thesis", mint)
        return {"route": "close"}
    reply = ""
    try:
        chat = deps.chat
        # grammar picks the action. before a buy it also requires both levels.
        if hasattr(chat, "bind"):
            chat = chat.bind(format=decision_format(phase))
        msg, aborted = await _call_model(chat, messages, deps.abort)
        if aborted or _bag_flat(deps, phase, mint):
            log.info("FLAT %s — sold during the think, writing thesis", mint)
            return {"route": "close"}
        reply = _message_text(msg)
        decision = decision_from_message(msg, required_sol=required)
    except (DecisionInvalid, Exception) as exc:  # noqa: BLE001
        raw = exc_text(exc)
        _save_think(deps, mint, messages, reply or None, error=raw)
        # one timeout and we drop the coin. retrying just burns another 2 minutes.
        if phase != "locked" and _timed_out(exc):
            reason = "timed out before a decision"
            log.info("THINK TIMEOUT %s %s — rejecting", mint, raw)
            deps.memory.append_event(
                mint,
                "decision",
                {
                    "action": "reject",
                    "reasoning": reason,
                    "narrative": None,
                    "narrative_updated": False,
                },
            )
            _emit(deps, "error", mint=mint, error=reason)
            decision = ThinkDecision(
                action="reject", reasoning=reason, narrative=""
            )
            return _with_peek(
                {
                    "steps": steps,
                    "pending_decision": decision.model_dump(),
                    "last_error": reason,
                    "route": "apply",
                    **seen,
                },
                peek_state,
            )
        # third look still wasn't a decision. don't ask again.
        if phase != "locked" and steps >= _MAX_SEARCH_LOOKS:
            return _with_peek(
                _queue_reject(
                    deps,
                    mint,
                    steps,
                    "looked 3 times without a buy or a reject",
                ),
                peek_state,
            )
        # bad json — stay on the coin, do not trade
        note = coach_text(raw)
        log.info("THINK FAIL %s %s", mint, raw)
        deps.memory.append_event(mint, "error", {"error": note})
        _emit(deps, "error", mint=mint, error=note)
        return _with_peek(
            {"steps": steps, "last_error": note, "route": "stop", **seen},
            peek_state,
        )

    _save_think(deps, mint, messages, reply)
    # blank narrative means the old read still stands. still log the reply.
    prev = (window.summary or "").strip()
    narrative = decision.narrative.strip()
    narrative_updated = bool(narrative) and narrative != prev
    if narrative_updated:
        deps.memory.set_summary(mint, narrative)
    deps.memory.append_event(
        mint,
        "decision",
        {
            "action": _label_action(decision.action, phase),
            "reasoning": decision.reasoning,
            "narrative": narrative if narrative_updated else None,
            "narrative_updated": narrative_updated,
            "profit_pct": decision.profit_pct,
            "loss_pct": decision.loss_pct,
        },
    )
    log.info("THINK %s phase=%s action=%s", mint, phase, decision.action)
    coin = state.get("coin") or {}
    shown_sol = decision.sol
    if decision.action == "buy":
        sized = _required_sol(deps, state)
        if sized is not None:
            shown_sol = sized
    _emit(
        deps,
        "decision",
        mint=mint,
        name=coin.get("name") or "",
        symbol=coin.get("symbol") or "",
        action=_label_action(decision.action, phase),
        narrative=narrative if narrative_updated else "",
        reasoning=decision.reasoning,
        sol=shown_sol,
        profit_pct=decision.profit_pct,
        loss_pct=decision.loss_pct,
    )
    return _with_peek(
        {
            "steps": steps,
            "pending_decision": decision.model_dump(),
            "last_narrative": narrative if narrative_updated else prev,
            "last_error": None,
            "route": "apply",
            **seen,
        },
        peek_state,
    )


async def _apply(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    deps = runtime.context
    raw = state.get("pending_decision") or {}
    decision = ThinkDecision.model_validate(raw)
    phase = state.get("phase") or "searching"
    mint = str(state.get("mint") or "")
    allowed = _LOCKED_ACTIONS if phase == "locked" else _FREE_ACTIONS
    if decision.action not in allowed:
        err = f"{decision.action} not allowed while {phase}"
        log.info("SKIP %s %s", mint, err)
        return {"last_error": err, "last_action": decision.action, "route": "stop"}

    if (
        decision.action == "hold"
        and phase != "locked"
        and int(state.get("steps") or 0) >= _MAX_SEARCH_LOOKS
    ):
        reason = "looked 3 times without a buy or a reject"
        log.info("SEARCH CAP %s — %s", mint, reason)
        deps.memory.append_event(
            mint,
            "decision",
            {
                "action": "reject",
                "reasoning": reason,
                "narrative": None,
                "narrative_updated": False,
            },
        )
        decision = ThinkDecision(action="reject", reasoning=reason, narrative="")

    if decision.action == "hold":
        # hold can also move the levels, but only once there is a bag
        if phase == "locked" and (
            decision.profit_pct is not None or decision.loss_pct is not None
        ):
            result = await deps.tools.call(
                "set_tpsl", mint=mint, **_level_kwargs(decision)
            )
            if not result.get("ok"):
                return {
                    "last_error": result.get("error") or "tpsl failed",
                    "last_action": "hold",
                    "route": "stop",
                }
            deps.memory.append_event(
                mint,
                "levels",
                {
                    "via": "hold",
                    "profit_pct": decision.profit_pct,
                    "loss_pct": decision.loss_pct,
                },
            )
            log.info(
                "HOLD LEVELS %s tp=%s sl=%s",
                mint,
                decision.profit_pct,
                decision.loss_pct,
            )
        _emit(
            deps,
            "hold",
            mint=mint,
            phase=phase,
            narrative=decision.narrative,
            reasoning=decision.reasoning,
            profit_pct=decision.profit_pct,
            loss_pct=decision.loss_pct,
        )
        return {"last_action": "hold", "route": "stop"}

    if decision.action == "reject":
        result = await deps.tools.call(
            "reject", mint=mint, reasoning=decision.reasoning
        )
        if not result.get("ok"):
            return {
                "last_error": result.get("error") or "reject failed",
                "route": "stop",
            }
        log.info("REJECT %s %s", mint, decision.reasoning)
        coin = state.get("coin") or {}
        _emit(
            deps,
            "reject",
            mint=mint,
            name=coin.get("name") or "",
            reasoning=decision.reasoning,
            narrative=decision.narrative,
        )
        # same close as a sell: thesis, then the next coin.
        return {"last_action": "reject", "route": "close"}

    if decision.action == "buy":
        fields = decision.buy_fields()
        sized = _required_sol(deps, state)
        if sized is not None and sized > 0:
            fields["sol"] = sized
        # the peek at the start of the think can be a minute old. the fill
        # price is the live tick right now, and that is the one we gate on.
        tick = await _live_tick(deps, mint)
        mcap = float(tick.fdv_usd) if tick is not None else None
        low = _buy_mcap_block(mcap)
        if low:
            log.info("BUY BLOCKED %s %s", mint, low)
            deps.memory.append_event(mint, "error", {"error": low})
            coin = state.get("coin") or {}
            _emit(
                deps,
                "error",
                mint=mint,
                name=coin.get("name") or "",
                error=low,
            )
            return {"last_error": low, "last_action": "buy", "route": "stop"}
        result = await deps.tools.call("buy", mint=mint, tick=tick, **fields)
        if not result.get("ok"):
            return {
                "last_error": result.get("error") or "buy failed",
                "last_action": "buy",
                "route": "stop",
            }
        sub = await deps.tools.call("subscribe_move_alerts", mint=mint)
        if not sub.get("ok"):
            log.warning("buy ok but alerts not armed: %s", sub.get("error"))
        deps.tools.set_focus("locked", mint)
        rid = ((result.get("fill") or {}).get("position") or {}).get("round_id")
        log.info(
            "BUY %s sol=%s tp=%s sl=%s",
            mint,
            fields["sol"],
            fields["profit_pct"],
            fields["loss_pct"],
        )
        coin = state.get("coin") or {}
        filled = (result.get("fill") or {}).get("fdv_usd")
        if isinstance(filled, (int, float)):
            mcap = float(filled)
        deps.memory.append_event(
            mint,
            "buy",
            {
                "sol": fields["sol"],
                "buy_mcap_usd": mcap,
                "profit_pct": fields["profit_pct"],
                "loss_pct": fields["loss_pct"],
            },
        )
        _emit(
            deps,
            "buy",
            mint=mint,
            name=coin.get("name") or "",
            sol=fields["sol"],
            mcap_usd=mcap,
            profit_pct=fields["profit_pct"],
            loss_pct=fields["loss_pct"],
        )
        return {
            "phase": "locked",
            "last_action": "buy",
            "round_id": int(rid) if rid is not None else state.get("round_id"),
            "route": "stop",
        }

    if decision.action == "revise_tpsl":
        result = await deps.tools.call("set_tpsl", mint=mint, **_tpsl_kwargs(decision))
        if not result.get("ok"):
            return {
                "last_error": result.get("error") or "tpsl failed",
                "last_action": "revise_tpsl",
                "route": "stop",
            }
        deps.memory.append_event(
            mint,
            "levels",
            {
                "via": "revise_tpsl",
                "profit_pct": decision.profit_pct,
                "loss_pct": decision.loss_pct,
                "reasoning": decision.reasoning,
            },
        )
        log.info("TPSL %s %s", mint, decision.reasoning)
        _emit(
            deps,
            "revise_tpsl",
            mint=mint,
            narrative=decision.narrative,
            reasoning=decision.reasoning,
            profit_pct=decision.profit_pct,
            loss_pct=decision.loss_pct,
        )
        return {"last_action": "revise_tpsl", "route": "stop"}

    # sell
    result = await deps.tools.call("sell", mint=mint, **decision.sell_fields())
    if not result.get("ok"):
        return {
            "last_error": result.get("error") or "sell failed",
            "last_action": "sell",
            "route": "stop",
        }
    flat = (result.get("fill") or {}).get("position") is None
    log.info("SELL %s flat=%s", mint, flat)
    sold_mcap = (result.get("fill") or {}).get("fdv_usd")
    if not isinstance(sold_mcap, (int, float)):
        sold_mcap = state.get("last_peek_mcap")
    deps.memory.append_event(
        mint,
        "sell",
        {
            "flat": flat,
            "sell_mcap_usd": sold_mcap,
            **decision.sell_fields(),
        },
    )
    fill = result.get("fill") or {}
    _emit(
        deps,
        "sell",
        mint=mint,
        flat=flat,
        narrative=decision.narrative,
        sol=fill.get("sol"),
        pnl_sol=fill.get("pnl_sol"),
        pnl_pct=fill.get("pnl_pct"),
    )
    if flat:
        return {"last_action": "sell", "route": "close"}
    return {"last_action": "sell", "route": "stop"}


async def _release(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    """left the coin without a closed trade (reject)."""
    deps = runtime.context
    mint = str(state.get("mint") or "")
    if mint:
        # keep the step log on the reject row, then drop the sliding copy
        await _stop_price_watch(deps, mint)
        coin = state.get("coin") or {}
        _flush_transcript(
            deps,
            mint,
            str(coin.get("symbol") or coin.get("name") or ""),
            "reject",
        )
        deps.memory.save_reject_log(mint)
        deps.memory.drop_focus(mint)
    _clear_abort(deps)
    deps.tools.set_focus("free", None)
    return {
        "phase": "idle",
        "mint": None,
        "coin": None,
        "round_id": None,
        "backlog_id": None,
        "pending_decision": None,
        "last_peek_mcap": None,
        "last_peek_at": None,
        "told_phase": None,
        "told_flat": None,
        "told_summary_empty": None,
        "route": "stop",
    }


async def _close(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    """full history → thesis → unlock. one model call."""
    deps = runtime.context
    mint = str(state.get("mint") or "")
    history = deps.memory.history_for_thesis(mint) if mint else []
    summary = deps.memory.prompt_window(mint).summary if mint else ""
    rid = state.get("round_id")
    rejected = state.get("last_action") == "reject"
    facts = deps.memory.round_row(int(rid)) if rid is not None else None
    thesis = summary
    err = None
    prompt = (
        load_reject_thesis_prompt()
        if rejected
        else (deps.thesis_prompt or load_thesis_prompt())
    )
    thesis_messages = _thesis_messages(
        prompt, state, history, facts, narrative=summary
    )
    reply = ""
    try:
        chat = deps.chat
        if hasattr(chat, "bind"):
            chat = chat.bind(format="json")
        msg = await chat.ainvoke(thesis_messages)
        reply = _message_text(msg)
        thesis = _parse_thesis(reply) or summary
        if mint:
            deps.memory.add_turn(mint, render_thesis(thesis_messages, reply))
    except Exception as exc:  # noqa: BLE001
        err = exc_text(exc)
        log.info("THESIS FAIL %s %s", mint, err)
        thesis = summary or f"thesis failed: {err}"
        if mint:
            deps.memory.add_turn(
                mint, render_thesis(thesis_messages, reply or None, error=err)
            )

    saved_pnl = None
    saved_held = None
    if rejected and mint:
        try:
            deps.memory.save_reject_log(mint)
        except Exception as exc:  # noqa: BLE001
            log.info("REJECT LOG NOT STORED %s %s", mint, exc)
    elif rid is not None and mint:
        try:
            deps.memory.save_round_log(int(rid), mint)
        except Exception as exc:  # noqa: BLE001
            log.info("LOG NOT STORED %s %s", mint, exc)
    if rid is not None and thesis.strip():
        try:
            saved = deps.memory.write_thesis(int(rid), thesis)
            saved_pnl = saved.pnl_sol
            saved_held = saved.time_held_seconds
            log.info(
                "THESIS %s pnl=%s held=%ss | %s",
                mint,
                saved.pnl_sol,
                saved.time_held_seconds,
                thesis.replace("\n", " ")[:400],
            )
        except ValueError as exc:
            err = str(exc)
            log.info("THESIS NOT STORED %s %s", mint, exc)
    coin = state.get("coin") or {}
    thesis_emit: dict[str, Any] = {
        "mint": mint,
        "name": (facts or {}).get("name") or coin.get("name") or "",
        "thesis": thesis,
    }
    if not rejected:
        thesis_emit["pnl_sol"] = (
            saved_pnl if saved_pnl is not None else (facts or {}).get("pnl_sol")
        )
        thesis_emit["held_sec"] = (
            saved_held
            if saved_held is not None
            else (facts or {}).get("time_held_seconds")
        )
    _emit(deps, "thesis", **thesis_emit)
    if mint:
        await _stop_price_watch(deps, mint)
        coin_label = str(
            (facts or {}).get("symbol")
            or coin.get("symbol")
            or coin.get("name")
            or ""
        )
        _flush_transcript(
            deps, mint, coin_label, "reject" if rejected else "closed"
        )
        deps.memory.drop_focus(mint)
    _clear_abort(deps)
    deps.tools.set_focus("free", None)
    return {
        "phase": "idle",
        "mint": None,
        "coin": None,
        "round_id": None,
        "backlog_id": None,
        "pending_decision": None,
        "last_peek_mcap": None,
        "last_peek_at": None,
        "told_phase": None,
        "told_flat": None,
        "told_summary_empty": None,
        "last_action": "thesis",
        "last_narrative": thesis,
        "last_error": err,
        "steps": int(state.get("steps") or 0) + 1,
        "route": "stop",
    }


def _is_open(deps: LoopDeps, mint: str) -> bool:
    if deps.position_open is not None:
        return bool(deps.position_open(mint))
    pos = get_position(deps.memory.ledger_path, mint)
    return pos is not None and pos.tokens > 0


def _level_kwargs(decision: ThinkDecision) -> dict[str, Any]:
    """percents a hold sent. only the ones that are actually set."""
    out: dict[str, Any] = {}
    if decision.profit_pct is not None:
        out["profit_pct"] = decision.profit_pct
    if decision.loss_pct is not None:
        out["loss_pct"] = decision.loss_pct
    return out


def _tpsl_kwargs(decision: ThinkDecision) -> dict[str, Any]:
    """map decision fields onto set_tpsl (clear flag, not a null percent)."""
    fields = decision.tpsl_fields()
    out: dict[str, Any] = {}
    if "profit_pct" in fields:
        if fields["profit_pct"] is None:
            out["clear_profit"] = True
        else:
            out["profit_pct"] = fields["profit_pct"]
    if "loss_pct" in fields:
        if fields["loss_pct"] is None:
            out["clear_loss"] = True
        else:
            out["loss_pct"] = fields["loss_pct"]
    return out


def _event_line(ev: Any) -> str:
    """alerts print the sentence. everything else stays a short json blob."""
    kind = getattr(ev, "kind", "")
    payload = getattr(ev, "payload", None)
    if kind == "alert" and isinstance(payload, dict):
        return f"- alert {alert_line(payload)}"
    blob = _compact(payload) if isinstance(payload, dict) else payload
    return f"- {kind} {json.dumps(blob, default=str)[:300]}"


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """drop nulls. a wait used to echo profit_pct: null back at the model."""
    return {key: val for key, val in payload.items() if val is not None and val != ""}


def _static_card(state: LoopState, buy_sol: float | None) -> str:
    """fields that do not move. saved on the first think and replayed after that."""
    coin = state.get("coin") or {}
    lines = [
        f"mint: {state.get('mint')}",
        f"name: {coin.get('name') or ''}",
        f"symbol: {coin.get('symbol') or ''}",
        f"socials: {coin.get('socials_present') or []}",
    ]
    desc = str(coin.get("description") or "")[:400]
    if desc:
        lines.append(f"description: {desc}")
    if buy_sol is not None:
        lines.append(f"required_buy_sol: {buy_sol}")
    return "\n".join(lines)


def _narrative_visible(events: list[Any], summary: str) -> bool:
    """true when one of the replayed events already says this narrative."""
    for ev in events:
        payload = getattr(ev, "payload", None)
        if isinstance(payload, dict) and payload.get("narrative") == summary:
            return True
    return False


def _delta(
    state: LoopState,
    peek: Any,
    window: Any,
    since: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """
    only what changed since the last think.

    the first card already has mint, name, symbol, fees, socials.
    phase, a flat bag, and an empty summary are said once, then skipped.
    """
    lines: list[str] = []
    phase = state.get("phase")
    told_phase = state.get("told_phase")
    if phase != told_phase:
        lines.append(f"phase: {phase}")

    holding = False
    peek_ok = isinstance(peek, dict) and bool(peek.get("ok"))
    if peek_ok:
        live_mcap = peek.get("fdv_usd")
        if isinstance(live_mcap, (int, float)):
            lines.append(f"mcap_usd: {live_mcap:,.0f}")
        holding = bool(peek.get("holding"))
        if holding:
            lines.append(f"pnl_pct: {peek.get('pnl_pct')}")
            held = peek.get("held_sec")
            if isinstance(held, (int, float)):
                lines.append(f"held_sec: {int(held)}")
    elif isinstance(peek, dict):
        lines.append(f"peek_error: {peek.get('error')}")

    lines.append(since_last_line(since))

    summary = (getattr(window, "summary", None) or "").strip()
    events = list(getattr(window, "events", None) or [])
    told_empty = bool(state.get("told_summary_empty"))
    if summary:
        told_empty = False
        if not _narrative_visible(events, summary):
            lines.append(f"summary: {summary}")
    elif not told_empty:
        lines.append("summary: (none yet)")
        told_empty = True

    lines.append("recent events (oldest first, max 5):")
    if not events:
        lines.append("- (none)")
    for ev in events:
        lines.append(_event_line(ev))
    # alerts were just written into those events. dont print them again.
    seen = {
        "told_phase": phase,
        "told_flat": True if (peek_ok and not holding) or state.get("told_flat") else False,
        "told_summary_empty": told_empty,
    }
    if holding:
        seen["told_flat"] = False
    return "\n".join(lines), seen


def _messages(
    prompt: str,
    state: LoopState,
    peek: Any,
    window: Any,
    _drained: Any,
    buy_sol: float | None = None,
    since: dict[str, Any] | None = None,
    opening: str | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """
    system prompt and the static card are the first think.

    every later call replays those two, then a short user message of what changed
    plus the last 5 events. the system text is not pasted again.
    """
    card = (opening or getattr(window, "opening", None) or "").strip()
    if not card:
        card = _static_card(state, buy_sol)
    delta, seen = _delta(state, peek, window, since)
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": card},
        {"role": "user", "content": delta},
    ], seen


def _thesis_messages(
    prompt: str,
    state: LoopState,
    history: list[Any],
    facts: dict[str, Any] | None,
    narrative: str = "",
) -> list[dict[str, str]]:
    """close prompt. thesis.md already says what to write, so this is just the facts."""
    coin = state.get("coin") or {}
    facts = facts or {}
    # thesis.md already told it to summarize. only the numbers that exist.
    fields = {
        "mint": state.get("mint"),
        "name": facts.get("name") or coin.get("name") or "",
        "symbol": facts.get("symbol") or coin.get("symbol") or "",
        "pnl_sol": facts.get("pnl_sol"),
        "pnl_pct": facts.get("pnl_pct"),
        "held_seconds": facts.get("time_held_seconds"),
        "bought_sol": facts.get("bought_sol"),
        "sold_sol": facts.get("sold_sol"),
        "buy_mcap_usd": _event_mcap(history, "buy", "buy_mcap_usd"),
        "sell_mcap_usd": _event_mcap(history, ("tpsl", "sell"), "sell_mcap_usd"),
        "narrative": (narrative or "").strip() or None,
    }
    lines = [f"{key}: {val}" for key, val in fields.items() if val not in (None, "")]
    if not history:
        lines.append("- (no events)")
    for ev in history:
        payload = ev.payload if isinstance(ev.payload, dict) else {"value": ev.payload}
        lines.append(
            f"- {ev.kind} {json.dumps(_compact(payload), default=str)[:500]}"
        )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _event_mcap(
    history: list[Any], kind: str | tuple[str, ...], key: str
) -> float | None:
    """last market cap stored on a buy or a sell event."""
    kinds = (kind,) if isinstance(kind, str) else kind
    found: float | None = None
    for ev in history:
        if getattr(ev, "kind", None) not in kinds:
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        val = payload.get(key)
        if isinstance(val, (int, float)):
            found = float(val)
    return found


def _mcap_floor() -> float:
    from archetype.config import get_settings

    return float(get_settings().min_buy_mcap_usd)


def _mcap_seen(mcap: Any) -> str:
    if isinstance(mcap, (int, float)):
        return f"${float(mcap):,.0f}"
    return "unknown"


def _handoff_block(mcap: Any) -> str | None:
    """coin never reaches the agent when the live market cap is under the floor."""
    floor = _mcap_floor()
    if isinstance(mcap, (int, float)) and float(mcap) >= floor:
        return None
    return (
        f"market cap is {_mcap_seen(mcap)}, under ${floor:,.0f}. "
        "skipped before the agent saw it."
    )


async def _live_tick(deps: LoopDeps, mint: str) -> Any:
    """latest vault tick. this is the price a buy fills at, not the old peek."""
    alerts = getattr(deps.tools, "alerts", None)
    live = getattr(alerts, "live", None) if alerts is not None else None
    if live is None or not mint:
        return None
    tick = live.latest(mint)
    if tick is not None:
        return tick
    return await live.wait_latest(mint, timeout=5)


def _buy_mcap_block(mcap: Any) -> str | None:
    """refuse the buy when market cap is under the floor. empty means it can go through."""
    floor = _mcap_floor()
    if isinstance(mcap, (int, float)) and float(mcap) >= floor:
        return None
    return (
        f"market cap is {_mcap_seen(mcap)}, under ${floor:,.0f}. too low to buy. "
        "reject this coin, or wait if you think market cap will get back above that."
    )


def _parse_thesis(raw: str) -> str:
    try:
        data = json.loads(_json_blob(raw))
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    text = data.get("thesis") or data.get("narrative") or ""
    return str(text).strip()


def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for block in content:
            if isinstance(block, str):
                bits.append(block)
            elif isinstance(block, dict):
                bits.append(str(block.get("text") or ""))
        return "".join(bits)
    return str(content)


def _json_blob(text: str) -> str:
    """pull the json object out of fences or a short preamble."""
    raw = text.strip()
    if "```" in raw:
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start : end + 1]
    return raw
