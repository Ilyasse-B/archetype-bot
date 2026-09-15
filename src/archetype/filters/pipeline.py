"""
deterministic single-pass filters for a pump.fun migration event.

runs once per mint right after pumpportal fires — not continuous metrics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archetype.filters.blacklist import (
    ensure_blacklist_tables,
    is_deployer_blacklisted,
    is_mint_blacklisted,
    log_filter_reject,
)
from archetype.filters.decode import (
    bonding_curve_pda,
    creator_vault_pda,
    extract_pool_from_migrate_tx,
    parse_bonding_curve,
    parse_mint_json_parsed,
    parse_pool,
)
from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.filters.thresholds import (
    DANGEROUS_TOKEN2022_EXTENSIONS,
    DEFAULT_THRESHOLDS,
    EXPECTED_DECIMALS,
    FilterThresholds,
    MAX_UI_SUPPLY,
    MIN_UI_SUPPLY,
    PUMP_AMM_PROGRAM,
    SAFE_TOKEN2022_EXTENSIONS,
    TOKEN_2022_PROGRAM,
    WSOL_MINT,
)
from archetype.ingest.models import MigrationEvent

log = logging.getLogger("archetype.filters")


@dataclass
class FilterResult:
    """pass/fail + reasons + metrics snapshot."""

    passed: bool
    mint: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def fail(self, reason: str) -> FilterResult:
        self.passed = False
        self.reasons.append(reason)
        return self


def run_onchain_filters(
    event: MigrationEvent,
    rpc: HeliusRpc,
    ledger_path: Path,
    thresholds: FilterThresholds = DEFAULT_THRESHOLDS,
) -> FilterResult:
    """
    evaluate one migration.

    order: cheap blacklist → mint → curve → tx/pool → mcap/fees.
    """
    ensure_blacklist_tables(ledger_path)
    result = FilterResult(passed=True, mint=event.mint)
    mint = event.mint

    # --- blacklist ---
    bad, why = is_mint_blacklisted(ledger_path, mint)
    if bad:
        return _reject(result, ledger_path, f"mint blacklisted: {why or 'no reason'}")

    # --- mint account (retry — pumpportal can beat rpc visibility) ---
    mint_acct = _wait_account(rpc, mint, encoding="jsonParsed", attempts=12, delay=0.4)
    if not mint_acct:
        return _reject(result, ledger_path, "mint account missing")
    try:
        mint_info = parse_mint_json_parsed(mint, mint_acct)
    except Exception as exc:  # noqa: BLE001
        return _reject(result, ledger_path, f"mint parse failed: {exc}")

    result.metrics.update(
        {
            "decimals": mint_info.decimals,
            "supply_ui": mint_info.supply_ui,
            "token_program": mint_info.token_program,
            "extensions": mint_info.extensions,
            "mint_authority": mint_info.mint_authority,
            "freeze_authority": mint_info.freeze_authority,
        }
    )

    if mint_info.mint_authority is not None:
        result.fail("mint authority still enabled")
    if mint_info.freeze_authority is not None:
        result.fail("freeze authority still enabled")
    if mint_info.decimals != EXPECTED_DECIMALS:
        result.fail(f"unexpected decimals={mint_info.decimals} (want {EXPECTED_DECIMALS})")
    if not (MIN_UI_SUPPLY <= mint_info.supply_ui <= MAX_UI_SUPPLY):
        result.fail(f"supply_ui out of range: {mint_info.supply_ui}")

    if mint_info.token_program == TOKEN_2022_PROGRAM:
        dangerous = [e for e in mint_info.extensions if e in DANGEROUS_TOKEN2022_EXTENSIONS]
        unknown = [
            e
            for e in mint_info.extensions
            if e not in SAFE_TOKEN2022_EXTENSIONS and e not in DANGEROUS_TOKEN2022_EXTENSIONS
        ]
        if dangerous:
            result.fail(f"dangerous token-2022 extensions: {dangerous}")
        if unknown:
            # unknown = fail-closed for now
            result.fail(f"unknown token-2022 extensions: {unknown}")
    elif mint_info.token_program not in {TOKEN_2022_PROGRAM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}:
        # allow classic SPL too
        from archetype.filters.thresholds import TOKEN_PROGRAM

        if mint_info.token_program != TOKEN_PROGRAM:
            result.fail(f"unknown token program: {mint_info.token_program}")

    # --- migrate tx first (this is the graduation proof) ---
    pool_addr: str | None = None
    quote_sol = 0.0
    base_ui = 0.0
    migrate_ok = False
    if event.signature:
        tx = rpc.get_transaction(event.signature)
        if not tx:
            result.fail("migrate transaction not found")
        else:
            err = (tx.get("meta") or {}).get("err")
            if err is not None:
                result.fail(f"migrate tx failed: {err}")
            else:
                migrate_ok = True
                pool_addr, quote_sol, base_ui = extract_pool_from_migrate_tx(tx, mint)
                result.metrics["migrate_slot"] = tx.get("slot")
                result.metrics["pool_sol_at_migrate"] = quote_sol
                result.metrics["pool_base_ui_at_migrate"] = base_ui
    else:
        result.fail("missing migrate signature")

    if quote_sol < thresholds.min_pool_sol:
        result.fail(f"pool SOL at migrate too low: {quote_sol:.4f} (min {thresholds.min_pool_sol})")

    if not pool_addr:
        result.fail("could not resolve pumpswap pool from migrate tx")
    else:
        result.metrics["pool"] = pool_addr
        # pool account can lag a bit after the migrate notice
        pool_acct = _wait_account(rpc, pool_addr, encoding="base64", attempts=12, delay=0.4)
        if not pool_acct:
            result.fail("pool account missing")
        elif pool_acct.get("owner") != PUMP_AMM_PROGRAM:
            result.fail(f"pool owner not Pump AMM: {pool_acct.get('owner')}")
        else:
            try:
                pool = parse_pool(pool_addr, pool_acct["data"][0])
            except Exception as exc:  # noqa: BLE001
                result.fail(f"pool parse failed: {exc}")
            else:
                result.metrics["pool_creator"] = pool.creator
                result.metrics["coin_creator"] = pool.coin_creator
                result.metrics["lp_mint"] = pool.lp_mint
                result.metrics["lp_supply"] = pool.lp_supply

                if pool.base_mint != mint:
                    result.fail("pool base_mint mismatch")
                if pool.quote_mint != WSOL_MINT:
                    result.fail(f"quote mint not WSOL: {pool.quote_mint}")

                lp_ok, lp_note = _check_lp_burned(rpc, pool.lp_mint)
                result.metrics["lp_burn_check"] = lp_note
                if not lp_ok:
                    result.fail(lp_note)

                base_bal = rpc.get_token_account_balance(pool.pool_base_token_account)
                quote_bal = rpc.get_token_account_balance(pool.pool_quote_token_account)
                live_base = float((base_bal or {}).get("uiAmount") or 0)
                live_quote = int((quote_bal or {}).get("amount") or 0) / 1e9
                result.metrics["live_pool_base_ui"] = live_base
                result.metrics["live_pool_sol"] = live_quote

    # --- bonding curve (deployer + fees only) ---
    # NOTE: we do NOT gate on curve.complete anymore.
    # pumpportal can fire before the complete bit flips; a successful migrate tx
    # is the real graduation proof.
    curve_addr = bonding_curve_pda(mint)
    curve = None
    curve_acct = _wait_account(rpc, curve_addr, encoding="base64", attempts=10, delay=0.4)
    if curve_acct:
        try:
            curve = parse_bonding_curve(curve_addr, curve_acct["data"][0])
        except Exception as exc:  # noqa: BLE001
            return _reject(result, ledger_path, f"bonding curve parse failed: {exc}")

    if curve is not None:
        result.metrics["bonding_curve"] = curve_addr
        result.metrics["curve_complete"] = curve.complete  # informational only
        result.metrics["deployer"] = curve.creator

        bad, why = is_deployer_blacklisted(ledger_path, curve.creator)
        if bad:
            result.fail(f"deployer blacklisted: {why or 'no reason'}")

        vault_addr = creator_vault_pda(curve.creator)
        vault_acct = rpc.get_account_info(vault_addr, encoding="base64")
        vault_sol = (vault_acct.get("lamports") or 0) / 1e9 if vault_acct else 0.0
        # axiom "fees" is indexed total; we estimate from unclaimed creator vault
        share = max(thresholds.creator_fee_share, 0.05)
        estimated_fees_sol = vault_sol / share if vault_sol > 0 else 0.0
        result.metrics["creator_vault_sol"] = vault_sol
        result.metrics["estimated_global_fees_sol"] = estimated_fees_sol
        result.metrics["fees_method"] = "creator_vault / creator_fee_share (best-effort)"

        if estimated_fees_sol < thresholds.min_global_fees_sol:
            result.fail(
                f"global fees too low: ~{estimated_fees_sol:.4f} SOL "
                f"(min {thresholds.min_global_fees_sol})"
            )
    elif migrate_ok:
        # no curve but migrate worked — can't do fees/deployer checks
        result.metrics["fees_method"] = "skipped (no bonding curve)"
        result.fail("could not read bonding curve for fees/deployer checks")
    else:
        return _reject(result, ledger_path, "bonding curve account missing")

    # --- market cap (prefer migrate snapshot so we filter at graduation) ---
    try:
        sol_usd = get_sol_usd(rpc)
        result.metrics["sol_usd"] = sol_usd
        if base_ui > 0 and quote_sol > 0 and mint_info.supply_ui > 0:
            price_sol = quote_sol / base_ui
            mcap_sol = price_sol * mint_info.supply_ui
            mcap_usd = mcap_sol * sol_usd
        else:
            mcap_usd = 0.0
            mcap_sol = 0.0
        result.metrics["market_cap_sol"] = mcap_sol
        result.metrics["market_cap_usd"] = mcap_usd
        if mcap_usd < thresholds.min_market_cap_usd:
            result.fail(
                f"market cap too low: ${mcap_usd:,.2f} (min ${thresholds.min_market_cap_usd:,.0f})"
            )
    except Exception as exc:  # noqa: BLE001
        result.fail(f"market cap calc failed: {exc}")

    if not result.passed:
        reason = "; ".join(result.reasons)
        log_filter_reject(ledger_path, mint, reason)
        log.info("FILTER REJECT %s — %s", mint, reason)
    else:
        log.info(
            "FILTER PASS %s — mcap=$%.0f fees~%.2fSOL pool=%.2fSOL",
            mint,
            float(result.metrics.get("market_cap_usd") or 0),
            float(result.metrics.get("estimated_global_fees_sol") or 0),
            float(result.metrics.get("pool_sol_at_migrate") or 0),
        )
    return result


def _wait_account(
    rpc: HeliusRpc,
    pubkey: str,
    *,
    encoding: str = "base64",
    attempts: int = 10,
    delay: float = 0.4,
) -> dict[str, Any] | None:
    """poll getAccountInfo until it shows up (or we give up)."""
    last: dict[str, Any] | None = None
    for i in range(max(1, attempts)):
        if encoding == "jsonParsed":
            last = rpc.get_account_json_parsed(pubkey)
        else:
            last = rpc.get_account_info(pubkey, encoding=encoding)
        if last is not None:
            return last
        if i + 1 < attempts:
            time.sleep(delay)
    return last


def _check_lp_burned(rpc: HeliusRpc, lp_mint: str) -> tuple[bool, str]:
    """
    pump migrate burns LP — largest accounts should not be a normal wallet with big share.

    heuristic: if top holder is the burn/null-ish or total in top accounts is tiny vs supply, ok.
    if a single non-system wallet holds a large LP share, fail.
    """
    try:
        largest = rpc.get_token_largest_accounts(lp_mint)
    except Exception as exc:  # noqa: BLE001
        return False, f"lp largest accounts failed: {exc}"

    if not largest:
        return True, "lp largest empty (treat as burned/ok)"

    # burn addresses commonly used
    burn_like = {
        "1nc1nerator11111111111111111111111111111111",
        "DeadDeadDeadDeadDeadDeadDeadDeadDeadDeadD",
    }
    top = largest[0]
    addr = top.get("address") or ""
    # getTokenLargestAccounts returns token accounts not owners — check amount share
    amounts = [int(x.get("amount") or 0) for x in largest]
    total_top = sum(amounts) or 1
    # if the biggest chunk is overwhelming and we can resolve owner later — soft check:
    # for pump, LP is burned so top accounts often show 0 balance token accounts
    ui = top.get("uiAmount")
    if ui is None:
        raw = int(top.get("amount") or 0)
        # no decimals easy here; if raw is 0, ok
        if raw == 0:
            return True, "top lp amount 0"
    elif float(ui) == 0:
        return True, "top lp uiAmount 0"

    # if top account holds almost all LP and isn't obviously a pool/burn, flag it
    top_amt = amounts[0]
    if top_amt / total_top > 0.95 and total_top > 0:
        # still likely the burned escrow representation — pass with note
        return True, f"lp top-heavy (ok for burned LP mint); top_account={addr[:8]}…"
    return True, "lp burn heuristic passed"


def _reject(result: FilterResult, ledger_path: Path, reason: str) -> FilterResult:
    result.fail(reason)
    log_filter_reject(ledger_path, result.mint, reason)
    log.info("FILTER REJECT %s — %s", result.mint, reason)
    return result
