"""
resolve pumpswap pool + vaults for a mint (migrated pump coins).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from archetype.filters.decode import PoolInfo, parse_mint_json_parsed, parse_pool
from archetype.filters.helius import HeliusRpc
from archetype.filters.thresholds import PUMP_AMM_PROGRAM, WSOL_MINT

log = logging.getLogger("archetype.price")


@dataclass
class ResolvedPool:
    """pool + mint supply bits we need for mcap."""

    mint: str
    pool: PoolInfo
    decimals: int
    supply_ui: float
    name: str | None = None
    symbol: str | None = None


def resolve_pumpswap_pool(rpc: HeliusRpc, mint: str) -> ResolvedPool:
    """
    find the pumpswap pool for this mint.

    heuristic: top token holders → ATA owner that is a Pump AMM pool account
    with matching base_mint + WSOL quote.
    """
    mint_acct = rpc.get_account_json_parsed(mint)
    if not mint_acct:
        raise RuntimeError(f"mint account missing: {mint}")
    mint_info = parse_mint_json_parsed(mint, mint_acct)

    name: str | None = None
    symbol: str | None = None
    try:
        asset = rpc.get_asset(mint)
        meta = ((asset or {}).get("content") or {}).get("metadata") or {}
        name = meta.get("name")
        symbol = meta.get("symbol")
    except Exception as exc:  # noqa: BLE001
        log.debug("das name lookup failed: %s", exc)

    largest = rpc.get_token_largest_accounts(mint)
    if not largest:
        raise RuntimeError(f"no token accounts found for mint {mint}")

    checked: set[str] = set()
    for entry in largest[:10]:
        ata = entry.get("address")
        if not ata or ata in checked:
            continue
        checked.add(ata)
        ta = rpc.get_account_json_parsed(ata)
        if not ta:
            continue
        info = ((ta.get("data") or {}).get("parsed") or {}).get("info") or {}
        owner = info.get("owner")
        if not owner or owner in checked:
            continue
        checked.add(owner)

        pa = rpc.get_account_info(owner, encoding="base64")
        if not pa or pa.get("owner") != PUMP_AMM_PROGRAM:
            continue
        try:
            pool = parse_pool(owner, pa["data"][0])
        except Exception as exc:  # noqa: BLE001
            log.debug("pool parse fail %s: %s", owner[:8], exc)
            continue
        if pool.base_mint != mint:
            continue
        if pool.quote_mint != WSOL_MINT:
            continue
        return ResolvedPool(
            mint=mint,
            pool=pool,
            decimals=mint_info.decimals,
            supply_ui=mint_info.supply_ui,
            name=name,
            symbol=symbol,
        )

    raise RuntimeError(
        f"no PumpSwap WSOL pool found for {mint} "
        "(is it migrated / still on bonding curve?)"
    )
