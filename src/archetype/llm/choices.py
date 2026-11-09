"""
turn one model reply into a decision.

action = trade verbs only. tool = research fetch (optional, alone or with action).
"""

from __future__ import annotations

import json
from typing import Any

from archetype.llm.decision import DecisionInvalid, ThinkDecision, parse_decision
from archetype.llm.tools_catalog import CALLABLE_TOOLS, is_callable_tool, norm_tool_name


def decision_format(
    phase: str,
    *,
    after_vamp: bool = False,
    allowed_tools: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """
    ollama grammar for one decision.

    action is optional (tool-only replies). tool is optional (action-only).
    at least one must be present — enforced in ThinkDecision after parse.

    before a buy, the grammar still requires both percents so a 3b cannot
    omit them; wait/reject/tool-only get those numbers stripped afterwards.
    """
    searching = phase != "locked"
    actions = (
        ["buy", "reject", "wait"]
        if searching
        else ["hold", "sell", "revise_tpsl"]
    )
    if allowed_tools is None:
        tools = list(CALLABLE_TOOLS)
    else:
        tools = [t for t in allowed_tools if t in CALLABLE_TOOLS]
    # empty enum breaks some grammars — keep at least a placeholder the
    # validator will reject if somehow used while everything is suppressed
    if not tools:
        tools = ["metrics"]

    required = ["reasoning"]
    # a 3b skips optional keys. force the two levels while it can still buy.
    if searching:
        required.extend(["profit_pct", "loss_pct"])
    reasoning_schema: dict[str, Any] = {"type": "string"}
    if after_vamp:
        reasoning_schema["minLength"] = 1
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "tool": {"type": "string", "enum": tools},
            "narrative": {"type": "string"},
            "reasoning": reasoning_schema,
            "profit_pct": {"type": "number", "exclusiveMinimum": 0},
            "loss_pct": {
                "type": "number",
                "exclusiveMinimum": 0,
                "exclusiveMaximum": 100,
            },
        },
        "required": required,
    }


def decision_from_message(msg: Any, *, required_sol: float | None) -> ThinkDecision:
    """
    turn one model reply into a decision.

    native tool_calls: if the name is a research tool, map to `tool`;
    otherwise treat the name as `action`.
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
        return _from_payload(name, dict(args), required_sol)

    text = _message_text(msg).strip()
    if not text:
        raise DecisionInvalid("reply with action and/or tool json")
    try:
        data = json.loads(_json_blob(text))
    except json.JSONDecodeError as exc:
        raise DecisionInvalid(f"not json: {exc}") from exc
    if not isinstance(data, dict):
        raise DecisionInvalid("reply with a json object")
    return _from_dict(data, required_sol)


def _from_payload(
    name: str, args: dict[str, Any], required_sol: float | None
) -> ThinkDecision:
    """langchain-style single tool call / action name."""
    payload = dict(args)
    key = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    tool = norm_tool_name(key)
    if is_callable_tool(tool):
        payload.setdefault("tool", tool)
    elif key:
        payload.setdefault("action", name)
    return _from_dict(payload, required_sol)


def _from_dict(data: dict[str, Any], required_sol: float | None) -> ThinkDecision:
    payload = dict(data)
    action = payload.get("action")
    if isinstance(action, str) and action.strip().lower() == "buy":
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
