"""
quote-token → USD → SOL helpers.

paper wallet stays SOL-denominated. for non-WSOL pumpswap pairs we mark
everything in SOL by converting the quote vault through USD.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple

from archetype.filters.helius import HeliusRpc
from archetype.filters.sol_price import get_sol_usd
from archetype.filters.thresholds import WSOL_MINT

log = logging.getLogger("archetype.price.convert")


class QuoteMeta(NamedTuple):
    """usd price + labels for a quote mint."""

    usd: float
    symbol: str | None
    name: str | None


# mint → (meta, ts)
_quote_cache: dict[str, tuple[QuoteMeta, float]] = {}
_CACHE_TTL = 60.0


def quote_meta(rpc: HeliusRpc, quote_mint: str) -> QuoteMeta:
    """
    usd / symbol / name for a quote mint.

    WSOL short-circuits to SOL/USD. everything else uses Helius DAS.
    """
    if quote_mint == WSOL_MINT:
        return QuoteMeta(float(get_sol_usd(rpc)), "SOL", "Solana")

    now = time.time()
    hit = _quote_cache.get(quote_mint)
    if hit and now - hit[1] < _CACHE_TTL and hit[0].usd > 0:
        return hit[0]

    meta = _from_das(rpc, quote_mint)
    if meta.usd is None or meta.usd <= 0:
        raise RuntimeError(f"no USD price for quote mint {quote_mint[:8]}…")
    out = QuoteMeta(float(meta.usd), meta.symbol, meta.name)
    _quote_cache[quote_mint] = (out, now)
    return out


def quote_ui_to_sol(rpc: HeliusRpc, quote_mint: str, quote_ui: float) -> float:
    """quote vault ui amount → SOL-equivalent value."""
    if quote_ui <= 0:
        return 0.0
    if quote_mint == WSOL_MINT:
        return float(quote_ui)
    q = quote_meta(rpc, quote_mint)
    sol_usd = float(get_sol_usd(rpc))
    if sol_usd <= 0:
        raise RuntimeError("SOL/USD is zero")
    return (float(quote_ui) * q.usd) / sol_usd


def quote_ui_to_usd(rpc: HeliusRpc, quote_mint: str, quote_ui: float) -> float:
    """quote vault ui amount → USD."""
    if quote_ui <= 0:
        return 0.0
    if quote_mint == WSOL_MINT:
        return float(quote_ui) * float(get_sol_usd(rpc))
    return float(quote_ui) * quote_meta(rpc, quote_mint).usd


def vault_quote_ui(quote_bal: dict[str, Any] | None) -> float:
    """ui amount from a getTokenAccountBalance / parsed vault dict."""
    if not quote_bal:
        return 0.0
    ui = quote_bal.get("uiAmount")
    if ui is not None:
        try:
            return float(ui)
        except (TypeError, ValueError):
            pass
    raw = quote_bal.get("amount")
    if raw is None:
        return 0.0
    try:
        dec = int(quote_bal.get("decimals") or 0)
        return int(raw) / (10 ** dec)
    except (TypeError, ValueError):
        return 0.0


class _DasBits(NamedTuple):
    usd: float | None
    symbol: str | None
    name: str | None


def _from_das(rpc: HeliusRpc, mint: str) -> _DasBits:
    try:
        asset = rpc.get_asset(mint)
    except Exception as exc:  # noqa: BLE001
        log.warning("das quote price failed %s: %s", mint[:8], exc)
        return _DasBits(None, None, None)
    if not asset:
        return _DasBits(None, None, None)
    info = (asset.get("token_info") or {}).get("price_info") or {}
    price = info.get("price_per_token")
    meta = ((asset.get("content") or {}).get("metadata") or {})
    symbol = meta.get("symbol")
    name = meta.get("name")
    try:
        usd = float(price) if price is not None else None
    except (TypeError, ValueError):
        usd = None
    return _DasBits(
        usd,
        str(symbol) if symbol else None,
        str(name) if name else None,
    )
