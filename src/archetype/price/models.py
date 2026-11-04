"""
live price tick for a pumpswap (migrated pump) pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# axiom-style pump display / FDV supply (most pump mints are 1B)
PUMP_FDV_SUPPLY = 1_000_000_000.0


@dataclass
class PriceTick:
    """one price/mcap snapshot."""

    mint: str
    pool: str
    price_sol: float
    price_usd: float
    # circulating-ish: on-chain mint supply
    market_cap_usd: float
    market_cap_sol: float
    # axiom-style: price * 1B
    fdv_usd: float
    fdv_sol: float
    pool_base_ui: float
    # SOL-equivalent of the quote vault (WSOL ui, or converted)
    pool_sol: float
    supply_ui: float
    fdv_supply: float
    sol_usd: float
    decimals: int
    name: str | None = None
    symbol: str | None = None
    quote_mint: str | None = None
    quote_symbol: str | None = None
    # raw quote vault ui (quote-token units); None when unknown
    pool_quote_ui: float | None = None
    slot: int | None = None
    source: str = "rpc"  # rpc | ws
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "pool": self.pool,
            "name": self.name,
            "symbol": self.symbol,
            "price_sol": self.price_sol,
            "price_usd": self.price_usd,
            "market_cap_usd": self.market_cap_usd,
            "market_cap_sol": self.market_cap_sol,
            "fdv_usd": self.fdv_usd,
            "fdv_sol": self.fdv_sol,
            "pool_base_ui": self.pool_base_ui,
            "pool_sol": self.pool_sol,
            "supply_ui": self.supply_ui,
            "fdv_supply": self.fdv_supply,
            "sol_usd": self.sol_usd,
            "decimals": self.decimals,
            "quote_mint": self.quote_mint,
            "quote_symbol": self.quote_symbol,
            "pool_quote_ui": self.pool_quote_ui,
            "slot": self.slot,
            "source": self.source,
            "at": self.at.isoformat(),
        }


def build_tick(
    *,
    mint: str,
    pool: str,
    base_ui: float,
    quote_sol: float,
    supply_ui: float,
    decimals: int,
    sol_usd: float,
    name: str | None = None,
    symbol: str | None = None,
    slot: int | None = None,
    source: str = "rpc",
    fdv_supply: float = PUMP_FDV_SUPPLY,
    quote_mint: str | None = None,
    quote_symbol: str | None = None,
    pool_quote_ui: float | None = None,
) -> PriceTick:
    """shared math for websocket / snapshot paths.

    quote_sol is the SOL-equivalent of the quote vault (already converted
    when the pair isnt WSOL).
    """
    if base_ui <= 0:
        raise ValueError("pool base reserve is zero")
    price_sol = quote_sol / base_ui
    price_usd = price_sol * sol_usd
    mcap_sol = price_sol * supply_ui
    mcap_usd = mcap_sol * sol_usd
    fdv_sol = price_sol * fdv_supply
    fdv_usd = fdv_sol * sol_usd
    return PriceTick(
        mint=mint,
        pool=pool,
        price_sol=price_sol,
        price_usd=price_usd,
        market_cap_usd=mcap_usd,
        market_cap_sol=mcap_sol,
        fdv_usd=fdv_usd,
        fdv_sol=fdv_sol,
        pool_base_ui=base_ui,
        pool_sol=quote_sol,
        supply_ui=supply_ui,
        fdv_supply=fdv_supply,
        sol_usd=sol_usd,
        decimals=decimals,
        name=name,
        symbol=symbol,
        quote_mint=quote_mint,
        quote_symbol=quote_symbol,
        pool_quote_ui=pool_quote_ui,
        slot=slot,
        source=source,
        at=datetime.now(timezone.utc),
    )
