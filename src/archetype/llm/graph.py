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
from archetype.llm.decision import DecisionInvalid, ThinkDecision, parse_decision
from archetype.llm.memory import FocusMemory

log = logging.getLogger("archetype.graph")

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "trader.md"

Phase = Literal["idle", "searching", "locked"]
_FREE_ACTIONS = frozenset({"reject", "hold", "buy"})
_LOCKED_ACTIONS = frozenset({"hold", "sell", "revise_tpsl"})


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


def load_system_prompt() -> str:
    """standing trader instructions (the markdown file)."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


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
    log.info("FOCUS %s %s", mint, coin.get("name") or coin.get("symbol") or "")
    return {
        "phase": "searching",
        "mint": mint,
        "coin": coin if isinstance(coin, dict) else {},
        "backlog_id": item.get("id"),
        "round_id": None,
        "last_error": None,
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

    peek = await deps.tools.call("peek_price", mint=mint)
    window = deps.memory.prompt_window(mint)
    messages = _messages(deps.prompt, state, peek, window, drained)
    steps = int(state.get("steps") or 0) + 1
    try:
        msg = await deps.chat.ainvoke(messages)
        decision = parse_decision(_json_blob(_message_text(msg)))
    except (DecisionInvalid, Exception) as exc:  # noqa: BLE001
        # bad json / timeout — stay on the coin, do not trade
        log.info("THINK FAIL %s %s", mint, exc)
        deps.memory.append_event(mint, "error", {"error": str(exc)})
        return {"steps": steps, "last_error": str(exc), "route": "stop"}

    deps.memory.append_event(mint, "decision", decision.model_dump())
    deps.memory.set_summary(mint, decision.narrative)
    log.info("THINK %s phase=%s action=%s", mint, phase, decision.action)
    return {
        "steps": steps,
        "pending_decision": decision.model_dump(),
        "last_narrative": decision.narrative,
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

    if decision.action == "hold":
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
        return {"last_action": "reject", "route": "release"}

    if decision.action == "buy":
        fields = decision.buy_fields()
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
        log.info("TPSL %s %s", mint, decision.narrative)
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
    if flat:
        return {"last_action": "sell", "route": "close"}
    return {"last_action": "sell", "route": "stop"}


async def _release(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    """left the coin without a closed trade (reject)."""
    deps = runtime.context
    mint = str(state.get("mint") or "")
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
        "route": "stop",
    }


async def _close(state: LoopState, runtime: Runtime[LoopDeps]) -> LoopState:
    """full history → thesis → unlock. one model call."""
    deps = runtime.context
    mint = str(state.get("mint") or "")
    history = deps.memory.history_for_thesis(mint) if mint else []
    summary = deps.memory.prompt_window(mint).summary if mint else ""
    thesis = summary
    err = None
    try:
        msg = await deps.chat.ainvoke(_thesis_messages(deps.prompt, state, history))
        thesis = _parse_thesis(_message_text(msg)) or summary
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        log.info("THESIS FAIL %s %s", mint, exc)
        thesis = summary or f"thesis failed: {exc}"

    rid = state.get("round_id")
    if rid is not None and thesis.strip():
        try:
            saved = deps.memory.write_thesis(int(rid), thesis)
            log.info(
                "THESIS %s pnl=%s held=%ss",
                mint,
                saved.pnl_sol,
                saved.time_held_seconds,
            )
        except ValueError as exc:
            err = str(exc)
            log.info("THESIS NOT STORED %s %s", mint, exc)
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


def _messages(prompt: str, state: LoopState, peek: Any, window: Any, drained: Any) -> list[dict[str, str]]:
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
    desc = str(coin.get("description") or "")[:400]
    if desc:
        lines.append(f"description: {desc}")
    if isinstance(peek, dict):
        if peek.get("ok"):
            lines.append(
                f"price_sol: {peek.get('price_sol')} fdv_usd: {peek.get('fdv_usd')} "
                f"pnl_pct: {peek.get('pnl_pct')} holding: {peek.get('holding')}"
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
    lines.append("Reply with one JSON decision.")
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _thesis_messages(prompt: str, state: LoopState, history: list[Any]) -> list[dict[str, str]]:
    lines = [
        "The position is flat. Write the final thesis for the training log.",
        'Reply with JSON only: {"thesis": "..."}',
        f"mint: {state.get('mint')}",
        "full history:",
    ]
    if not history:
        lines.append("- (no events)")
    for ev in history:
        lines.append(f"- {ev.kind} {json.dumps(ev.payload, default=str)[:400]}")
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
