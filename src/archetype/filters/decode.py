"""
decode mint / bonding curve / pumpswap pool bits from helius data.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey

from archetype.filters.thresholds import (
    PUMP_AMM_PROGRAM,
    PUMP_PROGRAM,
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


def creator_vault_pda(creator: str) -> str:
    """derive creator fee vault PDA."""
    vault, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(Pubkey.from_string(creator))],
        Pubkey.from_string(PUMP_PROGRAM),
    )
    return str(vault)


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


def extract_pool_from_migrate_tx(tx: dict[str, Any], mint: str) -> tuple[str | None, float, float]:
    """
    find pool address + quote SOL + base ui amount from migrate tx balances.

    returns (pool_address, quote_sol, base_ui).
    """
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys: list[str] = []
    for a in msg.get("accountKeys") or []:
        keys.append(a["pubkey"] if isinstance(a, dict) else str(a))

    quote_by_owner: dict[str, float] = {}
    base_by_owner: dict[str, float] = {}
    for bal in meta.get("postTokenBalances") or []:
        owner = bal.get("owner")
        m = bal.get("mint")
        if not owner:
            continue
        amt = bal.get("uiTokenAmount") or {}
        if m == WSOL_MINT:
            raw = amt.get("amount")
            if raw is not None:
                quote_by_owner[owner] = int(raw) / 1e9
        elif m == mint:
            ui = amt.get("uiAmount")
            if ui is None and amt.get("amount") is not None:
                dec = int(amt.get("decimals") or 0)
                ui = int(amt["amount"]) / (10 ** dec)
            if ui:
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


def is_known_token_program(program_id: str) -> bool:
    return program_id in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}
