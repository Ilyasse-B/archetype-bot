"""
fetch a live price/mcap snapshot from pool vault balances.
"""

from __future__ import annotations

import logging
from pathlib import Path

from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.filters.thresholds import WSOL_MINT
from archetype.price.convert import quote_meta, quote_ui_to_sol, vault_quote_ui
from archetype.price.models import PriceTick, build_tick
from archetype.price.resolve import ResolvedPool, resolve_pool

log = logging.getLogger("archetype.price")


def fetch_price_tick(
    rpc: HeliusRpc,
    mint: str,
    *,
    resolved: ResolvedPool | None = None,
    sol_usd: float | None = None,
    source: str = "rpc",
    known_pool: str | None = None,
    venue: str | None = None,
    name: str | None = None,
    symbol: str | None = None,
    ledger_path: Path | None = None,
) -> PriceTick:
    """
    one-shot price from current pool vault reserves.

    market_cap_* uses on-chain mint supply.
    fdv_* uses 1B (axiom-style pump display).

    for non-WSOL quotes, quote vault is converted to SOL-equivalent so
    price_sol / wallet math stay SOL-denominated.
    """
    pool_res = resolved or resolve_pool(
        rpc,
        mint,
        known_pool=known_pool,
        venue=venue,
        name=name,
        symbol=symbol,
        ledger_path=ledger_path,
        skip_das=bool(name and symbol),
    )
    pool = pool_res.pool

    base_bal, quote_bal = rpc.get_token_balances_pair(
        pool.pool_base_token_account,
        pool.pool_quote_token_account,
    )
    if not base_bal or not quote_bal:
        raise RuntimeError("pool vault balances not available yet")

    base_ui = float(base_bal.get("uiAmount") or 0)
    quote_ui = vault_quote_ui(quote_bal)
    quote_mint = pool.quote_mint or WSOL_MINT
    quote_sol = quote_ui_to_sol(rpc, quote_mint, quote_ui)
    usd = float(sol_usd) if sol_usd is not None else float(get_sol_usd(rpc))

    quote_symbol: str | None = None
    if quote_mint != WSOL_MINT:
        try:
            quote_symbol = quote_meta(rpc, quote_mint).symbol
        except Exception as exc:  # noqa: BLE001
            log.debug("quote symbol lookup failed: %s", exc)

    return build_tick(
        mint=mint,
        pool=pool.address,
        base_ui=base_ui,
        quote_sol=quote_sol,
        supply_ui=pool_res.supply_ui,
        decimals=pool_res.decimals,
        sol_usd=usd,
        name=pool_res.name or name,
        symbol=pool_res.symbol or symbol,
        source=source,
        quote_mint=quote_mint,
        quote_symbol=quote_symbol,
        pool_quote_ui=quote_ui,
    )


def fetch_price_tick_resolved(
    rpc: HeliusRpc,
    mint: str,
    **kwargs,
) -> tuple[PriceTick, ResolvedPool]:
    """same as fetch_price_tick but also returns the ResolvedPool used."""
    resolved: ResolvedPool | None = kwargs.pop("resolved", None)
    ledger_path = kwargs.get("ledger_path")
    known_pool = kwargs.get("known_pool")
    name = kwargs.get("name")
    symbol = kwargs.get("symbol")
    if resolved is None:
        resolved = resolve_pool(
            rpc,
            mint,
            known_pool=known_pool,
            venue=kwargs.get("venue"),
            name=name,
            symbol=symbol,
            ledger_path=ledger_path,
            skip_das=bool(name and symbol),
        )
    tick = fetch_price_tick(rpc, mint, resolved=resolved, **kwargs)
    return tick, resolved
