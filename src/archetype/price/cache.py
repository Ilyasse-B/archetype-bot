"""
mint → pumpswap pool cache (memory + sqlite) to cut repeated helius resolves.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from archetype.filters.decode import PoolInfo

if TYPE_CHECKING:
    from archetype.price.resolve import ResolvedPool

# process-local: mint -> (ts, ResolvedPool)
_mem: dict[str, tuple[float, object]] = {}
DEFAULT_TTL_SEC = 6 * 60 * 60  # 6h — pool vaults don't move for a mint


def ensure_pool_cache_table(ledger_path: Path) -> None:
    """idempotent table create."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_cache (
                mint TEXT PRIMARY KEY,
                pool TEXT NOT NULL,
                base_ata TEXT NOT NULL,
                quote_ata TEXT NOT NULL,
                decimals INTEGER NOT NULL,
                supply_ui REAL NOT NULL,
                name TEXT,
                symbol TEXT,
                creator TEXT NOT NULL,
                base_mint TEXT NOT NULL,
                quote_mint TEXT NOT NULL,
                lp_mint TEXT NOT NULL,
                lp_supply INTEGER NOT NULL,
                coin_creator TEXT,
                pool_index INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def cache_get(
    mint: str,
    *,
    ledger_path: Path | None = None,
    ttl_sec: float = DEFAULT_TTL_SEC,
) -> ResolvedPool | None:
    """memory first, then sqlite."""
    from archetype.price.resolve import ResolvedPool

    now = time.time()
    hit = _mem.get(mint)
    if hit is not None:
        ts, resolved = hit
        if now - ts <= ttl_sec:
            return resolved  # type: ignore[return-value]
        _mem.pop(mint, None)

    if ledger_path is None:
        return None
    ensure_pool_cache_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM pool_cache WHERE mint = ?", (mint,)
        ).fetchone()
    if row is None:
        return None
    if now - float(row["updated_at"]) > ttl_sec:
        return None

    pool = PoolInfo(
        address=row["pool"],
        creator=row["creator"],
        base_mint=row["base_mint"],
        quote_mint=row["quote_mint"],
        lp_mint=row["lp_mint"],
        pool_base_token_account=row["base_ata"],
        pool_quote_token_account=row["quote_ata"],
        lp_supply=int(row["lp_supply"]),
        coin_creator=row["coin_creator"],
        index=int(row["pool_index"] or 0),
    )
    resolved = ResolvedPool(
        mint=row["mint"],
        pool=pool,
        decimals=int(row["decimals"]),
        supply_ui=float(row["supply_ui"]),
        name=row["name"],
        symbol=row["symbol"],
    )
    _mem[mint] = (now, resolved)
    return resolved


def cache_put(
    resolved: ResolvedPool,
    *,
    ledger_path: Path | None = None,
) -> None:
    """write memory + optional sqlite."""
    now = time.time()
    _mem[resolved.mint] = (now, resolved)
    if ledger_path is None:
        return
    ensure_pool_cache_table(ledger_path)
    p = resolved.pool
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            """
            INSERT INTO pool_cache (
                mint, pool, base_ata, quote_ata, decimals, supply_ui,
                name, symbol, creator, base_mint, quote_mint, lp_mint,
                lp_supply, coin_creator, pool_index, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                pool=excluded.pool,
                base_ata=excluded.base_ata,
                quote_ata=excluded.quote_ata,
                decimals=excluded.decimals,
                supply_ui=excluded.supply_ui,
                name=excluded.name,
                symbol=excluded.symbol,
                creator=excluded.creator,
                base_mint=excluded.base_mint,
                quote_mint=excluded.quote_mint,
                lp_mint=excluded.lp_mint,
                lp_supply=excluded.lp_supply,
                coin_creator=excluded.coin_creator,
                pool_index=excluded.pool_index,
                updated_at=excluded.updated_at
            """,
            (
                resolved.mint,
                p.address,
                p.pool_base_token_account,
                p.pool_quote_token_account,
                resolved.decimals,
                resolved.supply_ui,
                resolved.name,
                resolved.symbol,
                p.creator,
                p.base_mint,
                p.quote_mint,
                p.lp_mint,
                p.lp_supply,
                p.coin_creator,
                p.index,
                now,
            ),
        )
        conn.commit()


def cache_invalidate(mint: str, *, ledger_path: Path | None = None) -> None:
    """drop one mint from caches."""
    _mem.pop(mint, None)
    if ledger_path is None:
        return
    ensure_pool_cache_table(ledger_path)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("DELETE FROM pool_cache WHERE mint = ?", (mint,))
        conn.commit()
