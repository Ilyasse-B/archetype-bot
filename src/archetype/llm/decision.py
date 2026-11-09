"""
one think-step decision from the local model.

trade verbs live on `action`. research tools live on `tool`.
you can set either, or both. tools are never valid action values.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from archetype.llm.tools_catalog import CALLABLE_TOOLS, is_callable_tool, norm_tool_name

# trade / phase verbs only — never metrics/safety/dex/wallets
ThinkAction = Literal["reject", "hold", "buy", "revise_tpsl", "sell"]

AgentTool = Literal["metrics", "safety", "dex", "wallets"]

# tool is named set_tpsl; plan action is revise_tpsl. same thing.
# wait (searching) is stored as hold; the loop labels it wait before buy.
_ACTION_ALIASES = {
    "set_tpsl": "revise_tpsl",
    "tpsl": "revise_tpsl",
    "wait": "hold",
}


class DecisionInvalid(ValueError):
    """model output we will not execute."""


class ThinkDecision(BaseModel):
    """structured output for one search or locked think step."""

    # optional when the reply is tool-only
    action: ThinkAction | None = None
    # optional research fetch; result lands on the next think
    tool: AgentTool | None = None
    # blank means "keep the narrative we already have"
    narrative: str = ""
    reasoning: str = Field(..., min_length=1, description="why this reply")
    # buy — same names as AgentBuyRequest
    sol: float | None = Field(None, gt=0, description="SOL to spend")
    profit_pct: float | None = Field(None, gt=0, description="take-profit %%")
    loss_pct: float | None = Field(
        None, gt=0, lt=100, description="stop-loss %% magnitude"
    )
    # sell — same names as AgentSellRequest. all empty = full sell
    percent: float | None = Field(None, gt=0, le=100, description="%% of tokens")
    sol_amount: float | None = Field(None, gt=0, description="SOL to sell out")
    initials: bool = Field(False, description="recover the first-buy SOL")
    # revise_tpsl — clear wins over a new pct
    clear_profit: bool = False
    clear_loss: bool = False

    @field_validator("action", mode="before")
    @classmethod
    def _norm_action(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            return value
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not key:
            return None
        # tool names must use the tool field — do not alias them
        if key in CALLABLE_TOOLS or norm_tool_name(key) in CALLABLE_TOOLS:
            raise ValueError(
                f'use tool:"{norm_tool_name(key) or key}" not action:"{key}"'
            )
        return _ACTION_ALIASES.get(key, key)

    @field_validator("tool", mode="before")
    @classmethod
    def _norm_tool(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        name = norm_tool_name(value)
        if name is None:
            return None
        if not is_callable_tool(name):
            raise ValueError(f"unknown tool:{name}")
        return name

    @field_validator("narrative", "reasoning", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _fields_for_action(self) -> ThinkDecision:
        if self.action is None and self.tool is None:
            raise ValueError("set action, tool, or both")
        if self.action == "buy":
            missing = [
                name
                for name, val in (
                    ("sol", self.sol),
                    ("profit_pct", self.profit_pct),
                    ("loss_pct", self.loss_pct),
                )
                if val is None
            ]
            if missing:
                raise ValueError(
                    "buy needs sol, profit_pct, and loss_pct (got missing "
                    + ", ".join(missing)
                    + ")"
                )
        elif self.action == "sell":
            modes = sum(
                [
                    self.percent is not None,
                    self.sol_amount is not None,
                    bool(self.initials),
                ]
            )
            if modes > 1:
                raise ValueError("use only one of percent / sol_amount / initials")
        elif self.action == "revise_tpsl":
            if not (
                self.profit_pct is not None
                or self.loss_pct is not None
                or self.clear_profit
                or self.clear_loss
            ):
                raise ValueError("pass profit_pct / loss_pct or clear flags")
        return self

    def buy_fields(self) -> dict[str, float]:
        """kwargs for agent buy, minus mint/tick (loop fills those)."""
        if self.action != "buy" or self.sol is None:
            raise DecisionInvalid("not a buy")
        assert self.profit_pct is not None and self.loss_pct is not None
        return {
            "sol": self.sol,
            "profit_pct": self.profit_pct,
            "loss_pct": self.loss_pct,
        }

    def sell_fields(self) -> dict[str, Any]:
        """kwargs for agent sell. empty dict means full sell."""
        if self.action != "sell":
            raise DecisionInvalid("not a sell")
        return {
            "percent": self.percent,
            "sol_amount": self.sol_amount,
            "initials": self.initials,
        }

    def tpsl_fields(self) -> dict[str, Any]:
        """kwargs for set_levels. clear_* wins over a new percent."""
        if self.action != "revise_tpsl":
            raise DecisionInvalid("not a tpsl revise")
        out: dict[str, Any] = {}
        if self.clear_profit:
            out["profit_pct"] = None
        elif self.profit_pct is not None:
            out["profit_pct"] = self.profit_pct
        if self.clear_loss:
            out["loss_pct"] = None
        elif self.loss_pct is not None:
            out["loss_pct"] = self.loss_pct
        return out


def _as_num(val: Any) -> float | None:
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _is_echo(text: str) -> bool:
    """the model pastes the last validator line back as its reason."""
    low = text.lower()
    return any(
        bit in low
        for bit in (
            "input should",
            "profit_pct",
            "loss_pct",
            "use profit",
            "readtimeout",
            "field required",
            "validation",
            "last reply",
            "send the json",
            "timed out",
            "name or fees",
            "take-profit",
            "stop-loss",
            'use tool:',
        )
    )


def _coerce_payload(data: dict[str, Any]) -> dict[str, Any]:
    """fix the replies a 3b actually sends before the schema sees them."""
    out = dict(data)
    action = out.get("action")
    if isinstance(action, str):
        action = action.strip().lower().replace("-", "_").replace(" ", "_")
        out["action"] = action or None
    elif action in ("",):
        out["action"] = None
        action = None

    tool_raw = out.get("tool")
    tool = norm_tool_name(tool_raw)
    if tool_raw not in (None, "") and tool is None:
        out["tool"] = tool_raw  # let validator complain
    elif tool is not None:
        out["tool"] = tool
    else:
        out.pop("tool", None)

    # buy size is `sol`. if it only filled the sell field, use that number
    if action == "buy" and out.get("sol") in (None, "") and out.get(
        "sol_amount"
    ) not in (None, ""):
        out["sol"] = out.pop("sol_amount")

    narrative = out.get("narrative")
    reasoning = out.get("reasoning")
    if isinstance(narrative, str) and _is_echo(narrative):
        out["narrative"] = ""
    if isinstance(reasoning, str) and _is_echo(reasoning):
        out["reasoning"] = ""
    if not isinstance(out.get("narrative"), str):
        out["narrative"] = ""

    # searching grammar always asks for percents; strip when not a buy
    # wait still says wait here (alias to hold happens in the model)
    if action in (None, "reject", "sell", "wait") or (
        action is None and out.get("tool")
    ):
        out.pop("profit_pct", None)
        out.pop("loss_pct", None)
    elif action in ("hold", "revise_tpsl"):
        for key in ("profit_pct", "loss_pct"):
            n = _as_num(out.get(key))
            if n is None or n <= 0:
                out.pop(key, None)
    return out


def coach_text(raw: str) -> str:
    """what the next prompt is allowed to see. not the pydantic line."""
    low = raw.lower()
    if "timeout" in low:
        return "timed out. send the json again."
    if "use tool:" in low or (
        "tool:" in low and "action:" in low and "not action" in low
    ):
        return 'use tool:"metrics" (or safety/dex/wallets), not action for tools.'
    if "unknown tool" in low:
        return "tool must be one of: metrics, safety, dex, wallets."
    if "set action, tool" in low or "action, tool, or both" in low:
        return "set action, tool, or both. reasoning is required."
    if "profit" in low or "loss" in low:
        return (
            "that buy had no take-profit or stop-loss. "
            "send the buy again with profit_pct and loss_pct filled in."
        )
    if "unterminated" in low or "invalid json" in low or "not prose" in low:
        return "reply was cut off. reasoning is one short sentence."
    if "vamp" in low and "reasoning" in low:
        return (
            "after a vamp alert every reply needs reasoning: "
            "why you sell, hold, reject, or keep this focus."
        )
    if "reasoning" in low:
        return "every reply needs reasoning: why you took this action or tool call."
    return "reply was not valid json. one short sentence."


def require_post_vamp_reasoning(decision: ThinkDecision, *, after_vamp: bool) -> None:
    """after a vamp alert, refuse an empty / missing why."""
    if not after_vamp:
        return
    why = (decision.reasoning or "").strip()
    if not why:
        raise DecisionInvalid(
            "after a vamp alert every reply needs reasoning: "
            "why you sell, hold, reject, or keep this focus"
        )


def _short_validation(exc: ValidationError) -> str:
    """one line, not the pydantic essay (that got copied into the next reply)."""
    bits: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc") or ())
        msg = str(err.get("msg") or "invalid")
        bits.append(f"{loc}: {msg}" if loc else msg)
    text = "; ".join(bits) if bits else "invalid decision"
    return text[:240]


def parse_decision(raw: str | dict[str, Any]) -> ThinkDecision:
    """
    parse one model reply.

    raises DecisionInvalid on bad json or a decision we must not execute.
    """
    if isinstance(raw, str):
        text = raw.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DecisionInvalid(f"invalid json: {exc}") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise DecisionInvalid("decision must be a json object")
    try:
        return ThinkDecision.model_validate(_coerce_payload(data))
    except ValidationError as exc:
        raise DecisionInvalid(_short_validation(exc)) from exc


def decision_json_schema() -> dict[str, Any]:
    """schema we can hand ollama as the json format later."""
    return ThinkDecision.model_json_schema()
