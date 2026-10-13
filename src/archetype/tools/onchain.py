"""
live on-chain metrics for the coin in focus.

http only. holders, 5m trades, deployer history.
no helius. a miss on one call leaves that line unavailable.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

log = logging.getLogger("archetype.tools.onchain")

_UA = "Mozilla/5.0 (compatible; ArchetypeBot/0.1; +https://localhost)"
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
}

_COINS_V2 = "https://frontend-api-v3.pump.fun/coins-v2/{mint}"
_HOLDERS = "https://frontend-api-v3.pump.fun/coins/top-holders/{mint}"
_ACTIVITY = "https://swap-api.pump.fun/v1/coins/{mint}/market-activity"
_CREATED = "https://frontend-api-v3.pump.fun/coins-v2/user-created-coins/{creator}"

# first payload only. later calls are just the numbers.
_METRICS_NOTE = "a gauge of the coin right now. do not decide the trade from these."


def fetch_onchain_metrics(mint: str, timeout: float = 12.0, explain: bool = False) -> str:
    """
    tight live block for the first think, and again when the agent asks.

    each call hits the apis again. nothing here is cached.
    explain sticks a short note on each field. only the first read wants that.
    """
    mint = (mint or "").strip()
    if not mint:
        return "onchain: unavailable"
    try:
        coin, holders, activity = _first_wave(mint, timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("onchain metrics failed %s: %s", mint, exc)
        return "onchain: unavailable"

    creator = ""
    supply = None
    if isinstance(coin, dict):
        raw_creator = coin.get("creator")
        if isinstance(raw_creator, str) and raw_creator.strip():
            creator = raw_creator.strip()
        supply = _ui_supply(coin.get("total_supply"))

    created = _get_json(_CREATED.format(creator=creator), timeout) if creator else None
    lines = [
        "onchain (live):",
        _said("holders", _holders_count(holders), "unique wallets holding the coin", explain),
        _said("top10_pct", _top10_pct(holders, supply), "supply held by the top 10 wallets", explain),
        _said("dev_pct", _dev_pct(holders, supply), "supply the developer still holds", explain),
        _said("snipers_in_top", _flag_count(holders, "isSniper"), "flagged snipers in the top 10", explain),
        _said("bundlers_in_top", _flag_count(holders, "isBundler"), "flagged bundlers in the top 10", explain),
        _said(
            "trades_5m",
            _trade_line(_window(activity, "5m")),
            "buys, sells, buyers, sellers, and the 5 minute price move",
            explain,
        ),
        _said(
            "deployer",
            _deployer_line(created),
            "coins this creator launched, and how many graduated",
            explain,
        ),
    ]
    if explain:
        lines.append(_METRICS_NOTE)
    return "\n".join(lines)


def _said(label: str, value: str, note: str, explain: bool) -> str:
    """number on its own, or number plus a short note the first time."""
    if not explain:
        return f"{label}: {value}"
    return f"{label}: {value} ({note})"


def _first_wave(mint: str, timeout: float) -> tuple[Any, Any, Any]:
    """coins-v2, holders, and trade windows. none of these need the others."""
    urls = (
        _COINS_V2.format(mint=mint),
        _HOLDERS.format(mint=mint),
        _ACTIVITY.format(mint=mint),
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        coin, holders, activity = pool.map(lambda url: _get_json(url, timeout), urls)
    return coin, holders, activity


def _get_json(url: str, timeout: float) -> dict[str, Any] | None:
    """one get. 404 and junk are None, not a second data source."""
    try:
        resp = httpx.get(
            url,
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("onchain get failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("onchain get %s -> %s", url, resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _ui_supply(raw: Any) -> float | None:
    """coins-v2 total_supply is raw. pump mints use 6 decimals."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return float(raw) / 1_000_000


def _holder_ui(amount: Any, ui_supply: float | None) -> float | None:
    """
    top-holders amount is ui tokens.

    if a row comes back in raw units it is bigger than the whole supply,
    so scale that one down. don't guess a supply when coins-v2 missed.
    """
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    val = float(amount)
    if val < 0:
        return None
    if ui_supply and val > ui_supply:
        val = val / 1_000_000
    return val


def _holder_rows(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    rows = body.get("topHolders")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _holders_count(body: Any) -> str:
    if not isinstance(body, dict):
        return "unavailable"
    return _count(body.get("totalHolders"))


def _top10_pct(body: Any, ui_supply: float | None) -> str:
    if not ui_supply:
        return "unavailable"
    ranked: list[float] = []
    for row in _holder_rows(body):
        amt = _holder_ui(row.get("amount"), ui_supply)
        if amt is None:
            continue
        ranked.append(amt)
    if not ranked:
        return "unavailable"
    ranked.sort(reverse=True)
    share = sum(ranked[:10]) / ui_supply * 100.0
    return _pct(share)


def _dev_pct(body: Any, ui_supply: float | None) -> str:
    if not ui_supply:
        return "unavailable"
    total = 0.0
    seen = False
    for row in _holder_rows(body):
        if row.get("isDev") is not True:
            continue
        amt = _holder_ui(row.get("amount"), ui_supply)
        if amt is None:
            continue
        seen = True
        total += amt
    if not seen:
        # not in the top list. that is 0, not a failed call.
        return "0" if _holder_rows(body) else "unavailable"
    return _pct(total / ui_supply * 100.0)


def _flag_count(body: Any, key: str) -> str:
    rows = _holder_rows(body)
    if not isinstance(body, dict):
        return "unavailable"
    if not rows and body.get("totalHolders") in (None, ""):
        return "unavailable"
    return str(sum(1 for row in rows if row.get(key) is True))


def _window(body: Any, key: str) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    window = body.get(key)
    return window if isinstance(window, dict) else None


def _trade_line(window: dict[str, Any] | None) -> str:
    if not isinstance(window, dict):
        return "unavailable"
    buys = _as_int(window.get("numBuys"))
    sells = _as_int(window.get("numSells"))
    buyers = _as_int(window.get("numBuyers"))
    sellers = _as_int(window.get("numSellers"))
    move = window.get("priceChangePercent")
    if None in (buys, sells, buyers, sellers) or isinstance(move, bool) or not isinstance(
        move, (int, float)
    ):
        return "unavailable"
    return (
        f"{buys:,} buys, {sells:,} sells, "
        f"{buyers:,} buyers, {sellers:,} sellers, "
        f"price {_signed(float(move))}%"
    )


def _deployer_line(body: Any) -> str:
    if not isinstance(body, dict):
        return "unavailable"
    count = body.get("count")
    coins = body.get("coins")
    if isinstance(count, bool) or not isinstance(count, int) or not isinstance(coins, list):
        return "unavailable"
    graduated = sum(1 for coin in coins if isinstance(coin, dict) and coin.get("complete") is True)
    if count > len(coins):
        return f"{count} launched, {graduated} graduated among the latest {len(coins)}"
    return f"{count} launched, {graduated} graduated"


def _count(value: Any) -> str:
    n = _as_int(value)
    if n is None:
        return "unavailable"
    return f"{n:,}"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _pct(value: float) -> str:
    text = f"{value:.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


def _signed(value: float) -> str:
    text = _pct(value)
    if value > 0:
        return f"+{text}"
    return text
