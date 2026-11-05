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
# raydium launchlab → cpmm
LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
# meteora dynamic bonding curve → damm v2
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
METEORA_DAMM_V2_PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# amm label shown on the agent card (not the venue key)
AMM_BY_VENUE = {
    "pump": "pumpswap",
    "launchlab": "raydium",
    "meteora_dbc": "meteora",
}

# pump mints are almost always 6 decimals / 1B ui supply (sometimes 2B variants exist)
EXPECTED_DECIMALS = 6
# launchlab / meteora sometimes ship 9-dec mints (bags etc)
ALLOWED_DECIMALS_NON_PUMP = frozenset({6, 9})
MIN_UI_SUPPLY = 100_000_000  # 100M
MAX_UI_SUPPLY = 10_000_000_000  # 10B


@dataclass(frozen=True)
class FilterThresholds:
    """numeric gates for a single-pass migrate filter."""

    min_market_cap_usd: float = 10_000.0
    # hard floor — birdeye global_fees_paid must clear this or we reject
    min_global_fees_sol: float = 2.0
    # cleared the hard floor but still under this → PASS + fees_check_needed
    fees_warn_below_sol: float = 2.25
    min_pool_sol: float = 0.5  # sanity floor on quote vault at migrate
    pool_poll_sec: float = 6.0
    pool_poll_interval_sec: float = 0.9


DEFAULT_THRESHOLDS = FilterThresholds()
