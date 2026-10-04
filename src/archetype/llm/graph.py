"""
lock-loop state machine.

one tick = one transition, then stop. the daemon (later) calls tick again.
no model call when the backlog is empty.
searching: pop one coin, one decision (reject / hold / buy).
locked: one decision (hold / revise_tpsl / sell) from summary + last 5 events.
flat bag: one thesis call over the full history, then unlock.
"""

from __future__ import annotations

import json
import logging
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

log = logging.getLogger("archetype.graph")

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "trader.md"
THESIS_PATH = PROMPT_PATH.with_name("thesis.md")

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
    """close-out instructions. not the trader prompt."""
    return THESIS_PATH.read_text(encoding="utf-8").strip()


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


def _emit(deps: LoopDeps, kind: str, **fields: Any) -> None:
    """push a live line. a bad subscriber must not kill the trade."""
    fn = deps.emit
    if fn is None:
        return
    try:
        fn(kind, **fields)
    except Exception:  # noqa: BLE001
        log.debug("agent feed emit failed", exc_info=True)


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
        {"apply": "apply", "stop": END},
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
    deps.memory.begin_focus(mint)
    deps.tools.set_focus("free", mint)
    coin = item.get("coin_info") or {}
    if not isinstance(coin, dict):
        coin = {}
    name = coin.get("name") or coin.get("symbol") or ""
    log.info("FOCUS %s %s", mint, name)
    _emit(
        deps,
        "focus",
        mint=mint,
        name=coin.get("name") or "",
        symbol=coin.get("symbol") or "",
        mcap_usd=coin.get("market_cap_usd"),
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
        "route": "think",
    }


async def _think(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    deps = runtime.context
    mint = str(state.get("mint") or "")
    phase: Phase = state.get("phase") or "searching"  # type: ignore[assignment]
    mode = "locked" if phase == "locked" else "free"
    deps.tools.set_focus(mode, mint)

    drained = deps.tools.drain_for_step()
    for alert in (drained or {}).get("alerts") or []:
        if isinstance(alert, dict):
            deps.memory.append_event(mint, "alert", alert)

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
    price_sol = fdv_usd = None
    if isinstance(peek, dict) and peek.get("ok"):
        price_sol = peek.get("price_sol")
        fdv_usd = peek.get("fdv_usd")
    _emit(
        deps,
        "thinking",
        mint=mint,
        name=coin.get("name") or "",
        symbol=coin.get("symbol") or "",
        price_sol=price_sol,
        fdv_usd=fdv_usd,
    )
    required = _required_sol(deps, state)
    messages = _messages(
        deps.prompt, state, peek, window, drained, buy_sol=required
    )
    steps = int(state.get("steps") or 0) + 1
    try:
        chat = deps.chat
        # grammar picks the action. percents are optional in it.
        if hasattr(chat, "bind"):
            chat = chat.bind(format=decision_format(phase))
        msg = await chat.ainvoke(messages)
        decision = decision_from_message(msg, required_sol=required)
    except (DecisionInvalid, Exception) as exc:  # noqa: BLE001
        raw = exc_text(exc)
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
            return {
                "steps": steps,
                "pending_decision": decision.model_dump(),
                "last_error": reason,
                "route": "apply",
            }
        # third look still wasn't a decision. don't ask again.
        if phase != "locked" and steps >= _MAX_SEARCH_LOOKS:
            return _queue_reject(
                deps,
                mint,
                steps,
                "looked 3 times without a buy or a reject",
            )
        # bad json — stay on the coin, do not trade
        note = coach_text(raw)
        log.info("THINK FAIL %s %s", mint, raw)
        deps.memory.append_event(mint, "error", {"error": note})
        _emit(deps, "error", mint=mint, error=note)
        return {"steps": steps, "last_error": note, "route": "stop"}

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
    return {
        "steps": steps,
        "pending_decision": decision.model_dump(),
        "last_narrative": narrative if narrative_updated else prev,
        "last_error": None,
        "route": "apply",
    }


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
        return {"last_action": "reject", "route": "release"}

    if decision.action == "buy":
        fields = decision.buy_fields()
        sized = _required_sol(deps, state)
        if sized is not None and sized > 0:
            fields["sol"] = sized
        result = await deps.tools.call("buy", mint=mint, **fields)
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
        deps.memory.append_event(
            mint,
            "buy",
            {
                "sol": fields["sol"],
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
    deps.memory.append_event(mint, "sell", {"flat": flat, **decision.sell_fields()})
    _emit(deps, "sell", mint=mint, flat=flat, narrative=decision.narrative)
    if flat:
        return {"last_action": "sell", "route": "close"}
    return {"last_action": "sell", "route": "stop"}


async def _release(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    """left the coin without a closed trade (reject)."""
    deps = runtime.context
    mint = str(state.get("mint") or "")
    if mint:
        # keep the step log on the reject row, then drop the sliding copy
        deps.memory.save_reject_log(mint)
        deps.memory.drop_focus(mint)
    deps.tools.set_focus("free", None)
    return {
        "phase": "idle",
        "mint": None,
        "coin": None,
        "round_id": None,
        "backlog_id": None,
        "pending_decision": None,
        "route": "stop",
    }


async def _close(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    """full history → thesis → unlock. one model call."""
    deps = runtime.context
    mint = str(state.get("mint") or "")
    history = deps.memory.history_for_thesis(mint) if mint else []
    summary = deps.memory.prompt_window(mint).summary if mint else ""
    rid = state.get("round_id")
    facts = deps.memory.round_row(int(rid)) if rid is not None else None
    thesis = summary
    err = None
    try:
        prompt = deps.thesis_prompt or load_thesis_prompt()
        chat = deps.chat
        if hasattr(chat, "bind"):
            chat = chat.bind(format="json")
        msg = await chat.ainvoke(_thesis_messages(prompt, state, history, facts))
        thesis = _parse_thesis(_message_text(msg)) or summary
    except Exception as exc:  # noqa: BLE001
        err = exc_text(exc)
        log.info("THESIS FAIL %s %s", mint, err)
        thesis = summary or f"thesis failed: {err}"

    saved_pnl = None
    saved_held = None
    if rid is not None and mint:
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
    _emit(
        deps,
        "thesis",
        mint=mint,
        name=(facts or {}).get("name") or coin.get("name") or "",
        thesis=thesis,
        pnl_sol=saved_pnl if saved_pnl is not None else (facts or {}).get("pnl_sol"),
        held_sec=saved_held if saved_held is not None else (facts or {}).get("time_held_seconds"),
    )
    if mint:
        deps.memory.drop_focus(mint)
    deps.tools.set_focus("free", None)
    return {
        "phase": "idle",
        "mint": None,
        "coin": None,
        "round_id": None,
        "backlog_id": None,
        "pending_decision": None,
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


def _messages(
    prompt: str,
    state: LoopState,
    peek: Any,
    window: Any,
    drained: Any,
    buy_sol: float | None = None,
) -> list[dict[str, str]]:
    coin = state.get("coin") or {}
    lines = [
        f"phase: {state.get('phase')}",
        f"mint: {state.get('mint')}",
        f"name: {coin.get('name') or ''}",
        f"symbol: {coin.get('symbol') or ''}",
        f"mcap_usd: {coin.get('market_cap_usd')}",
        f"fees_sol: {coin.get('estimated_global_fees_sol')}",
        f"fees_check_needed: {bool(coin.get('fees_check_needed'))}",
        f"socials: {coin.get('socials_present') or []}",
    ]
    if buy_sol is not None:
        lines.append(f"required_buy_sol: {buy_sol}")
    desc = str(coin.get("description") or "")[:400]
    if desc:
        lines.append(f"description: {desc}")
    if isinstance(peek, dict):
        if peek.get("ok"):
            lines.append(
                f"price_sol: {peek.get('price_sol')} fdv_usd: {peek.get('fdv_usd')} "
                f"pnl_pct: {peek.get('pnl_pct')} owns_coin: {bool(peek.get('holding'))}"
            )
        else:
            lines.append(f"peek_error: {peek.get('error')}")
    lines.append(f"summary: {window.summary or '(none yet)'}")
    lines.append("recent events (oldest first, max 5):")
    if not window.events:
        lines.append("- (none)")
    for ev in window.events:
        lines.append(f"- {ev.kind} {json.dumps(ev.payload, default=str)[:300]}")
    alerts = (drained or {}).get("alerts") if isinstance(drained, dict) else None
    if alerts:
        lines.append("alerts drained this step:")
        for alert in alerts:
            lines.append(f"- {json.dumps(alert, default=str)[:300]}")
    if any(ev.kind == "error" for ev in window.events):
        lines.append(
            "The last reply was cut off. One short sentence about this coin. "
            "Do not repeat the error line."
        )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _thesis_messages(
    prompt: str,
    state: LoopState,
    history: list[Any],
    facts: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """close prompt. the history is the trade, not a generic lesson."""
    coin = state.get("coin") or {}
    facts = facts or {}
    lines = [
        "The position is flat. Summarize this trade from the history below.",
        'Reply with JSON only: {"thesis": "..."}',
        f"mint: {state.get('mint')}",
        f"name: {facts.get('name') or coin.get('name') or ''}",
        f"symbol: {facts.get('symbol') or coin.get('symbol') or ''}",
        f"pnl_sol: {facts.get('pnl_sol')}",
        f"pnl_pct: {facts.get('pnl_pct')}",
        f"held_seconds: {facts.get('time_held_seconds')}",
        f"bought_sol: {facts.get('bought_sol')}",
        f"sold_sol: {facts.get('sold_sol')}",
        "what you did, oldest first:",
    ]
    if not history:
        lines.append("- (no events)")
    for ev in history:
        lines.append(f"- {ev.kind} {json.dumps(ev.payload, default=str)[:500]}")
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]


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
