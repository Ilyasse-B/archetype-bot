"""
fetch a live price/mcap snapshot from pool vault balances.
"""

from __future__ import annotations

import logging

from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.price.models import PriceTick, build_tick
from archetype.price.resolve import ResolvedPool, resolve_pumpswap_pool

log = logging.getLogger("archetype.price")


def fetch_price_tick(
    rpc: HeliusRpc,
    mint: str,
    *,
    resolved: ResolvedPool | None = None,
    sol_usd: float | None = None,
    source: str = "rpc",
) -> PriceTick:
    """
    one-shot price from current pool vault reserves.

    market_cap_* uses on-chain mint supply.
    fdv_* uses 1B (axiom-style pump display).
    """
    pool_res = resolved or resolve_pumpswap_pool(rpc, mint)
    pool = pool_res.pool

    base_bal = rpc.get_token_account_balance(pool.pool_base_token_account)
    quote_bal = rpc.get_token_account_balance(pool.pool_quote_token_account)
    if not base_bal or not quote_bal:
        raise RuntimeError("pool vault balances not available yet")

    base_ui = float(base_bal.get("uiAmount") or 0)
    quote_sol = int(quote_bal.get("amount") or 0) / 1e9
    usd = float(sol_usd) if sol_usd is not None else float(get_sol_usd(rpc))

    return build_tick(
        mint=mint,
        pool=pool.address,
        base_ui=base_ui,
        quote_sol=quote_sol,
        supply_ui=pool_res.supply_ui,
        decimals=pool_res.decimals,
        sol_usd=usd,
        name=pool_res.name,
        symbol=pool_res.symbol,
        source=source,
    )
