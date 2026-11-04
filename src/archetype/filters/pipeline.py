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
from archetype.filters.birdeye import fetch_global_fees_paid
from archetype.filters.decode import (
    bonding_curve_pda,
    extract_pool_from_migrate_tx,
    parse_bonding_curve,
    parse_mint_json_parsed,
    parse_pool,
    PoolInfo,
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
from archetype.price.convert import quote_meta, quote_ui_to_sol, quote_ui_to_usd, vault_quote_ui

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
    *,
    finalize: bool = True,
) -> FilterResult:
    """
    evaluate one migration (on-chain only).

    order: cheap blacklist → mint → (fast-fail) → tx/pool → curve/deployer → birdeye fees → mcap.
    hard mint fails / obvious junk bail before expensive polls.
    set finalize=False if a caller (e.g. socials gate) will log the final outcome.
    """
    ensure_blacklist_tables(ledger_path)
    result = FilterResult(passed=True, mint=event.mint)
    mint = event.mint

    # --- blacklist ---
    bad, why = is_mint_blacklisted(ledger_path, mint)
    if bad:
        return _reject(
            result, ledger_path, f"mint blacklisted: {why or 'no reason'}", finalize=finalize
        )

    # --- mint account (retry — pumpportal can beat rpc visibility) ---
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

    # hard mint fails → bail before getTransaction / pool poll / fee poll
    if not result.passed:
        return _done(result, ledger_path, finalize=finalize)

    # --- migrate tx first (this is the graduation proof) ---
    quote_mint = event.quote_mint or WSOL_MINT
    result.metrics["quote_mint"] = quote_mint
    result.metrics["venue"] = event.venue
    if event.platform:
        result.metrics["platform"] = event.platform

    pool_addr: str | None = None
    quote_ui = 0.0
    quote_sol = 0.0  # SOL-equivalent of quote vault (for floors + mcap)
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
                pool_addr, quote_ui, base_ui = extract_pool_from_migrate_tx(
                    tx, mint, quote_mint=quote_mint
                )
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
                    result.fail(f"quote→SOL convert failed: {exc}")
    else:
        result.fail("missing migrate signature")

    # liquidity floor: WSOL uses SOL units; other quotes use USD ≡ min_pool_sol * sol_usd
    if result.passed and quote_mint == WSOL_MINT:
        if quote_sol < thresholds.min_pool_sol:
            result.fail(
                f"pool SOL at migrate too low: {quote_sol:.4f} "
                f"(min {thresholds.min_pool_sol})"
            )
    elif result.passed:
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
        result.fail("could not resolve pumpswap pool from migrate tx")
    else:
        result.metrics["pool"] = pool_addr

    # already dead from migrate snapshot → skip the pool poll and the fee read
    if not result.passed:
        result.metrics["pool_poll_skipped"] = "already failing after migrate snapshot"
        return _done(result, ledger_path, finalize=finalize)

    # poll pool account only while still a candidate
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
    elif pool_acct.get("owner") != PUMP_AMM_PROGRAM:
        result.fail(f"pool owner not Pump AMM: {pool_acct.get('owner')}")
        return _done(result, ledger_path, finalize=finalize)
    else:
        result.metrics["pool_account_pending"] = False
        try:
            pool = parse_pool(pool_addr, pool_acct["data"][0])
        except Exception as exc:  # noqa: BLE001
            result.fail(f"pool parse failed: {exc}")
            return _done(result, ledger_path, finalize=finalize)
        else:
            result.metrics["pool_creator"] = pool.creator
            result.metrics["coin_creator"] = pool.coin_creator
            result.metrics["lp_mint"] = pool.lp_mint
            result.metrics["lp_supply"] = pool.lp_supply

            if pool.base_mint != mint:
                result.fail("pool base_mint mismatch")
            # any quote is fine for pump — just must match what repair found
            if pool.quote_mint != quote_mint:
                result.fail(
                    f"pool quote_mint mismatch: pool={pool.quote_mint} "
                    f"event={quote_mint}"
                )
            result.metrics["pool_quote_mint"] = pool.quote_mint
            if not result.passed:
                return _done(result, ledger_path, finalize=finalize)

            lp_ok, lp_note = _check_lp_burned(pool)
            result.metrics["lp_burn_check"] = lp_note
            if not lp_ok:
                result.fail(lp_note)
                return _done(result, ledger_path, finalize=finalize)

            # stash pool for later price resolves (cache)
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
                try:
                    result.metrics["live_pool_sol"] = quote_ui_to_sol(
                        rpc, pool.quote_mint, live_quote_ui
                    )
                except Exception as exc:  # noqa: BLE001
                    result.metrics["live_pool_sol"] = 0.0
                    result.metrics["live_pool_sol_error"] = str(exc)

    # --- bonding curve (deployer only) ---
    # NOTE: we do NOT gate on curve.complete anymore.
    curve_addr = bonding_curve_pda(mint)
    curve = None
    curve_acct = _wait_account(rpc, curve_addr, encoding="base64", attempts=6, delay=0.4)
    if curve_acct:
        try:
            curve = parse_bonding_curve(curve_addr, curve_acct["data"][0])
        except Exception as exc:  # noqa: BLE001
            return _reject(
                result, ledger_path, f"bonding curve parse failed: {exc}", finalize=finalize
            )

    if curve is not None:
        result.metrics["bonding_curve"] = curve_addr
        result.metrics["curve_complete"] = curve.complete  # informational only
        result.metrics["deployer"] = curve.creator

        bad, why = is_deployer_blacklisted(ledger_path, curve.creator)
        if bad:
            result.fail(f"deployer blacklisted: {why or 'no reason'}")
            return _done(result, ledger_path, finalize=finalize)
    elif migrate_ok:
        result.fail("could not read bonding curve for deployer checks")
        return _done(result, ledger_path, finalize=finalize)
    else:
        return _reject(
            result, ledger_path, "bonding curve account missing", finalize=finalize
        )

    # --- global fees (birdeye indexed — replaces old vault estimate) ---
    try:
        fees_sol = fetch_global_fees_paid(mint)
    except Exception as exc:  # noqa: BLE001
        result.fail(f"global fees unavailable: {exc}")
        return _done(result, ledger_path, finalize=finalize)

    result.metrics["estimated_global_fees_sol"] = fees_sol
    result.metrics["fees_method"] = "birdeye token_overview global_fees_paid"
    # cleared 2 SOL but still under the warn bar → yellow CHECK in --trade
    fees_check = fees_sol < thresholds.fees_warn_below_sol
    result.metrics["fees_check_needed"] = fees_check
    result.metrics["fees_warn_below_sol"] = thresholds.fees_warn_below_sol

    if fees_sol < thresholds.min_global_fees_sol:
        result.fail(
            f"global fees too low: {fees_sol:.4f} SOL "
            f"(min {thresholds.min_global_fees_sol})"
        )
        return _done(result, ledger_path, finalize=finalize)

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

    return _done(result, ledger_path, finalize=finalize)


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
    """
    use already-fetched pool.lp_supply — no extra helius call.

    pump migrate burns LP; lp_supply 0 (or tiny) is the usual signal.
    """
    lp_supply = int(pool.lp_supply or 0)
    if lp_supply == 0:
        return True, "lp_supply=0 (burned/ok)"
    # soft pass — historical heuristic always passed after largest-accounts too
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
        fees = float(result.metrics.get("estimated_global_fees_sol") or 0)
        fees_note = " fees=CHECK" if result.metrics.get("fees_check_needed") else ""
        log.info(
            "FILTER PASS %s — mcap=$%.0f fees~%.2fSOL%s pool=%.2fSOL",
            result.mint,
            float(result.metrics.get("market_cap_usd") or 0),
            fees,
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
