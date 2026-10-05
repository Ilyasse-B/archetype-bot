"""
agent tool I/O shapes — stable JSON the LLM / LangGraph will see later.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PeekResult(BaseModel):
    """one-shot price/FDV (+ optional position mark)."""

    ok: bool = True
    mint: str
    name: str | None = None
    symbol: str | None = None
    price_sol: float
    price_usd: float
    fdv_usd: float
    pool_sol: float
    pool: str | None = None
    sol_usd: float
    # position overlay when holding this mint
    holding: bool = False
    value_sol: float | None = None
    pnl_sol: float | None = None
    pnl_pct: float | None = None
    profit_pct: float | None = None
    loss_pct: float | None = None
    source: str = "rpc"


class MoveAlert(BaseModel):
    """fast price move. note is set only the first time for this coin."""

    mint: str
    direction: Literal["up", "down"]
    change_pct: float
    elapsed_sec: float = 0
    from_price_sol: float
    to_price_sol: float
    from_fdv_usd: float
    to_fdv_usd: float
    name: str | None = None
    symbol: str | None = None
    threshold_pct: float
    # long explanation once. later alerts are numbers only.
    note: str = ""
    seq: int
    ts: str


class BacklogPeekItem(BaseModel):
    """one pending candidate (not consumed)."""

    id: int
    mint: str
    timestamp: str
    name: str | None = None
    symbol: str | None = None
    market_cap_usd: float | None = None
    estimated_global_fees_sol: float | None = None
    fees_check_needed: bool = False
    socials_present: list[str] = Field(default_factory=list)
    coin_info: dict[str, Any] = Field(default_factory=dict)

