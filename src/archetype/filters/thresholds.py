"""
filter thresholds — axiom-ish gates + on-chain safety.

tweak via config/default.yaml or ARCHETYPE_FILTER_* env later if needed.
"""

from __future__ import annotations

from dataclasses import dataclass


# extensions that are ok on pump token-2022 mints
SAFE_TOKEN2022_EXTENSIONS = frozenset(
    {
        "metadataPointer",
        "tokenMetadata",
        "mintCloseAuthority",  # often null authority
    }
)

# hard reject if present
DANGEROUS_TOKEN2022_EXTENSIONS = frozenset(
    {
        "transferFeeConfig",
        "transferHook",
        "permanentDelegate",
        "nonTransferable",
        "nonTransferableAccount",
        "defaultAccountState",  # can freeze new accounts
        "pausable",
        "confidentialTransferMint",
    }
)

WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# pump mints are almost always 6 decimals / 1B ui supply (sometimes 2B variants exist)
EXPECTED_DECIMALS = 6
MIN_UI_SUPPLY = 100_000_000  # 100M
MAX_UI_SUPPLY = 10_000_000_000  # 10B


@dataclass(frozen=True)
class FilterThresholds:
    """numeric gates for a single-pass migrate filter."""

    min_market_cap_usd: float = 10_000.0
    min_global_fees_sol: float = 3.0
    min_pool_sol: float = 0.5  # sanity floor on quote vault at migrate
    # creator fee share of total fees (rough) — used to estimate global fees
    # from unclaimed creator vault. axiom "fees" is indexed; this is best-effort.
    creator_fee_share: float = 0.25
    # post-migrate races: fees / pool accounts often land a few seconds late
    fees_poll_sec: float = 7.0
    fees_poll_interval_sec: float = 1.0
    pool_poll_sec: float = 7.0
    pool_poll_interval_sec: float = 0.7


DEFAULT_THRESHOLDS = FilterThresholds()
