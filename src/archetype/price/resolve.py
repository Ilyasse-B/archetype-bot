"""
resolve pumpswap pool + vaults for a mint (migrated pump coins).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from archetype.filters.decode import PoolInfo, parse_mint_json_parsed, parse_pool
from archetype.filters.helius import HeliusRpc
from archetype.filters.thresholds import PUMP_AMM_PROGRAM
from archetype.price.cache import cache_get, cache_put

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


def resolve_pumpswap_pool(
    rpc: HeliusRpc,
    mint: str,
    *,
    known_pool: str | None = None,
    name: str | None = None,
    symbol: str | None = None,
    ledger_path: Path | None = None,
    use_cache: bool = True,
    skip_das: bool = False,
) -> ResolvedPool:
    """
    find the pumpswap pool for this mint.

    order:
      1) memory/sqlite cache
      2) known_pool account (from migrate/backlog/position)
      3) top holders → ATA owner that is a Pump AMM pool (any quote)
    """
    if use_cache:
        cached = cache_get(mint, ledger_path=ledger_path)
        if cached is not None:
            # fill missing labels if caller has them (and persist)
            patched = False
            if name and not cached.name:
                cached.name = name
                patched = True
            if symbol and not cached.symbol:
                cached.symbol = symbol
                patched = True
            if patched and ledger_path is not None:
                cache_put(cached, ledger_path=ledger_path)
            return cached

    mint_acct = rpc.get_account_json_parsed(mint)
    if not mint_acct:
        raise RuntimeError(f"mint account missing: {mint}")
    mint_info = parse_mint_json_parsed(mint, mint_acct)

    out_name = name
    out_symbol = symbol
    # DAS only when we still need a label
    if not skip_das and (not out_name or not out_symbol):
        try:
            asset = rpc.get_asset(mint)
            meta = ((asset or {}).get("content") or {}).get("metadata") or {}
            out_name = out_name or meta.get("name")
            out_symbol = out_symbol or meta.get("symbol")
        except Exception as exc:  # noqa: BLE001
            log.debug("das name lookup failed: %s", exc)

    # --- fast path: known pool pubkey ---
    if known_pool:
        try:
            resolved = _from_pool_account(
                rpc,
                mint,
                known_pool,
                decimals=mint_info.decimals,
                supply_ui=mint_info.supply_ui,
                name=out_name,
                symbol=out_symbol,
            )
            if use_cache:
                cache_put(resolved, ledger_path=ledger_path)
            return resolved
        except Exception as exc:  # noqa: BLE001
            log.debug("known pool %s failed, falling back: %s", known_pool[:8], exc)

    # --- holder scan (batched ATA fetches) ---
    largest = rpc.get_token_largest_accounts(mint)
    if not largest:
        raise RuntimeError(f"no token accounts found for mint {mint}")

    atas = [e.get("address") for e in largest[:10] if e.get("address")]
    ata_accts = (
        rpc.get_multiple_accounts(atas, encoding="jsonParsed") if atas else []
    )

    owners: list[str] = []
    seen: set[str] = set()
    for ata, ta in zip(atas, ata_accts, strict=False):
        if not ta:
            continue
        info = ((ta.get("data") or {}).get("parsed") or {}).get("info") or {}
        owner = info.get("owner")
        if not owner or owner in seen:
            continue
        seen.add(owner)
        owners.append(owner)

    if owners:
        owner_accts = rpc.get_multiple_accounts(owners, encoding="base64")
        for owner, pa in zip(owners, owner_accts, strict=False):
            if not pa or pa.get("owner") != PUMP_AMM_PROGRAM:
                continue
            try:
                pool = parse_pool(owner, pa["data"][0])
            except Exception as exc:  # noqa: BLE001
                log.debug("pool parse fail %s: %s", owner[:8], exc)
                continue
            if pool.base_mint != mint:
                continue
            # any quote mint is fine (WSOL, PUMP, xStock, …)
            resolved = ResolvedPool(
                mint=mint,
                pool=pool,
                decimals=mint_info.decimals,
                supply_ui=mint_info.supply_ui,
                name=out_name,
                symbol=out_symbol,
            )
            if use_cache:
                cache_put(resolved, ledger_path=ledger_path)
            return resolved

    raise RuntimeError(
        f"no PumpSwap pool found for {mint} "
        "(is it migrated / still on bonding curve?)"
    )


def _from_pool_account(
    rpc: HeliusRpc,
    mint: str,
    pool_addr: str,
    *,
    decimals: int,
    supply_ui: float,
    name: str | None,
    symbol: str | None,
) -> ResolvedPool:
    """load + validate a known pool address."""
    pa = rpc.get_account_info(pool_addr, encoding="base64")
    if not pa:
        raise RuntimeError(f"pool account missing: {pool_addr}")
    if pa.get("owner") != PUMP_AMM_PROGRAM:
        raise RuntimeError(f"not a Pump AMM pool: {pool_addr}")
    pool = parse_pool(pool_addr, pa["data"][0])
    if pool.base_mint != mint:
        raise RuntimeError("pool base_mint mismatch")
    return ResolvedPool(
        mint=mint,
        pool=pool,
        decimals=decimals,
        supply_ui=supply_ui,
        name=name,
        symbol=symbol,
    )
