"""
live metrics for the coin in focus.

jupiter + solana tracker. no pump.fun http. no helius.
a miss on one call leaves that line unavailable.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

log = logging.getLogger("archetype.tools.onchain")

_ACCEPT = {"Accept": "application/json"}
_JUPITER = "https://lite-api.jup.ag/tokens/v2/search"
_TRACKER = "https://data.solanatracker.io/tokens/{mint}"
_TRACKER_STATS = "https://data.solanatracker.io/stats/{mint}"

# first payload only. later calls are just the numbers.
_METRICS_NOTE = "a gauge of the coin right now. do not decide the trade from these."


def fetch_onchain_metrics(
    mint: str,
    tracker_api_key: str = "",
    timeout: float = 12.0,
    explain: bool = False,
) -> str:
    """
    tight live block for the first think, and again when the agent asks.

    each call hits the apis again. nothing here is cached.
    explain sticks a short note on each field. only the first read wants that.
    """
    mint = (mint or "").strip()
    if not mint:
        return "onchain: unavailable"
    try:
        jup, tracker, stats = _first_wave(mint, tracker_api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("onchain metrics failed %s: %s", mint, exc)
        return "onchain: unavailable"

    if not isinstance(jup, dict) and not isinstance(tracker, dict) and not isinstance(
        stats, dict
    ):
        return "onchain: unavailable"

    lines = [
        "onchain (live):",
        _said(
            "holders",
            _holders(jup, tracker),
            "unique wallets holding the coin",
            explain,
        ),
        _said(
            "top10_pct",
            _top10(jup),
            "supply held by the top 10 wallets",
            explain,
        ),
        _said(
            "dev_pct",
            _dev_pct(tracker),
            "supply the developer still holds",
            explain,
        ),
        _said(
            "snipers",
            _snipers(tracker),
            "flagged sniper wallets that still hold supply",
            explain,
        ),
        _said(
            "bundlers",
            _bundlers(tracker),
            "flagged bundler wallets that still hold supply",
            explain,
        ),
        _said(
            "trades_5m",
            _trades_5m(stats, jup),
            "buys, sells, buyers, sellers, and the 5 minute price move",
            explain,
        ),
        _said(
            "deployer",
            _deployer(jup),
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


def _first_wave(
    mint: str, tracker_api_key: str, timeout: float
) -> tuple[Any, Any, Any]:
    """jupiter + tracker token + tracker stats. independent."""
    key = (tracker_api_key or "").strip()
    th = dict(_ACCEPT)
    if key:
        th["x-api-key"] = key

    def jup() -> Any:
        return _get_jupiter(mint, timeout)

    def tok() -> Any:
        if not key:
            return None
        return _get_json(_TRACKER.format(mint=mint), timeout, th)

    def stats() -> Any:
        if not key:
            return None
        return _get_json(_TRACKER_STATS.format(mint=mint), timeout, th)

    with ThreadPoolExecutor(max_workers=3) as pool:
        return pool.submit(jup).result(), pool.submit(tok).result(), pool.submit(stats).result()


def _get_jupiter(mint: str, timeout: float) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            _JUPITER,
            params={"query": mint},
            headers=_ACCEPT,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("jupiter onchain failed %s: %s", mint[:8], exc)
        return None
    if resp.status_code != 200:
        log.warning("jupiter onchain %s -> %s", mint[:8], resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, list):
        return None
    for row in body:
        if isinstance(row, dict) and row.get("id") == mint:
            return row
    return body[0] if body and isinstance(body[0], dict) else None


def _get_json(
    url: str, timeout: float, headers: dict[str, str] | None = None
) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            url,
            headers=headers or _ACCEPT,
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


def _holders(jup: Any, tracker: Any) -> str:
    if isinstance(jup, dict):
        n = _as_int(jup.get("holderCount"))
        if n is not None:
            return f"{n:,}"
    if isinstance(tracker, dict):
        n = _as_int(tracker.get("holders"))
        if n is not None:
            return f"{n:,}"
    return "unavailable"


def _top10(jup: Any) -> str:
    if not isinstance(jup, dict):
        return "unavailable"
    audit = jup.get("audit")
    if not isinstance(audit, dict):
        return "unavailable"
    pct = audit.get("topHoldersPercentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return "unavailable"
    return _pct(float(pct))


def _dev_pct(tracker: Any) -> str:
    if not isinstance(tracker, dict):
        return "unavailable"
    risk = tracker.get("risk")
    if not isinstance(risk, dict):
        return "unavailable"
    dev = risk.get("dev")
    if not isinstance(dev, dict):
        return "unavailable"
    pct = dev.get("percentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return "unavailable"
    return _pct(float(pct))


def _snipers(tracker: Any) -> str:
    """count of flagged snipers that still hold (balance > 0)."""
    if not isinstance(tracker, dict):
        return "unavailable"
    risk = tracker.get("risk")
    if not isinstance(risk, dict) or not isinstance(risk.get("snipers"), dict):
        return "unavailable"
    group = risk["snipers"]
    wallets = group.get("wallets")
    if isinstance(wallets, list):
        holding = 0
        for row in wallets:
            if not isinstance(row, dict):
                continue
            bal = row.get("balance")
            if isinstance(bal, (int, float)) and not isinstance(bal, bool) and bal > 0:
                holding += 1
        return str(holding)
    # no wallet list — fall back to count only when they still hold pct
    count = _as_int(group.get("count"))
    pct = group.get("totalPercentage")
    if count is None:
        return "unavailable"
    if isinstance(pct, (int, float)) and not isinstance(pct, bool) and pct <= 0:
        return "0"
    return str(count)


def _bundlers(tracker: Any) -> str:
    if not isinstance(tracker, dict):
        return "unavailable"
    risk = tracker.get("risk")
    if not isinstance(risk, dict) or not isinstance(risk.get("bundlers"), dict):
        return "unavailable"
    group = risk["bundlers"]
    count = _as_int(group.get("count"))
    pct = group.get("totalPercentage")
    if count is None:
        return "unavailable"
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        return f"{count} holding {_pct(float(pct))}%"
    return str(count)


def _trades_5m(stats: Any, jup: Any) -> str:
    """prefer tracker stats (has buyers/sellers); else jupiter stats5m."""
    if isinstance(stats, dict):
        window = stats.get("5m")
        if isinstance(window, dict):
            buys = _as_int(window.get("buys"))
            sells = _as_int(window.get("sells"))
            buyers = _as_int(window.get("buyers"))
            sellers = _as_int(window.get("sellers"))
            move = window.get("priceChangePercentage")
            if None not in (buys, sells, buyers, sellers) and isinstance(
                move, (int, float)
            ) and not isinstance(move, bool):
                return (
                    f"{buys:,} buys, {sells:,} sells, "
                    f"{buyers:,} buyers, {sellers:,} sellers, "
                    f"price {_signed(float(move))}%"
                )
    if isinstance(jup, dict) and isinstance(jup.get("stats5m"), dict):
        s = jup["stats5m"]
        buys = _as_int(s.get("numBuys"))
        sells = _as_int(s.get("numSells"))
        traders = _as_int(s.get("numTraders"))
        move = s.get("priceChange")
        if None not in (buys, sells) and isinstance(move, (int, float)) and not isinstance(
            move, bool
        ):
            # jupiter has traders, not buyers/sellers — say so clearly
            traders_s = f"{traders:,} traders" if traders is not None else "traders n/a"
            return (
                f"{buys:,} buys, {sells:,} sells, {traders_s}, "
                f"price {_signed(float(move))}%"
            )
    return "unavailable"


def _deployer(jup: Any) -> str:
    if not isinstance(jup, dict):
        return "unavailable"
    audit = jup.get("audit")
    if not isinstance(audit, dict):
        return "unavailable"
    launched = _as_int(audit.get("devMints"))
    graduated = _as_int(audit.get("devMigrations"))
    if launched is None or graduated is None:
        return "unavailable"
    return f"{launched} launched, {graduated} graduated"


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
