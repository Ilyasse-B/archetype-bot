"""
one think-step decision from the local model.

field names match the /agent trade routes:
  buy        → sol, profit_pct, loss_pct
  sell       → percent | sol_amount | initials (none of them = full sell)
  revise_tpsl→ profit_pct, loss_pct, clear_profit, clear_loss
  reject     → reasoning

mint, price, and pool stay on the loop. the model does not get to
pick a different coin or invent a fill price.

bad json, or a buy missing tp+sl, raises DecisionInvalid.
that is a failed step — do not trade.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

ThinkAction = Literal[
    "reject", "hold", "buy", "revise_tpsl", "sell", "metrics", "safety", "dex", "wallets"
]

# tool is named set_tpsl; plan action is revise_tpsl. same thing.
_ACTION_ALIASES = {
    "set_tpsl": "revise_tpsl",
    "tpsl": "revise_tpsl",
    # before a buy, "wait" is the word. the loop still stores it as hold.
    "wait": "hold",
    "onchain": "metrics",
    "rugcheck": "safety",
    "dexscreener": "dex",
    "dex_paid": "dex",
    "tracked": "wallets",
    "tracked_wallets": "wallets",
}


class DecisionInvalid(ValueError):
    """model output we will not execute."""


class ThinkDecision(BaseModel):
    """structured output for one search or locked think step."""

    action: ThinkAction
    # blank means "keep the narrative we already have"
    narrative: str = ""
    reasoning: str = Field(..., min_length=1, description="why this action")
    # buy — same names as AgentBuyRequest
    sol: float | None = Field(None, gt=0, description="SOL to spend")
    profit_pct: float | None = Field(None, gt=0, description="take-profit %%")
    loss_pct: float | None = Field(None, gt=0, lt=100, description="stop-loss %% magnitude")
    # sell — same names as AgentSellRequest. all empty = sell 100%
    percent: float | None = Field(None, gt=0, le=100, description="%% of tokens")
    sol_amount: float | None = Field(None, gt=0, description="SOL to sell out")
    initials: bool = Field(False, description="recover the first-buy SOL")
    # revise_tpsl — same names as AgentTpslRequest. clear wins over a new pct
    clear_profit: bool = False
    clear_loss: bool = False

    @field_validator("action", mode="before")
    @classmethod
    def _norm_action(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        return _ACTION_ALIASES.get(key, key)

    @field_validator("narrative", "reasoning", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _fields_for_action(self) -> ThinkDecision:
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
            # clear flags count even when a pct is also set (clear wins later)
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
        """
        kwargs for set_levels.

        clear_* wins over a new percent, same as POST /agent/tpsl.
        """
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
        )
    )


def _coerce_payload(data: dict[str, Any]) -> dict[str, Any]:
    """fix the replies a 3b actually sends before the schema sees them."""
    out = dict(data)
    action = out.get("action")
    if isinstance(action, str):
        action = action.strip().lower().replace("-", "_").replace(" ", "_")
        out["action"] = action
    # buy size is `sol`. if it only filled the sell field, use that number
    if action == "buy" and out.get("sol") in (None, "") and out.get("sol_amount") not in (None, ""):
        out["sol"] = out.pop("sol_amount")
    narrative = out.get("narrative")
    reasoning = out.get("reasoning")
    if isinstance(narrative, str) and _is_echo(narrative):
        out["narrative"] = ""
    if isinstance(reasoning, str) and _is_echo(reasoning):
        out["reasoning"] = ""
    # narrative and reasoning stay separate. a blank narrative means keep the old one.
    if not isinstance(out.get("narrative"), str):
        out["narrative"] = ""
    # reject and sell do not set levels. a wait can still echo 0s from the
    # schema — drop them so the reply stays a wait.
    # metrics still has to send percents (the search grammar requires them).
    # they are not levels. drop them so a refresh is not a buy.
    if action in (
        "reject",
        "sell",
        "wait",
        "metrics",
        "onchain",
        "safety",
        "rugcheck",
        "dex",
        "dexscreener",
        "dex_paid",
        "wallets",
        "tracked",
        "tracked_wallets",
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
        return "every reply needs reasoning: why you took this action."
    return "reply was not valid json. one short sentence."


def require_post_vamp_reasoning(decision: ThinkDecision, *, after_vamp: bool) -> None:
    """
    after a vamp alert, refuse an empty / missing why.

    the ollama grammar already requires reasoning; this catches echo-stripped
    blanks and anything that slipped the schema.
    """
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
