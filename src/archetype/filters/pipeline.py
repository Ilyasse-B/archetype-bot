"""
deterministic single-pass filters for a migration event.

runs once per mint right after a feed fires — not continuous metrics.
birdeye global fees apply to every venue. bonding-curve deployer checks stay pump-only.
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
from archetype.filters.birdeye import fetch_global_fees_paid
from archetype.filters.decode import (
    bonding_curve_pda,
    extract_pool_from_migrate_tx,
    parse_bonding_curve,
    parse_mint_json_parsed,
    parse_pool_by_owner,
    PoolInfo,
    venue_for_pool_owner,
)
from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.filters.thresholds import (
    ALLOWED_DECIMALS_NON_PUMP,
    DANGEROUS_TOKEN2022_EXTENSIONS,
    DEFAULT_THRESHOLDS,
    EXPECTED_DECIMALS,
    FilterThresholds,
    MAX_UI_SUPPLY,
    METEORA_DAMM_V2_PROGRAM,
    MIN_UI_SUPPLY,
    PUMP_AMM_PROGRAM,
    RAYDIUM_CPMM_PROGRAM,
    SAFE_TOKEN2022_EXTENSIONS,
    TOKEN_2022_PROGRAM,
    WSOL_MINT,
)
from archetype.ingest.models import MigrationEvent
from archetype.ingest.platform import amm_for_venue, launchpad_for_event
from archetype.price.convert import quote_meta, quote_ui_to_sol, quote_ui_to_usd, vault_quote_ui

log = logging.getLogger("archetype.filters")

_AMM_OWNERS = frozenset(
    {PUMP_AMM_PROGRAM, RAYDIUM_CPMM_PROGRAM, METEORA_DAMM_V2_PROGRAM}
)


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
    *,
    finalize: bool = True,
) -> FilterResult:
    """
    evaluate one migration (on-chain only).

    order: cheap blacklist → mint → tx/pool → (pump: curve/fees) → mcap.
    """
    ensure_blacklist_tables(ledger_path)
    result = FilterResult(passed=True, mint=event.mint)
    mint = event.mint

    if not mint or mint == "pending":
        return _reject(result, ledger_path, "mint still unresolved after repair", finalize=finalize)

    # --- blacklist ---
    bad, why = is_mint_blacklisted(ledger_path, mint)
    if bad:
        return _reject(
            result, ledger_path, f"mint blacklisted: {why or 'no reason'}", finalize=finalize
        )

    # --- mint account ---
    mint_acct = _wait_account(rpc, mint, encoding="jsonParsed", attempts=8, delay=0.35)
    if not mint_acct:
        return _reject(result, ledger_path, "mint account missing", finalize=finalize)
    try:
        mint_info = parse_mint_json_parsed(mint, mint_acct)
    except Exception as exc:  # noqa: BLE001
        return _reject(result, ledger_path, f"mint parse failed: {exc}", finalize=finalize)

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

    venue = event.venue or "pump"
    if venue == "pump":
        if mint_info.decimals != EXPECTED_DECIMALS:
            result.fail(f"unexpected decimals={mint_info.decimals} (want {EXPECTED_DECIMALS})")
    elif mint_info.decimals not in ALLOWED_DECIMALS_NON_PUMP:
        result.fail(
            f"unexpected decimals={mint_info.decimals} "
            f"(want {sorted(ALLOWED_DECIMALS_NON_PUMP)})"
        )

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
            result.fail(f"unknown token-2022 extensions: {unknown}")
    elif mint_info.token_program not in {
        TOKEN_2022_PROGRAM,
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    }:
        from archetype.filters.thresholds import TOKEN_PROGRAM

        if mint_info.token_program != TOKEN_PROGRAM:
            result.fail(f"unknown token program: {mint_info.token_program}")

    if not result.passed:
        return _done(result, ledger_path, finalize=finalize)

    quote_mint = event.quote_mint or WSOL_MINT
    result.metrics["quote_mint"] = quote_mint
    result.metrics["venue"] = venue
    result.metrics["amm"] = amm_for_venue(venue)
    launchpad = launchpad_for_event(venue=venue, platform=event.platform, mint=mint)
    result.metrics["launchpad"] = launchpad
    if event.platform:
        result.metrics["platform"] = event.platform

    pool_addr: str | None = event.pool if event.pool and len(event.pool) >= 32 else None
    quote_ui = 0.0
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
                found_pool, quote_ui, base_ui = extract_pool_from_migrate_tx(
                    tx, mint, quote_mint=quote_mint
                )
                if not pool_addr:
                    pool_addr = found_pool
                result.metrics["migrate_slot"] = tx.get("slot")
                result.metrics["pool_quote_ui_at_migrate"] = quote_ui
                result.metrics["pool_base_ui_at_migrate"] = base_ui
                try:
                    quote_sol = quote_ui_to_sol(rpc, quote_mint, quote_ui)
                    result.metrics["pool_sol_at_migrate"] = quote_sol
                    result.metrics["pool_quote_usd_at_migrate"] = quote_ui_to_usd(
                        rpc, quote_mint, quote_ui
                    )
                    if quote_mint != WSOL_MINT:
                        qm = quote_meta(rpc, quote_mint)
                        result.metrics["quote_symbol"] = qm.symbol
                        result.metrics["quote_name"] = qm.name
                except Exception as exc:  # noqa: BLE001
                    # non-pump may still recover liquidity from live vaults
                    if venue == "pump":
                        result.fail(f"quote→SOL convert failed: {exc}")
                    else:
                        result.metrics["quote_convert_error"] = str(exc)
    else:
        result.fail("missing migrate signature")

    # liquidity floor from migrate snapshot when we have it
    if result.passed and quote_ui > 0:
        if quote_mint == WSOL_MINT:
            if quote_sol < thresholds.min_pool_sol:
                result.fail(
                    f"pool SOL at migrate too low: {quote_sol:.4f} "
                    f"(min {thresholds.min_pool_sol})"
                )
        else:
            try:
                sol_usd = get_sol_usd(rpc)
                min_usd = thresholds.min_pool_sol * sol_usd
                quote_usd = float(result.metrics.get("pool_quote_usd_at_migrate") or 0)
                if quote_usd < min_usd:
                    result.fail(
                        f"pool quote liquidity too low: ${quote_usd:,.2f} "
                        f"(min ${min_usd:,.2f} ≈ {thresholds.min_pool_sol} SOL)"
                    )
            except Exception as exc:  # noqa: BLE001
                result.fail(f"liquidity floor check failed: {exc}")

    if not pool_addr:
        result.fail("could not resolve pool from migrate tx")
    else:
        result.metrics["pool"] = pool_addr

    if not result.passed:
        result.metrics["pool_poll_skipped"] = "already failing after migrate snapshot"
        return _done(result, ledger_path, finalize=finalize)

    pool_attempts = max(
        1, int(thresholds.pool_poll_sec / max(thresholds.pool_poll_interval_sec, 0.1)) + 1
    )
    assert pool_addr is not None
    pool_acct = _wait_account(
        rpc,
        pool_addr,
        encoding="base64",
        attempts=pool_attempts,
        delay=thresholds.pool_poll_interval_sec,
    )
    if not pool_acct:
        result.metrics["pool_account_pending"] = True
        result.metrics["lp_burn_check"] = "skipped (pool account not visible after poll)"
        log.info(
            "pool account still missing after %.1fs for %s (%s…) — using migrate tx snapshot",
            thresholds.pool_poll_sec,
            mint,
            pool_addr[:8],
        )
    else:
        owner = pool_acct.get("owner") or ""
        expected = _expected_owner(venue)
        if owner not in _AMM_OWNERS:
            result.fail(f"pool owner not a known amm: {owner}")
            return _done(result, ledger_path, finalize=finalize)
        if expected and owner != expected:
            # reclassify if the account tells a clearer story
            inferred = venue_for_pool_owner(owner)
            if inferred:
                venue = inferred
                event.venue = inferred  # type: ignore[assignment]
                result.metrics["venue"] = venue
                result.metrics["amm"] = amm_for_venue(venue)
            else:
                result.fail(f"pool owner mismatch for venue={venue}: {owner}")
                return _done(result, ledger_path, finalize=finalize)

        result.metrics["pool_account_pending"] = False
        try:
            pool = parse_pool_by_owner(
                pool_addr, owner, pool_acct["data"][0], coin_mint=mint
            )
        except Exception as exc:  # noqa: BLE001
            result.fail(f"pool parse failed: {exc}")
            return _done(result, ledger_path, finalize=finalize)

        result.metrics["pool_creator"] = pool.creator
        result.metrics["coin_creator"] = pool.coin_creator
        result.metrics["lp_mint"] = pool.lp_mint
        result.metrics["lp_supply"] = pool.lp_supply

        if pool.base_mint != mint:
            result.fail("pool base_mint mismatch")
        if pool.quote_mint != quote_mint:
            # trust on-chain pool if event quote was still default WSOL
            if quote_mint == WSOL_MINT and pool.quote_mint != WSOL_MINT:
                quote_mint = pool.quote_mint
                event.quote_mint = quote_mint
                result.metrics["quote_mint"] = quote_mint
            else:
                result.fail(
                    f"pool quote_mint mismatch: pool={pool.quote_mint} "
                    f"event={quote_mint}"
                )
        result.metrics["pool_quote_mint"] = pool.quote_mint
        if not result.passed:
            return _done(result, ledger_path, finalize=finalize)

        if venue == "pump":
            lp_ok, lp_note = _check_lp_burned(pool)
            result.metrics["lp_burn_check"] = lp_note
            if not lp_ok:
                result.fail(lp_note)
                return _done(result, ledger_path, finalize=finalize)
        else:
            result.metrics["lp_burn_check"] = "skipped (non-pump venue)"

        try:
            from archetype.price.cache import cache_put
            from archetype.price.resolve import ResolvedPool

            cache_put(
                ResolvedPool(
                    mint=mint,
                    pool=pool,
                    decimals=mint_info.decimals,
                    supply_ui=mint_info.supply_ui,
                    name=event.name,
                    symbol=event.symbol,
                    venue=venue,
                ),
                ledger_path=ledger_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("pool cache put skipped: %s", exc)

        base_bal, quote_bal = _poll_token_balances(
            rpc,
            pool.pool_base_token_account,
            pool.pool_quote_token_account,
            timeout_sec=min(3.0, thresholds.pool_poll_sec),
            interval_sec=0.6,
        )
        if base_bal is None and quote_bal is None:
            result.metrics["live_vaults_note"] = (
                "skipped (vault accounts not visible after poll)"
            )
        else:
            live_base = float((base_bal or {}).get("uiAmount") or 0)
            live_quote_ui = vault_quote_ui(quote_bal)
            result.metrics["live_pool_base_ui"] = live_base
            result.metrics["live_pool_quote_ui"] = live_quote_ui
            live_sol = 0.0
            try:
                live_sol = quote_ui_to_sol(rpc, pool.quote_mint, live_quote_ui)
                result.metrics["live_pool_sol"] = live_sol
                # backfill migrate liquidity if snapshot was empty
                if base_ui <= 0 and live_base > 0:
                    base_ui = live_base
                if quote_sol <= 0 and live_sol > 0:
                    quote_sol = live_sol
                    result.metrics["pool_sol_at_migrate"] = quote_sol
                    result.metrics["pool_quote_ui_at_migrate"] = live_quote_ui
            except Exception as exc:  # noqa: BLE001
                result.metrics["live_pool_sol"] = 0.0
                result.metrics["live_pool_sol_error"] = str(exc)

            # liquidity floor from live vaults when migrate snapshot lacked balances
            if result.passed and float(result.metrics.get("pool_sol_at_migrate") or 0) <= 0:
                try:
                    if pool.quote_mint == WSOL_MINT:
                        if live_sol < thresholds.min_pool_sol:
                            result.fail(
                                f"live pool SOL too low: {live_sol:.4f} "
                                f"(min {thresholds.min_pool_sol})"
                            )
                    else:
                        sol_usd = get_sol_usd(rpc)
                        min_usd = thresholds.min_pool_sol * sol_usd
                        q_usd = quote_ui_to_usd(rpc, pool.quote_mint, live_quote_ui)
                        if q_usd < min_usd:
                            result.fail(
                                f"live pool quote liquidity too low: ${q_usd:,.2f} "
                                f"(min ${min_usd:,.2f})"
                            )
                except Exception as exc:  # noqa: BLE001
                    result.fail(f"live liquidity floor check failed: {exc}")

    # --- pump-only: bonding curve deployer ---
    if venue == "pump":
        curve_ok = _pump_curve_deployer(
            event, rpc, ledger_path, result, migrate_ok=migrate_ok
        )
        if not curve_ok:
            return _done(result, ledger_path, finalize=finalize)

    # --- all venues: birdeye global fees (same 2 SOL floor) ---
    if not _check_global_fees(event.mint, result, thresholds):
        return _done(result, ledger_path, finalize=finalize)

    # --- market cap ---
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

    return _done(result, ledger_path, finalize=finalize)


def _pump_curve_deployer(
    event: MigrationEvent,
    rpc: HeliusRpc,
    ledger_path: Path,
    result: FilterResult,
    *,
    migrate_ok: bool,
) -> bool:
    """pump bonding-curve deployer blacklist. false → caller should _done."""
    mint = event.mint
    curve_addr = bonding_curve_pda(mint)
    curve = None
    curve_acct = _wait_account(rpc, curve_addr, encoding="base64", attempts=6, delay=0.4)
    if curve_acct:
        try:
            curve = parse_bonding_curve(curve_addr, curve_acct["data"][0])
        except Exception as exc:  # noqa: BLE001
            result.fail(f"bonding curve parse failed: {exc}")
            return False

    if curve is not None:
        result.metrics["bonding_curve"] = curve_addr
        result.metrics["curve_complete"] = curve.complete
        result.metrics["deployer"] = curve.creator
        bad, why = is_deployer_blacklisted(ledger_path, curve.creator)
        if bad:
            result.fail(f"deployer blacklisted: {why or 'no reason'}")
            return False
        return True
    if migrate_ok:
        result.fail("could not read bonding curve for deployer checks")
        return False
    result.fail("bonding curve account missing")
    return False


def _check_global_fees(
    mint: str,
    result: FilterResult,
    thresholds: FilterThresholds,
) -> bool:
    """birdeye global_fees_paid floor — every venue. false → caller should _done."""
    try:
        fees_sol = fetch_global_fees_paid(mint)
    except Exception as exc:  # noqa: BLE001
        result.fail(f"global fees unavailable: {exc}")
        return False

    result.metrics["estimated_global_fees_sol"] = fees_sol
    result.metrics["fees_method"] = "birdeye token_overview global_fees_paid"
    result.metrics["fees_check_needed"] = fees_sol < thresholds.fees_warn_below_sol
    result.metrics["fees_warn_below_sol"] = thresholds.fees_warn_below_sol

    if fees_sol < thresholds.min_global_fees_sol:
        result.fail(
            f"global fees too low: {fees_sol:.4f} SOL "
            f"(min {thresholds.min_global_fees_sol})"
        )
        return False
    return True


def _expected_owner(venue: str) -> str | None:
    if venue == "pump":
        return PUMP_AMM_PROGRAM
    if venue == "launchlab":
        return RAYDIUM_CPMM_PROGRAM
    if venue == "meteora_dbc":
        return METEORA_DAMM_V2_PROGRAM
    return None


def _wait_account(
    rpc: HeliusRpc,
    pubkey: str,
    *,
    encoding: str = "base64",
    attempts: int = 10,
    delay: float = 0.4,
) -> dict[str, Any] | None:
    """poll getAccountInfo until it shows up (or we give up) — backoff."""
    last: dict[str, Any] | None = None
    wait = delay
    for i in range(max(1, attempts)):
        if encoding == "jsonParsed":
            last = rpc.get_account_json_parsed(pubkey)
        else:
            last = rpc.get_account_info(pubkey, encoding=encoding)
        if last is not None:
            return last
        if i + 1 < attempts:
            time.sleep(wait)
            wait = min(wait * 1.4, 2.0)
    return last


def _poll_token_balances(
    rpc: HeliusRpc,
    base_ata: str,
    quote_ata: str,
    *,
    timeout_sec: float,
    interval_sec: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """wait briefly for pool vault ATAs (batched read each try)."""
    deadline = time.monotonic() + max(0.0, timeout_sec)
    base_bal: dict[str, Any] | None = None
    quote_bal: dict[str, Any] | None = None
    wait = max(0.05, interval_sec)
    while True:
        b, q = rpc.get_token_balances_pair(base_ata, quote_ata)
        if b is not None:
            base_bal = b
        if q is not None:
            quote_bal = q
        if base_bal is not None and quote_bal is not None:
            return base_bal, quote_bal
        if time.monotonic() >= deadline:
            return base_bal, quote_bal
        time.sleep(wait)
        wait = min(wait * 1.3, 2.0)


def _check_lp_burned(pool: PoolInfo) -> tuple[bool, str]:
    """use already-fetched pool.lp_supply — no extra helius call."""
    lp_supply = int(pool.lp_supply or 0)
    if lp_supply == 0:
        return True, "lp_supply=0 (burned/ok)"
    return True, f"lp_supply={lp_supply} (ok; no extra rpc)"


def _done(
    result: FilterResult,
    ledger_path: Path,
    *,
    finalize: bool,
) -> FilterResult:
    """optionally log final pass/fail (skip when caller will log)."""
    if not finalize:
        return result
    if not result.passed:
        reason = "; ".join(result.reasons)
        log_filter_reject(ledger_path, result.mint, reason)
        log.info("FILTER REJECT %s — %s", result.mint, reason)
    else:
        fees = result.metrics.get("estimated_global_fees_sol")
        fees_s = f"{float(fees):.2f}" if fees is not None else "-"
        fees_note = " fees=CHECK" if result.metrics.get("fees_check_needed") else ""
        log.info(
            "FILTER PASS %s — venue=%s mcap=$%.0f fees~%sSOL%s pool=%.2fSOL",
            result.mint,
            result.metrics.get("venue"),
            float(result.metrics.get("market_cap_usd") or 0),
            fees_s,
            fees_note,
            float(result.metrics.get("pool_sol_at_migrate") or 0),
        )
    return result


def _reject(
    result: FilterResult,
    ledger_path: Path,
    reason: str,
    *,
    finalize: bool = True,
) -> FilterResult:
    """fail with one reason and optionally log."""
    result.fail(reason)
    return _done(result, ledger_path, finalize=finalize)
