"""
turn one model reply into a decision.

searching: reject, hold, buy, metrics.
locked: hold, sell, revise_tpsl, metrics.
the loop runs the real command after. buy size is filled in here, not by the model.
"""

from __future__ import annotations

import json
from typing import Any

from archetype.llm.decision import DecisionInvalid, ThinkDecision, parse_decision


def decision_format(phase: str) -> dict[str, Any]:
    """
    ollama grammar for one decision.

    narrative can be left out. before a buy, the grammar requires both
    percents so a buy cannot come back without them. wait and reject
    still get those numbers stripped afterwards.
    """
    searching = phase != "locked"
    actions = (
        ["hold", "sell", "revise_tpsl", "metrics", "safety", "dex", "wallets"]
        if not searching
        else ["buy", "reject", "wait", "metrics", "safety", "dex", "wallets"]
    )
    required = ["action", "reasoning"]
    # a 3b skips optional keys. force the two levels while it can still buy.
    if searching:
        required.extend(["profit_pct", "loss_pct"])
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "narrative": {"type": "string"},
            "reasoning": {"type": "string"},
            "profit_pct": {"type": "number", "exclusiveMinimum": 0},
            "loss_pct": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 100},
        },
        "required": required,
    }


def decision_from_message(msg: Any, *, required_sol: float | None) -> ThinkDecision:
    """
    turn one model reply into a decision.

    a tool call wins. a json blob is only a fallback.
    """
    calls = list(getattr(msg, "tool_calls", None) or [])
    if calls:
        call = calls[0]
        if isinstance(call, dict):
            name = str(call.get("name") or "")
            args = call.get("args") or {}
        else:
            name = str(getattr(call, "name", "") or "")
            args = getattr(call, "args", None) or {}
        if not isinstance(args, dict):
            raise DecisionInvalid(f"tool {name} args were not an object")
        return _from_choice(name, args, required_sol)

    text = _message_text(msg).strip()
    if not text:
        raise DecisionInvalid("call one tool")
    try:
        data = json.loads(_json_blob(text))
    except json.JSONDecodeError as exc:
        raise DecisionInvalid(f"call one tool (not prose): {exc}") from exc
    if not isinstance(data, dict):
        raise DecisionInvalid("call one tool")
    # old habit: {"action":"buy", ...}
    name = str(data.pop("action", "") or "")
    if not name and len(data) == 1:
        # {"buy": {...}} sometimes
        name = next(iter(data))
        inner = data[name]
        data = inner if isinstance(inner, dict) else {}
    return _from_choice(name, data, required_sol)


def _from_choice(name: str, args: dict[str, Any], required_sol: float | None) -> ThinkDecision:
    payload = dict(args)
    payload["action"] = name
    if payload.get("action") in ("buy", "Buy"):
        if payload.get("sol") in (None, ""):
            if required_sol is None:
                raise DecisionInvalid("buy size is not configured")
            payload["sol"] = required_sol
    return parse_decision(payload)


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
    return str(content or "")


def _json_blob(text: str) -> str:
    raw = text.strip()
    if "```" in raw:
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start : end + 1]
    return raw
