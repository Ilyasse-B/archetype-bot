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

ThinkAction = Literal["reject", "hold", "buy", "revise_tpsl", "sell"]

# tool is named set_tpsl; plan action is revise_tpsl. same thing.
_ACTION_ALIASES = {
    "set_tpsl": "revise_tpsl",
    "tpsl": "revise_tpsl",
}


class DecisionInvalid(ValueError):
    """model output we will not execute."""


class ThinkDecision(BaseModel):
    """structured output for one search or locked think step."""

    action: ThinkAction
    narrative: str = Field(..., min_length=1, description="short thesis for this step")
    reasoning: str = Field(
        "",
        description="one-line why, required on reject (agent_rejects.reasoning)",
    )
    # buy — same names as AgentBuyRequest
    sol: float | None = Field(None, gt=0, description="SOL to spend")
    profit_pct: float | None = Field(None, gt=0, description="take-profit %%")
    loss_pct: float | None = Field(None, gt=0, description="stop-loss %% magnitude")
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
        if self.action == "reject":
            if not self.reasoning:
                raise ValueError("reject needs reasoning")
        elif self.action == "buy":
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
        return ThinkDecision.model_validate(data)
    except ValidationError as exc:
        raise DecisionInvalid(str(exc)) from exc


def decision_json_schema() -> dict[str, Any]:
    """schema we can hand ollama as the json format later."""
    return ThinkDecision.model_json_schema()
