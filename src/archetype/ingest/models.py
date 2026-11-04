"""
normalized migration event from pumpportal (or later other sources).

field notes for later venues (wave B+):
  coin_mint  — the graduated token (authoritative). `mint` aliases this.
  quote_mint — what the pool is paired against (WSOL, PUMP, xStock, …)
  venue      — pump | launchlab | meteora_dbc | unknown
  platform   — optional launchpad label (e.g. StonkFun); unused on classic pump
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# keep ingest free of filters imports — same pubkey as thresholds.WSOL_MINT
_WSOL = "So11111111111111111111111111111111111111112"

Venue = Literal["pump", "launchlab", "meteora_dbc", "unknown"]


class MigrationEvent(BaseModel):
    """one graduation / migration we care about."""

    id: int = 0
    # mint kept as the primary key everywhere; always == coin_mint after repair
    mint: str
    coin_mint: str | None = None
    quote_mint: str | None = None
    venue: Venue = "pump"
    platform: str | None = None
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

    @model_validator(mode="after")
    def _sync_mints(self) -> MigrationEvent:
        # coin_mint defaults to mint; mint always follows coin_mint when set
        if not self.coin_mint:
            self.coin_mint = self.mint
        else:
            self.mint = self.coin_mint
        if not self.quote_mint:
            # classic pump is sol-quoted until repair says otherwise
            self.quote_mint = _WSOL
        return self


def parse_pumpportal_message(payload: Any) -> list[MigrationEvent]:
    """
    turn pumpportal json into MigrationEvent(s).

    they sometimes send a single object, sometimes a list — handle both.
    field names vary a bit so we poke a few common keys.
    note: on custom-quote coins `mint` is often the QUOTE until repair runs.
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
        # pumpportal pool tag → venue guess (repair may refine)
        pool_s = str(pool).lower() if pool else ""
        venue: Venue = "pump"
        if "raydium" in pool_s or "cpmm" in pool_s:
            venue = "launchlab"
        elif "meteora" in pool_s or "damm" in pool_s or "dbc" in pool_s:
            venue = "meteora_dbc"

        out.append(
            MigrationEvent(
                mint=mint,
                coin_mint=mint,
                # leave quote as WSOL until repair; wrong-mint frames get fixed there
                quote_mint=_WSOL,
                venue=venue,
                pool=str(pool) if pool else None,
                signature=str(sig) if sig else None,
                symbol=str(symbol) if symbol else None,
                name=str(name) if name else None,
                raw=item,
            )
        )
    return out
