"""
decode mint / bonding curve / amm pool bits from helius data.

pumpswap + raydium cpmm + meteora damm v2 all normalize into PoolInfo
so price/fills can stay venue-agnostic at the vault layer.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey

from archetype.filters.thresholds import (
    LAUNCHLAB_PROGRAM,
    METEORA_DAMM_V2_PROGRAM,
    METEORA_DBC_PROGRAM,
    PUMP_AMM_PROGRAM,
    PUMP_PROGRAM,
    RAYDIUM_CPMM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
)


@dataclass
class MintInfo:
    """parsed mint account."""

    mint: str
    decimals: int
    supply_raw: int
    mint_authority: str | None
    freeze_authority: str | None
    token_program: str
    extensions: list[str]

    @property
    def supply_ui(self) -> float:
        return self.supply_raw / (10 ** self.decimals) if self.decimals >= 0 else 0.0


@dataclass
class BondingCurveInfo:
    """pump bonding curve account."""

    address: str
    complete: bool
    creator: str
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int


@dataclass
class PoolInfo:
    """pumpswap pool account."""

    address: str
    creator: str
    base_mint: str
    quote_mint: str
    lp_mint: str
    pool_base_token_account: str
    pool_quote_token_account: str
    lp_supply: int
    coin_creator: str | None
    index: int


def bonding_curve_pda(mint: str) -> str:
    """derive bonding-curve PDA."""
    curve, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(PUMP_PROGRAM),
    )
    return str(curve)


def canonical_pool_pda(mint: str, pool_creator: str, index: int = 0) -> str:
    """derive canonical pumpswap pool PDA (index usually 0)."""
    pool, _ = Pubkey.find_program_address(
        [
            b"pool",
            struct.pack("<H", index),
            bytes(Pubkey.from_string(pool_creator)),
            bytes(Pubkey.from_string(mint)),
            bytes(Pubkey.from_string(WSOL_MINT)),
        ],
        Pubkey.from_string(PUMP_AMM_PROGRAM),
    )
    return str(pool)


def parse_mint_json_parsed(mint: str, account: dict[str, Any]) -> MintInfo:
    """from getAccountInfo jsonParsed."""
    owner = account.get("owner") or ""
    parsed = (account.get("data") or {}).get("parsed") or {}
    info = parsed.get("info") or {}
    extensions = []
    for ext in info.get("extensions") or []:
        name = ext.get("extension")
        if isinstance(name, str):
            extensions.append(name)
    supply_s = info.get("supply") or "0"
    return MintInfo(
        mint=mint,
        decimals=int(info.get("decimals") or 0),
        supply_raw=int(supply_s),
        mint_authority=info.get("mintAuthority"),
        freeze_authority=info.get("freezeAuthority"),
        token_program=owner,
        extensions=extensions,
    )


def parse_bonding_curve(address: str, data_b64: str) -> BondingCurveInfo:
    """decode bonding curve account bytes."""
    data = base64.b64decode(data_b64)
    if len(data) < 81:
        raise ValueError(f"bonding curve too short: {len(data)}")
    vtr, vsr, rtr, rsr, supply = struct.unpack_from("<5Q", data, 8)
    complete = bool(data[48])
    creator = str(Pubkey.from_bytes(data[49:81]))
    return BondingCurveInfo(
        address=address,
        complete=complete,
        creator=creator,
        virtual_token_reserves=vtr,
        virtual_sol_reserves=vsr,
        real_token_reserves=rtr,
        real_sol_reserves=rsr,
        token_total_supply=supply,
    )


def parse_pool(address: str, data_b64: str) -> PoolInfo:
    """decode pumpswap pool account bytes."""
    data = base64.b64decode(data_b64)
    if len(data) < 211:
        raise ValueError(f"pool account too short: {len(data)}")
    index = struct.unpack_from("<H", data, 9)[0]
    creator = str(Pubkey.from_bytes(data[11:43]))
    base_mint = str(Pubkey.from_bytes(data[43:75]))
    quote_mint = str(Pubkey.from_bytes(data[75:107]))
    lp_mint = str(Pubkey.from_bytes(data[107:139]))
    base_ata = str(Pubkey.from_bytes(data[139:171]))
    quote_ata = str(Pubkey.from_bytes(data[171:203]))
    lp_supply = struct.unpack_from("<Q", data, 203)[0]
    coin_creator = str(Pubkey.from_bytes(data[211:243])) if len(data) >= 243 else None
    return PoolInfo(
        address=address,
        creator=creator,
        base_mint=base_mint,
        quote_mint=quote_mint,
        lp_mint=lp_mint,
        pool_base_token_account=base_ata,
        pool_quote_token_account=quote_ata,
        lp_supply=lp_supply,
        coin_creator=coin_creator,
        index=index,
    )


def parse_cpmm_pool(
    address: str,
    data_b64: str,
    *,
    coin_mint: str | None = None,
) -> PoolInfo:
    """
    raydium cpmm pool → PoolInfo oriented around the graduated coin.

    layout (live pools): vault0@72 vault1@104 mint0@168 mint1@200.
    token0/token1 order is sorted by mint — coin may be either side.
    """
    data = base64.b64decode(data_b64)
    if len(data) < 232:
        raise ValueError(f"cpmm pool too short: {len(data)}")
    vault0 = str(Pubkey.from_bytes(data[72:104]))
    vault1 = str(Pubkey.from_bytes(data[104:136]))
    mint0 = str(Pubkey.from_bytes(data[168:200]))
    mint1 = str(Pubkey.from_bytes(data[200:232]))
    base_mint, quote_mint, base_ata, quote_ata = _orient_pair(
        mint0, mint1, vault0, vault1, coin_mint=coin_mint
    )
    return PoolInfo(
        address=address,
        creator="",
        base_mint=base_mint,
        quote_mint=quote_mint,
        lp_mint="",
        pool_base_token_account=base_ata,
        pool_quote_token_account=quote_ata,
        lp_supply=0,
        coin_creator=None,
        index=0,
    )


def parse_damm_v2_pool(
    address: str,
    data_b64: str,
    *,
    coin_mint: str | None = None,
) -> PoolInfo:
    """
    meteora damm v2 pool → PoolInfo.

    layout (live pools): mint0@168 mint1@200 vault0@232 vault1@264.
    """
    data = base64.b64decode(data_b64)
    if len(data) < 296:
        raise ValueError(f"damm v2 pool too short: {len(data)}")
    mint0 = str(Pubkey.from_bytes(data[168:200]))
    mint1 = str(Pubkey.from_bytes(data[200:232]))
    vault0 = str(Pubkey.from_bytes(data[232:264]))
    vault1 = str(Pubkey.from_bytes(data[264:296]))
    base_mint, quote_mint, base_ata, quote_ata = _orient_pair(
        mint0, mint1, vault0, vault1, coin_mint=coin_mint
    )
    return PoolInfo(
        address=address,
        creator="",
        base_mint=base_mint,
        quote_mint=quote_mint,
        lp_mint="",
        pool_base_token_account=base_ata,
        pool_quote_token_account=quote_ata,
        lp_supply=0,
        coin_creator=None,
        index=0,
    )


def parse_pool_by_owner(
    address: str,
    owner: str,
    data_b64: str,
    *,
    coin_mint: str | None = None,
) -> PoolInfo:
    """dispatch pool parse from account owner program id."""
    if owner == PUMP_AMM_PROGRAM:
        return parse_pool(address, data_b64)
    if owner == RAYDIUM_CPMM_PROGRAM:
        return parse_cpmm_pool(address, data_b64, coin_mint=coin_mint)
    if owner == METEORA_DAMM_V2_PROGRAM:
        return parse_damm_v2_pool(address, data_b64, coin_mint=coin_mint)
    raise ValueError(f"unknown pool owner program: {owner}")


def venue_for_pool_owner(owner: str) -> str | None:
    """map amm program → ingest venue key."""
    if owner == PUMP_AMM_PROGRAM:
        return "pump"
    if owner == RAYDIUM_CPMM_PROGRAM:
        return "launchlab"
    if owner == METEORA_DAMM_V2_PROGRAM:
        return "meteora_dbc"
    return None


def decode_migrate_to_cpswap_accounts(tx: dict[str, Any]) -> tuple[str, str, str] | None:
    """
    launchlab migrate_to_cpswap → (coin_mint, quote_mint, cpmm_pool).

    idl: [1]=base_mint [2]=quote_mint [5]=cpswap_pool (live float migrate matches).
    """
    for accts in _iter_program_ix_accounts(tx, LAUNCHLAB_PROGRAM):
        if len(accts) < 6:
            continue
        coin = str(accts[1])
        quote = str(accts[2])
        pool = str(accts[5])
        # index 4 is the cpswap program id itself — skip those
        if pool == RAYDIUM_CPMM_PROGRAM:
            continue
        if coin and quote and pool:
            return coin, quote, pool
    return None


def decode_migration_damm_v2_accounts(tx: dict[str, Any]) -> tuple[str, str, str] | None:
    """
    meteora migration_damm_v2 → (coin_mint, quote_mint, damm_pool).

    docs / plan: [4]=pool [13]=base_mint [14]=quote_mint.
    """
    for accts in _iter_program_ix_accounts(tx, METEORA_DBC_PROGRAM):
        if len(accts) < 15:
            continue
        pool = str(accts[4])
        coin = str(accts[13])
        quote = str(accts[14])
        if pool in {METEORA_DBC_PROGRAM, METEORA_DAMM_V2_PROGRAM}:
            # wrong slot — try next matching ix
            continue
        if coin and quote and pool:
            return coin, quote, pool
    return None


def _orient_pair(
    mint0: str,
    mint1: str,
    vault0: str,
    vault1: str,
    *,
    coin_mint: str | None,
) -> tuple[str, str, str, str]:
    """pick base=coin side; default token0 as base when unknown."""
    if coin_mint and coin_mint == mint1:
        return mint1, mint0, vault1, vault0
    if coin_mint and coin_mint == mint0:
        return mint0, mint1, vault0, vault1
    # no hint — treat mint0 as base (price still works; just labels swap)
    return mint0, mint1, vault0, vault1


def _iter_program_ix_accounts(tx: dict[str, Any], program_id: str) -> list[list[str]]:
    """top-level + inner instruction account lists for one program."""
    out: list[list[str]] = []
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ix in msg.get("instructions") or []:
        if not isinstance(ix, dict):
            continue
        if ix.get("programId") != program_id:
            continue
        accts = ix.get("accounts") or []
        if accts:
            out.append([str(a) for a in accts])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            if not isinstance(ix, dict):
                continue
            if ix.get("programId") != program_id:
                continue
            accts = ix.get("accounts") or []
            if accts:
                out.append([str(a) for a in accts])
    return out


def extract_pool_from_migrate_tx(
    tx: dict[str, Any],
    mint: str,
    *,
    quote_mint: str = WSOL_MINT,
) -> tuple[str | None, float, float]:
    """
    find pool address + quote ui amount + base ui from migrate tx balances.

    quote_mint defaults to WSOL (classic pump). for custom quotes pass PUMP / MSTRx / …
    returns (pool_address, quote_ui, base_ui) — quote_ui is in quote-token units,
    NOT assumed to be SOL. caller converts to SOL via quote→USD→SOL when needed.
    """
    meta = tx.get("meta") or {}

    quote_by_owner: dict[str, float] = {}
    base_by_owner: dict[str, float] = {}
    for bal in meta.get("postTokenBalances") or []:
        owner = bal.get("owner")
        m = bal.get("mint")
        if not owner:
            continue
        amt = bal.get("uiTokenAmount") or {}
        ui = _ui_amount(amt)
        if ui is None:
            continue
        if m == quote_mint:
            quote_by_owner[owner] = float(ui)
        elif m == mint:
            base_by_owner[owner] = float(ui)

    # pool owner appears in both maps with the biggest quote among shared
    shared = set(quote_by_owner) & set(base_by_owner)
    if not shared:
        # fallback: owner with largest base balance for this mint
        if base_by_owner:
            pool = max(base_by_owner, key=base_by_owner.get)  # type: ignore[arg-type]
            return pool, quote_by_owner.get(pool, 0.0), base_by_owner[pool]
        return None, 0.0, 0.0

    pool = max(shared, key=lambda o: quote_by_owner.get(o, 0.0))
    return pool, quote_by_owner.get(pool, 0.0), base_by_owner.get(pool, 0.0)


def _ui_amount(amt: dict[str, Any]) -> float | None:
    """uiAmount, or raw/decimals if ui missing."""
    ui = amt.get("uiAmount")
    if ui is not None:
        try:
            return float(ui)
        except (TypeError, ValueError):
            pass
    raw = amt.get("amount")
    if raw is None:
        return None
    try:
        dec = int(amt.get("decimals") or 0)
        return int(raw) / (10 ** dec)
    except (TypeError, ValueError):
        return None


def is_known_token_program(program_id: str) -> bool:
    return program_id in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}
