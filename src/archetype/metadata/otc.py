"""
otc desks enrichment — not a feed, just a mint lookup after a filter PASS.

GET https://otcdesks.cash/api/coins?mint=<mint>
  hit  → row with rewardBasket / rewardSymbol
  miss → {"coins": []}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("archetype.otc")

OTC_COINS_URL = "https://otcdesks.cash/api/coins"


@dataclass
class OtcInfo:
    """reward basket attached to an otc desks coin."""

    mint: str
    name: str | None = None
    symbol: str | None = None
    category: str | None = None
    reward_mint: str | None = None
    reward_symbol: str | None = None
    # human labels for the agent, e.g. "NVIDIA xStock (NVDAx)"
    basket_labels: list[str] = field(default_factory=list)
    # raw mints from the api
    basket_mints: list[str] = field(default_factory=list)

    def to_metrics(self) -> dict[str, Any]:
        """fields we stash on filter_metrics / coin_info."""
        return {
            "otc": True,
            "otc_category": self.category,
            "otc_reward_mint": self.reward_mint,
            "otc_reward_symbol": self.reward_symbol,
            "otc_basket_mints": list(self.basket_mints),
            "otc_basket": list(self.basket_labels),
        }


def lookup_otc_coin(mint: str, *, timeout: float = 12.0) -> OtcInfo | None:
    """
    query otcdesks by mint. None if not listed / request failed.

    only call this for PASSED migrations — one cheap GET per pass.
    """
    mint = (mint or "").strip()
    if not mint or mint == "pending":
        return None
    try:
        resp = httpx.get(
            OTC_COINS_URL,
            params={"mint": mint},
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("otc lookup failed %s: %s", mint[:8], exc)
        return None

    coins = (body or {}).get("coins") if isinstance(body, dict) else None
    if not isinstance(coins, list) or not coins:
        return None
    row = coins[0]
    if not isinstance(row, dict):
        return None

    basket_mints = [
        str(x) for x in (row.get("rewardBasket") or []) if isinstance(x, str) and x
    ]
    reward_mint = row.get("rewardMint")
    if isinstance(reward_mint, str) and reward_mint and reward_mint not in basket_mints:
        # keep primary first if api omitted it from the array
        basket_mints = [reward_mint, *basket_mints]

    labels = _label_basket(basket_mints, primary_symbol=row.get("rewardSymbol"))
    return OtcInfo(
        mint=str(row.get("mint") or mint),
        name=row.get("name") if isinstance(row.get("name"), str) else None,
        symbol=row.get("symbol") if isinstance(row.get("symbol"), str) else None,
        category=row.get("category") if isinstance(row.get("category"), str) else None,
        reward_mint=str(reward_mint) if isinstance(reward_mint, str) else None,
        reward_symbol=(
            str(row.get("rewardSymbol"))
            if isinstance(row.get("rewardSymbol"), str)
            else None
        ),
        basket_labels=labels,
        basket_mints=basket_mints,
    )


def attach_otc_to_event(event: Any) -> OtcInfo | None:
    """
    if this mint is on otc desks, stamp metrics + prefer their name/symbol.

    mutates event.filter_metrics (and name/symbol when missing).
    """
    info = lookup_otc_coin(getattr(event, "mint", "") or "")
    if info is None:
        return None
    metrics = dict(getattr(event, "filter_metrics", None) or {})
    metrics.update(info.to_metrics())
    event.filter_metrics = metrics
    if info.name and not getattr(event, "name", None):
        event.name = info.name
    if info.symbol and not getattr(event, "symbol", None):
        event.symbol = info.symbol
    log.info(
        "OTC hit %s — reward=%s basket=%s",
        event.mint,
        info.reward_symbol or "?",
        ",".join(info.basket_labels) or "-",
    )
    return info


def _label_basket(
    mints: list[str],
    *,
    primary_symbol: Any = None,
) -> list[str]:
    """turn basket mints into 'Name (TICKER)' lines for the agent card."""
    out: list[str] = []
    for i, m in enumerate(mints):
        name, sym = _resolve_mint_label(m)
        if i == 0 and not sym and isinstance(primary_symbol, str) and primary_symbol:
            sym = primary_symbol
        if name and sym:
            out.append(f"{name} ({sym})")
        elif sym:
            out.append(sym)
        elif name:
            out.append(name)
        else:
            out.append(m[:8] + "…")
    return out


def _resolve_mint_label(mint: str) -> tuple[str | None, str | None]:
    """best-effort name/symbol via jupiter (no key)."""
    try:
        resp = httpx.get(
            "https://lite-api.jup.ag/tokens/v2/search",
            params={"query": mint},
            timeout=10.0,
        )
        resp.raise_for_status()
        rows = resp.json()
        if isinstance(rows, list) and rows:
            row = rows[0]
            if isinstance(row, dict) and row.get("id") == mint:
                name = row.get("name")
                sym = row.get("symbol")
                return (
                    str(name) if isinstance(name, str) else None,
                    str(sym) if isinstance(sym, str) else None,
                )
    except Exception as exc:  # noqa: BLE001
        log.debug("basket label resolve fail %s: %s", mint[:8], exc)
    return None, None
