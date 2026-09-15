"""
normalized migration event from pumpportal (or later other sources).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class MigrationEvent(BaseModel):
    """one pump.fun graduation / migration we care about."""

    id: int = 0
    mint: str
    pool: str | None = None
    signature: str | None = None
    symbol: str | None = None
    name: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = Field(default_factory=dict)
    # filled after deterministic filters (None = not filtered yet)
    filter_passed: bool | None = None
    filter_reasons: list[str] = Field(default_factory=list)
    filter_metrics: dict[str, Any] = Field(default_factory=dict)
    # off-chain metadata snapshot (after uri fetch)
    metadata: dict[str, Any] = Field(default_factory=dict)


def parse_pumpportal_message(payload: Any) -> list[MigrationEvent]:
    """
    turn pumpportal json into MigrationEvent(s).

    they sometimes send a single object, sometimes a list — handle both.
    field names vary a bit so we poke a few common keys.
    """
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        return []

    out: list[MigrationEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mint = (
            item.get("mint")
            or item.get("token")
            or item.get("tokenAddress")
            or item.get("baseMint")
        )
        if not mint or not isinstance(mint, str):
            continue
        pool = item.get("pool") or item.get("poolId") or item.get("market")
        sig = item.get("signature") or item.get("tx")
        symbol = item.get("symbol") or item.get("ticker")
        name = item.get("name")
        out.append(
            MigrationEvent(
                mint=mint,
                pool=str(pool) if pool else None,
                signature=str(sig) if sig else None,
                symbol=str(symbol) if symbol else None,
                name=str(name) if name else None,
                raw=item,
            )
        )
    return out
