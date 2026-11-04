"""
repair pumpportal frames where `mint` is actually the quote.

custom-quote graduations often put PUMP / an xStock / USDC in `mint`.
the migrate signature has the real coin (migrate_v2 account 2) and quote (3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from archetype.filters.helius import HeliusRpc
from archetype.filters.thresholds import PUMP_PROGRAM, WSOL_MINT
from archetype.ingest.models import MigrationEvent

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
    fix coin/quote on a pumpportal event using the migrate signature.

    wave A: only decodes pump migrate_v2. launchlab/meteora wait for wave B.
    never invents a coin from the quote alone — no sig / no decode → reject
    when the payload mint looks like a shared quote.
    """
    # non-pump venues: leave alone for now (filters will reject wrong pool owner)
    if event.venue not in ("pump", "unknown"):
        return RepairOutcome(event=event)

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
        return RepairOutcome(event=event)

    decoded = decode_migrate_v2_accounts(tx)
    if decoded is None:
        if _looks_like_shared_quote(payload_mint):
            return RepairOutcome(
                event=event,
                reject_reason="no pump migrate_v2 accounts in migrate tx",
            )
        return RepairOutcome(event=event)

    coin, quote, pool = decoded
    changed = coin != payload_mint or event.quote_mint != quote
    event.coin_mint = coin
    event.mint = coin
    event.quote_mint = quote
    event.venue = "pump"
    # prefer on-chain pool pubkey over pumpportal's "pump-amm" tag
    if pool and (not event.pool or event.pool.lower() in {"pump", "pump-amm", "pumpswap"}):
        event.pool = pool

    if changed:
        log.info(
            "repaired mint %s… → coin=%s… quote=%s… pool=%s…",
            payload_mint[:8],
            coin[:8],
            quote[:8],
            (pool or "?")[:8],
        )
    return RepairOutcome(event=event, repaired=changed)


def _looks_like_shared_quote(mint: str) -> bool:
    """true if this mint is a known shared quote (or a common xStock prefix)."""
    if mint in KNOWN_QUOTE_MINTS:
        return True
    # xStocks / tokenized stocks on sol often start with Xs…
    if mint.startswith("Xs") and len(mint) >= 32:
        return True
    return False
