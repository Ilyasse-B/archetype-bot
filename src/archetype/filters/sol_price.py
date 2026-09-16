"""
sol/usd helper — coingecko first, helius DAS fallback.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from archetype.filters.helius import HeliusRpc

log = logging.getLogger("archetype.sol_price")

_cache: dict[str, float] = {"price": 0.0, "ts": 0.0}
# fdv moves with token price far more than sol — 5 min is fine
_CACHE_TTL = 300.0
_cg_fail_until = 0.0


def get_sol_usd(rpc: HeliusRpc | None = None) -> float:
    """cached SOL/USD."""
    global _cg_fail_until
    now = time.time()
    if _cache["price"] > 0 and now - _cache["ts"] < _CACHE_TTL:
        return _cache["price"]

    price = None
    if now >= _cg_fail_until:
        price = _from_coingecko()
        if price is None:
            # back off CG briefly so we dont hammer it
            _cg_fail_until = now + 60.0

    if price is None and rpc is not None:
        price = _from_helius_das(rpc)
    if price is None or price <= 0:
        raise RuntimeError("could not fetch SOL/USD price")
    _cache["price"] = price
    _cache["ts"] = now
    return price


def _from_coingecko() -> float | None:
    try:
        resp = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        return float(resp.json()["solana"]["usd"])
    except Exception as exc:  # noqa: BLE001
        log.warning("coingecko sol price failed: %s", exc)
        return None


def _from_helius_das(rpc: HeliusRpc) -> float | None:
    try:
        asset = rpc.get_asset("So11111111111111111111111111111111111111112")
        if not asset:
            return None
        info = (asset.get("token_info") or {}).get("price_info") or {}
        price = info.get("price_per_token")
        return float(price) if price is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning("helius das sol price failed: %s", exc)
        return None
