"""
run on-chain filters then (if pass) metadata + socials gate.
"""

from __future__ import annotations

import logging
from pathlib import Path

from archetype.filters.blacklist import log_filter_reject
from archetype.filters.helius import HeliusRpc
from archetype.filters.pipeline import FilterResult, run_onchain_filters
from archetype.filters.thresholds import DEFAULT_THRESHOLDS, FilterThresholds
from archetype.ingest.models import MigrationEvent
from archetype.metadata.fetch import fetch_token_metadata
from archetype.metadata.socials import apply_socials_gate

log = logging.getLogger("archetype.filters")


def run_full_filters(
    event: MigrationEvent,
    rpc: HeliusRpc,
    ledger_path: Path,
    thresholds: FilterThresholds = DEFAULT_THRESHOLDS,
) -> FilterResult:
    """
    phase 2 pipeline: on-chain → (only if pass) metadata uri → socials gate.

    socials rule: need ≥1 of twitter / telegram / website.
    """
    result = run_onchain_filters(
        event, rpc, ledger_path, thresholds, finalize=False
    )

    if not result.passed:
        reason = "; ".join(result.reasons)
        log_filter_reject(ledger_path, event.mint, reason)
        log.info("FILTER REJECT %s — %s", event.mint, reason)
        return result

    # on-chain passed → pull off-chain metadata + socials
    meta = fetch_token_metadata(event.mint, rpc)
    meta_dict = meta.to_dict()
    raw = meta_dict.pop("raw_uri", {})
    meta_dict["raw_uri_keys"] = sorted(raw.keys()) if isinstance(raw, dict) else []
    result.metrics["metadata"] = meta_dict

    event.name = meta.name or event.name
    event.symbol = meta.symbol or event.symbol
    event.metadata = meta_dict

    gate = apply_socials_gate(meta)
    result.metrics["socials_present"] = gate.socials_present
    result.metrics["socials_gate_passed"] = gate.passed

    if not gate.passed:
        for reason in gate.reasons:
            result.fail(reason)
        reason = "; ".join(result.reasons)
        log_filter_reject(ledger_path, event.mint, reason)
        log.info("FILTER REJECT %s — %s", event.mint, reason)
        return result

    # wave C — only on PASS: mint lookup on otcdesks (empty = not otc)
    try:
        from archetype.metadata.otc import attach_otc_to_event

        otc = attach_otc_to_event(event)
        if otc is not None:
            result.metrics.update(otc.to_metrics())
    except Exception as exc:  # noqa: BLE001
        log.warning("otc enrichment skipped %s: %s", event.mint, exc)
        result.metrics["otc"] = False
        result.metrics["otc_error"] = str(exc)

    fees = result.metrics.get("estimated_global_fees_sol")
    fees_s = f"{float(fees):.2f}" if fees is not None else "-"
    fees_note = " fees=CHECK" if result.metrics.get("fees_check_needed") else ""
    otc_note = " otc=yes" if result.metrics.get("otc") else ""
    log.info(
        "FILTER PASS %s — mcap=$%.0f fees~%sSOL%s socials=%s name=%s%s",
        event.mint,
        float(result.metrics.get("market_cap_usd") or 0),
        fees_s,
        fees_note,
        ",".join(gate.socials_present) or "-",
        (meta.name or event.name or "?")[:32],
        otc_note,
    )
    return result
