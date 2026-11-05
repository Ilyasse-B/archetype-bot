"""
resolve launchpad / platform labels for graduated coins.

raydium mint api covers launchlab platforms (float, stonk, higher, …).
bags / pump / pure amm launches get a simple heuristic fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from archetype.ingest.models import Venue

log = logging.getLogger("archetype.platform")

RAYDIUM_MINT_API = "https://launch-mint-v1.raydium.io/get/by/mints"


def amm_for_venue(venue: Venue | str) -> str:
    """agent-facing amm name."""
    from archetype.filters.thresholds import AMM_BY_VENUE

    return AMM_BY_VENUE.get(str(venue), str(venue) or "unknown")


def launchpad_for_event(
    *,
    venue: Venue | str,
    platform: str | None,
    mint: str | None = None,
) -> str:
    """
    what launchpad minted this coin.

    if platform is set (Float, Bags, …) use that.
    otherwise assume a pure venue launch (pump / raydium / meteora).
    """
    if platform and str(platform).strip():
        return str(platform).strip()
    v = str(venue or "")
    if v == "pump":
        return "pump"
    if v == "launchlab":
        # bags-style mint suffix sometimes shows up on raydium too
        if mint and mint.endswith("BAGS"):
            return "Bags"
        return "raydium"
    if v == "meteora_dbc":
        if mint and mint.endswith("BAGS"):
            return "Bags"
        return "meteora"
    return "unknown"


def fetch_launchlab_row(mint: str, *, timeout: float = 12.0) -> dict[str, Any] | None:
    """
    one-shot raydium launch-mint row for this mint, or None.

    free, no key. shape: data.rows[0] with platformInfo / migrateAmmId / mintB.
    """
    mint = (mint or "").strip()
    if not mint:
        return None
    try:
        resp = httpx.get(
            RAYDIUM_MINT_API,
            params={"ids": mint},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("raydium mint api fail %s: %s", mint[:8], exc)
        return None
    rows = ((body or {}).get("data") or {}).get("rows") or []
    if not rows:
        return None
    row = rows[0]
    return row if isinstance(row, dict) else None


def enrich_launchlab_platform(mint: str) -> tuple[str | None, str | None, str | None]:
    """
    (platform_name, migrate_amm_pool, quote_mint) from raydium api.

    any field may be None when the mint isnt on launchlab.
    """
    row = fetch_launchlab_row(mint)
    if not row:
        return None, None, None
    pi = row.get("platformInfo") or {}
    platform = pi.get("name") if isinstance(pi, dict) else None
    pool = row.get("migrateAmmId") or None
    mint_b = row.get("mintB")
    if isinstance(mint_b, dict):
        quote = mint_b.get("address")
    else:
        quote = mint_b if isinstance(mint_b, str) else None
    return (
        str(platform) if platform else None,
        str(pool) if pool else None,
        str(quote) if quote else None,
    )


def guess_platform_heuristic(mint: str, venue: Venue | str) -> str | None:
    """cheap mint-suffix / venue guesses when http enrichment misses."""
    if mint.endswith("BAGS"):
        return "Bags"
    if mint.endswith("pump"):
        return "pump"
    # high suffix used by higher.fun sometimes
    if mint.lower().endswith("high"):
        return "higher.fun"
    return None
