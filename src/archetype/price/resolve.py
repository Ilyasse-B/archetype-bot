"""
resolve graduated pool + vaults for a mint (pumpswap / cpmm / damm v2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from archetype.filters.decode import (
    PoolInfo,
    parse_mint_json_parsed,
    parse_pool_by_owner,
    venue_for_pool_owner,
)
from archetype.filters.helius import HeliusRpc
from archetype.filters.thresholds import (
    METEORA_DAMM_V2_PROGRAM,
    PUMP_AMM_PROGRAM,
    RAYDIUM_CPMM_PROGRAM,
)
from archetype.price.cache import cache_get, cache_put

log = logging.getLogger("archetype.price")

# owners we accept when scanning holders
_AMM_OWNERS = frozenset(
    {PUMP_AMM_PROGRAM, RAYDIUM_CPMM_PROGRAM, METEORA_DAMM_V2_PROGRAM}
)

_VENUE_OWNER = {
    "pump": PUMP_AMM_PROGRAM,
    "launchlab": RAYDIUM_CPMM_PROGRAM,
    "meteora_dbc": METEORA_DAMM_V2_PROGRAM,
}


@dataclass
class ResolvedPool:
    """pool + mint supply bits we need for mcap."""

    mint: str
    pool: PoolInfo
    decimals: int
    supply_ui: float
    name: str | None = None
    symbol: str | None = None
    venue: str | None = None


def resolve_pool(
    rpc: HeliusRpc,
    mint: str,
    *,
    known_pool: str | None = None,
    venue: str | None = None,
    name: str | None = None,
    symbol: str | None = None,
    ledger_path: Path | None = None,
    use_cache: bool = True,
    skip_das: bool = False,
) -> ResolvedPool:
    """
    find the graduated amm pool for this mint.

    order:
      1) memory/sqlite cache
      2) known_pool account (from migrate/backlog/position)
      3) top holders → ATA owner that is a known amm pool
    """
    if use_cache:
        cached = cache_get(mint, ledger_path=ledger_path)
        if cached is not None:
            patched = False
            if name and not cached.name:
                cached.name = name
                patched = True
            if symbol and not cached.symbol:
                cached.symbol = symbol
                patched = True
            if venue and not getattr(cached, "venue", None):
                cached.venue = venue
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
    if not skip_das and (not out_name or not out_symbol):
        try:
            asset = rpc.get_asset(mint)
            meta = ((asset or {}).get("content") or {}).get("metadata") or {}
            out_name = out_name or meta.get("name")
            out_symbol = out_symbol or meta.get("symbol")
        except Exception as exc:  # noqa: BLE001
            log.debug("das name lookup failed: %s", exc)

    prefer_owner = _VENUE_OWNER.get(venue or "")

    # launchlab: free mint api has migrateAmmId — cpmm vaults arent owned by the pool
    # so holder-scan alone cant find them the way pumpswap can
    if not known_pool and (venue in (None, "launchlab", "unknown")):
        try:
            from archetype.ingest.platform import enrich_launchlab_platform

            _plat, api_pool, _quote = enrich_launchlab_platform(mint)
            if api_pool:
                known_pool = api_pool
                venue = venue or "launchlab"
                prefer_owner = prefer_owner or RAYDIUM_CPMM_PROGRAM
        except Exception as exc:  # noqa: BLE001
            log.debug("launchlab mint api resolve skip: %s", exc)

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
                prefer_owner=prefer_owner,
            )
            if venue:
                resolved.venue = venue
            if use_cache:
                cache_put(resolved, ledger_path=ledger_path)
            return resolved
        except Exception as exc:  # noqa: BLE001
            log.debug("known pool %s failed, falling back: %s", known_pool[:8], exc)

    largest = rpc.get_token_largest_accounts(mint)
    if not largest:
        raise RuntimeError(f"no token accounts found for mint {mint}")

    atas = [e.get("address") for e in largest[:12] if e.get("address")]
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
        # prefer venue owner first, then any known amm
        ordered = list(zip(owners, owner_accts, strict=False))
        if prefer_owner:
            ordered.sort(key=lambda pair: 0 if (pair[1] or {}).get("owner") == prefer_owner else 1)
        for owner, pa in ordered:
            if not pa:
                continue
            prog = pa.get("owner")
            if prog not in _AMM_OWNERS:
                continue
            try:
                pool = parse_pool_by_owner(
                    owner, prog, pa["data"][0], coin_mint=mint
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("pool parse fail %s: %s", owner[:8], exc)
                continue
            if pool.base_mint != mint:
                continue
            resolved = ResolvedPool(
                mint=mint,
                pool=pool,
                decimals=mint_info.decimals,
                supply_ui=mint_info.supply_ui,
                name=out_name,
                symbol=out_symbol,
                venue=venue or venue_for_pool_owner(prog),
            )
            if use_cache:
                cache_put(resolved, ledger_path=ledger_path)
            return resolved

    raise RuntimeError(
        f"no graduated pool found for {mint} "
        "(not on pumpswap / raydium cpmm / meteora damm?)"
    )


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
    """back-compat wrapper — prefer pumpswap when scanning."""
    return resolve_pool(
        rpc,
        mint,
        known_pool=known_pool,
        venue="pump",
        name=name,
        symbol=symbol,
        ledger_path=ledger_path,
        use_cache=use_cache,
        skip_das=skip_das,
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
    prefer_owner: str | None = None,
) -> ResolvedPool:
    """load + validate a known pool address."""
    pa = rpc.get_account_info(pool_addr, encoding="base64")
    if not pa:
        raise RuntimeError(f"pool account missing: {pool_addr}")
    owner = pa.get("owner") or ""
    if owner not in _AMM_OWNERS:
        raise RuntimeError(f"not a known amm pool: {pool_addr} owner={owner}")
    if prefer_owner and owner != prefer_owner:
        # still accept — caller may have stale venue tag
        log.debug(
            "pool owner %s != preferred %s for %s",
            owner[:8],
            prefer_owner[:8],
            mint[:8],
        )
    pool = parse_pool_by_owner(pool_addr, owner, pa["data"][0], coin_mint=mint)
    if pool.base_mint != mint:
        raise RuntimeError("pool base_mint mismatch")
    return ResolvedPool(
        mint=mint,
        pool=pool,
        decimals=decimals,
        supply_ui=supply_ui,
        name=name,
        symbol=symbol,
        venue=venue_for_pool_owner(owner),
    )
