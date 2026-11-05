"""
repair migration frames so coin_mint / quote_mint / pool are authoritative.

pumpportal custom-quote frames often put the quote in `mint`.
helius launchlab / meteora frames already carry the migrate sig — decode it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from archetype.filters.decode import (
    decode_migrate_to_cpswap_accounts,
    decode_migration_damm_v2_accounts,
)
from archetype.filters.helius import HeliusRpc
from archetype.filters.thresholds import PUMP_PROGRAM, WSOL_MINT
from archetype.ingest.models import MigrationEvent, Venue
from archetype.ingest.platform import (
    enrich_launchlab_platform,
    guess_platform_heuristic,
)

log = logging.getLogger("archetype.repair")

# well-known quotes — if payload mint is one of these and we cant decode, reject
KNOWN_QUOTE_MINTS = frozenset(
    {
        WSOL_MINT,
        "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",  # PUMP
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }
)


@dataclass
class RepairOutcome:
    """event after repair attempt + optional hard-reject reason."""

    event: MigrationEvent
    repaired: bool = False
    reject_reason: str | None = None


def decode_migrate_v2_accounts(tx: dict[str, Any]) -> tuple[str, str, str] | None:
    """
    pull (coin_mint, quote_mint, pool) from a pump migrate_v2 ix.

    account layout (pump IDL): 2=base_mint, 3=quote_mint, 10=pool.
    returns None if no matching pump instruction with enough accounts.
    """
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ix in msg.get("instructions") or []:
        if not isinstance(ix, dict):
            continue
        if ix.get("programId") != PUMP_PROGRAM:
            continue
        accts = ix.get("accounts") or []
        if len(accts) < 11:
            continue
        coin = str(accts[2])
        quote = str(accts[3])
        pool = str(accts[10])
        if coin and quote:
            return coin, quote, pool
    return None


def repair_migration_event(event: MigrationEvent, rpc: HeliusRpc) -> RepairOutcome:
    """
    fix coin/quote/pool/platform using the migrate signature (+ launchlab http).

    never invents a coin from the quote alone — no sig / no decode → reject
    when the payload mint looks like a shared quote.
    """
    venue = event.venue or "unknown"

    if venue in ("launchlab", "meteora_dbc"):
        return _repair_non_pump(event, rpc, venue)

    # pump / unknown — try pump migrate_v2 first, then launchlab/meteora
    return _repair_pumpish(event, rpc)


def _repair_pumpish(event: MigrationEvent, rpc: HeliusRpc) -> RepairOutcome:
    """classic pump path; may reclassify if the sig is actually launchlab/meteora."""
    sig = (event.signature or "").strip()
    payload_mint = event.mint

    if not sig:
        if _looks_like_shared_quote(payload_mint):
            return RepairOutcome(
                event=event,
                reject_reason=(
                    "missing migrate signature — cannot recover coin mint "
                    f"(payload mint looks like quote {payload_mint[:8]}…)"
                ),
            )
        _apply_platform_defaults(event)
        return RepairOutcome(event=event)

    try:
        tx = rpc.get_transaction(sig)
    except Exception as exc:  # noqa: BLE001
        log.warning("repair getTransaction failed %s: %s", sig[:16], exc)
        tx = None

    if not tx:
        if _looks_like_shared_quote(payload_mint):
            return RepairOutcome(
                event=event,
                reject_reason=f"migrate tx not found for repair (sig={sig[:16]}…)",
            )
        _apply_platform_defaults(event)
        return RepairOutcome(event=event)

    decoded = decode_migrate_v2_accounts(tx)
    if decoded is not None:
        coin, quote, pool = decoded
        out = _apply_decoded(
            event, coin, quote, pool, venue="pump", payload_mint=payload_mint
        )
        _apply_platform_defaults(out.event)
        return out

    # pumpportal sometimes tags wrong — try other migrate ixs on same sig
    decoded = decode_migrate_to_cpswap_accounts(tx)
    if decoded is not None:
        coin, quote, pool = decoded
        out = _apply_decoded(
            event, coin, quote, pool, venue="launchlab", payload_mint=payload_mint
        )
        _enrich_launchlab(out.event)
        return out

    decoded = decode_migration_damm_v2_accounts(tx)
    if decoded is not None:
        coin, quote, pool = decoded
        out = _apply_decoded(
            event, coin, quote, pool, venue="meteora_dbc", payload_mint=payload_mint
        )
        _enrich_meteora_platform(out.event)
        return out

    if _looks_like_shared_quote(payload_mint):
        return RepairOutcome(
            event=event,
            reject_reason="no migrate accounts in migrate tx",
        )
    _apply_platform_defaults(event)
    return RepairOutcome(event=event)


def _repair_non_pump(
    event: MigrationEvent,
    rpc: HeliusRpc,
    venue: Venue,
) -> RepairOutcome:
    """launchlab / meteora — decode migrate ix, then platform label."""
    sig = (event.signature or "").strip()
    payload_mint = event.mint

    if not sig:
        # may already have real mint from parsed stream — still need pool
        if venue == "launchlab":
            _enrich_launchlab(event)
        else:
            _enrich_meteora_platform(event)
        if not event.pool:
            return RepairOutcome(
                event=event,
                reject_reason="missing migrate signature for non-pump venue",
            )
        return RepairOutcome(event=event)

    try:
        tx = rpc.get_transaction(sig)
    except Exception as exc:  # noqa: BLE001
        log.warning("repair getTransaction failed %s: %s", sig[:16], exc)
        tx = None

    if not tx:
        if venue == "launchlab":
            _enrich_launchlab(event)
        else:
            _enrich_meteora_platform(event)
        return RepairOutcome(event=event)

    decoded: tuple[str, str, str] | None = None
    if venue == "launchlab":
        decoded = decode_migrate_to_cpswap_accounts(tx)
    else:
        decoded = decode_migration_damm_v2_accounts(tx)

    # allow cross-decode if venue guess was wrong
    if decoded is None and venue == "launchlab":
        decoded = decode_migration_damm_v2_accounts(tx)
        if decoded is not None:
            venue = "meteora_dbc"
    if decoded is None and venue == "meteora_dbc":
        decoded = decode_migrate_to_cpswap_accounts(tx)
        if decoded is not None:
            venue = "launchlab"

    if decoded is None:
        if venue == "launchlab":
            _enrich_launchlab(event)
        else:
            _enrich_meteora_platform(event)
        return RepairOutcome(event=event)

    coin, quote, pool = decoded
    out = _apply_decoded(event, coin, quote, pool, venue=venue, payload_mint=payload_mint)
    if out.event.venue == "launchlab":
        _enrich_launchlab(out.event)
    else:
        _enrich_meteora_platform(out.event)
    return out


def _apply_decoded(
    event: MigrationEvent,
    coin: str,
    quote: str,
    pool: str,
    *,
    venue: Venue,
    payload_mint: str,
) -> RepairOutcome:
    """stamp coin/quote/pool/venue onto the event."""
    changed = (
        coin != payload_mint
        or event.quote_mint != quote
        or event.venue != venue
        or (pool and event.pool != pool)
    )
    event.coin_mint = coin
    event.mint = coin
    event.quote_mint = quote
    event.venue = venue
    if pool and (not event.pool or _is_tag_pool(event.pool)):
        event.pool = pool
    if changed:
        log.info(
            "repaired mint %s… → coin=%s… quote=%s… venue=%s pool=%s…",
            payload_mint[:8],
            coin[:8],
            quote[:8],
            venue,
            (pool or "?")[:8],
        )
    return RepairOutcome(event=event, repaired=changed)


def _enrich_launchlab(event: MigrationEvent) -> None:
    """pull platform + migrate amm id from raydium mint api."""
    platform, pool, quote = enrich_launchlab_platform(event.mint)
    if platform and not event.platform:
        event.platform = platform
    if pool and (not event.pool or _is_tag_pool(event.pool)):
        event.pool = pool
    if quote and (not event.quote_mint or event.quote_mint == WSOL_MINT):
        # only overwrite default WSOL when api gives a real quote
        if quote != WSOL_MINT or event.quote_mint == WSOL_MINT:
            event.quote_mint = quote
    if not event.platform:
        event.platform = guess_platform_heuristic(event.mint, "launchlab")


def _enrich_meteora_platform(event: MigrationEvent) -> None:
    """bags etc — no raydium row; heuristic / leave blank → pure meteora."""
    if event.platform:
        return
    event.platform = guess_platform_heuristic(event.mint, "meteora_dbc")


def _apply_platform_defaults(event: MigrationEvent) -> None:
    """pump coins get launchpad=pump unless something else set it."""
    if event.venue == "pump" and not event.platform:
        event.platform = "pump"


def _is_tag_pool(pool: str) -> bool:
    """pumpportal sends tags like pump-amm / raydium-cpmm instead of pubkeys."""
    p = (pool or "").lower()
    if not p:
        return True
    return p in {
        "pump",
        "pump-amm",
        "pumpswap",
        "raydium",
        "raydium-cpmm",
        "cpmm",
        "meteora",
        "meteora-dbc",
        "damm",
        "dbc",
    }


def _looks_like_shared_quote(mint: str) -> bool:
    """true if this mint is a known shared quote (or a common xStock prefix)."""
    if mint in KNOWN_QUOTE_MINTS:
        return True
    # xStocks / tokenized stocks on sol often start with Xs…
    if mint.startswith("Xs") and len(mint) >= 32:
        return True
    return False
