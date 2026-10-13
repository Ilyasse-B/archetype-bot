"""
safety read for the coin in focus.

rugcheck score and insider networks, streamflow locks, solana tracker
snipers / insiders / bundlers / fees. one block. no helius.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("archetype.tools.safety")

_UA = "Mozilla/5.0 (compatible; ArchetypeBot/0.1; +https://localhost)"
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
}

_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
_LOCKS = "https://api-public.streamflow.finance/v2/api/locks/solana/{mint}/active/"
_VESTING = "https://api-public.streamflow.finance/v2/api/vesting/solana/{mint}/active/"
_TRACKER = "https://data.solanatracker.io/tokens/{mint}"
_BUNDLERS = "https://data.solanatracker.io/tokens/{mint}/bundlers"

# first payload only. later calls are just the numbers.
_SAFETY_NOTE = (
    "weigh this with the rest of what you know. "
    "only reject or sell on this alone if it clearly looks dangerous."
)


def fetch_safety(mint: str, api_key: str = "", timeout: float = 15.0, explain: bool = False) -> str:
    """
    tight safety block. each call hits the apis again.

    a miss on one source leaves that line unavailable. no fallback.
    explain sticks a short note on each field. only the first read wants that.
    """
    mint = (mint or "").strip()
    if not mint:
        return "safety: unavailable"
    try:
        report, locks, vesting, token = _first_wave(mint, api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("safety fetch failed %s: %s", mint, exc)
        return "safety: unavailable"

    supply = _supply(report)
    lines = [
        "safety:",
        _said("score", _score(report), "rugcheck score. lower is safer", explain),
        _said("risks", _risks(report), "warnings rugcheck found. none means nothing flagged", explain),
        _said(
            "insider_networks",
            _networks(report, supply),
            "groups of wallets that look like the same person",
            explain,
        ),
        _said(
            "locked",
            _locked(locks, vesting, supply),
            "supply that still cannot be sold, and how many locks. a past unlock is not counted",
            explain,
        ),
    ]
    lines.extend(_tracker_lines(mint, token, api_key, timeout, explain))
    if explain:
        lines.append(_SAFETY_NOTE)
    return "\n".join(lines)


def _said(label: str, value: str, note: str, explain: bool) -> str:
    """number on its own, or number plus a short note the first time."""
    if not explain:
        return f"{label}: {value}"
    return f"{label}: {value} ({note})"


def _first_wave(mint: str, api_key: str, timeout: float) -> tuple[Any, Any, Any, Any]:
    """rugcheck, both streamflow lists, and the tracker token. independent calls."""
    tracker_headers = dict(_HEADERS)
    if api_key.strip():
        tracker_headers["x-api-key"] = api_key.strip()

    def tracker() -> Any:
        if not api_key.strip():
            return None
        return _get_json(_TRACKER.format(mint=mint), timeout, tracker_headers)

    jobs = (
        lambda: _get_json(_RUGCHECK.format(mint=mint), timeout),
        lambda: _get_json(_LOCKS.format(mint=mint), timeout),
        lambda: _get_json(_VESTING.format(mint=mint), timeout),
        tracker,
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        report, locks, vesting, token = (fut.result() for fut in (pool.submit(job) for job in jobs))
    return report, locks, vesting, token


def _get_json(url: str, timeout: float, headers: dict[str, str] | None = None) -> Any:
    """one get. junk is None. a list is fine (streamflow)."""
    try:
        resp = httpx.get(
            url,
            headers=headers or _HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("safety get failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("safety get %s -> %s", url, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def _supply(report: Any) -> float | None:
    """rugcheck token.supply is raw. same units as streamflow amounts."""
    if not isinstance(report, dict):
        return None
    token = report.get("token")
    if not isinstance(token, dict):
        return None
    raw = token.get("supply")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return float(raw)


def _score(report: Any) -> str:
    if not isinstance(report, dict):
        return "unavailable"
    score = report.get("score_normalised")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return "unavailable"
    return _num(float(score))


def _risks(report: Any) -> str:
    if not isinstance(report, dict):
        return "unavailable"
    risks = report.get("risks")
    if not isinstance(risks, list):
        return "unavailable"
    bits: list[str] = []
    for item in risks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        level = str(item.get("level") or "").strip()
        if not name:
            continue
        bits.append(f"{name} ({level})" if level else name)
    if not bits:
        return "none"
    return "; ".join(bits)


def _networks(report: Any, supply: float | None) -> str:
    if not isinstance(report, dict):
        return "unavailable"
    nets = report.get("insiderNetworks")
    if not isinstance(nets, list):
        return "unavailable"
    groups = 0
    active = 0
    holding = 0.0
    for net in nets:
        if not isinstance(net, dict):
            continue
        groups += 1
        active += _as_int(net.get("activeAccounts")) or 0
        held = _as_float(net.get("currentHolding"))
        if held is not None:
            holding += held
    net_word = "network" if groups == 1 else "networks"
    wallet_word = "wallet" if active == 1 else "wallets"
    line = f"{groups} {net_word} with {active} active {wallet_word}"
    if not supply:
        return line
    return f"{line} holding {_num(holding / supply * 100)}% supply"


def _locked(locks: Any, vesting: Any, supply: float | None) -> str:
    rows = _rows(locks) + _rows(vesting)
    # both calls missed. dont pretend the coin has zero locks.
    if locks is None and vesting is None:
        return "unavailable"
    now = datetime.now(timezone.utc)
    still = 0.0
    count = 0
    for row in rows:
        amount = _active_amount(row, now)
        if amount is None:
            continue
        still += amount
        count += 1
    if count == 0:
        return "0% across 0"
    if not supply:
        return "unavailable"
    return f"{_num(still / supply * 100)}% across {count}"


def _rows(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        data = body.get("data") or body.get("items") or body.get("locks")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    return []


def _active_amount(row: dict[str, Any], now: datetime) -> float | None:
    """raw tokens still locked. a finished unlock does not count."""
    if _is_past(row.get("endDt"), now) or _is_past(row.get("expiryDt"), now):
        return None
    amount = _as_float(row.get("amount"))
    withdrawn = _as_float(row.get("withdrawnAmount")) or 0.0
    if amount is None:
        return None
    still = amount - withdrawn
    if still <= 0:
        return None
    return still


def _is_past(value: Any, now: datetime) -> bool:
    """true when this timestamp has already passed. year 9999 is a placeholder."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when.year >= 9000:
        return False
    return when <= now


def _tracker_lines(
    mint: str, token: Any, api_key: str, timeout: float, explain: bool
) -> list[str]:
    if not api_key.strip() or not isinstance(token, dict):
        return [
            _said("snipers", "unavailable", "sniper wallets that still hold supply", explain),
            _said("insiders", "unavailable", "insider wallets and the supply they still hold", explain),
            _said("bundlers", "unavailable", "wallets that bought in a bundle, and the supply they still hold", explain),
            _said("fees_sol", "unavailable", "total trading fees paid on this coin, in SOL", explain),
        ]
    risk = token.get("risk") if isinstance(token.get("risk"), dict) else {}
    lines: list[str] = []
    snipers = _group_line(risk.get("snipers"))
    # sold-out snipers are noise. only mention them when they still hold.
    if snipers is not None:
        lines.append(_said("snipers", snipers, "sniper wallets that still hold supply", explain))
    elif not isinstance(risk.get("snipers"), dict):
        lines.append(_said("snipers", "unavailable", "sniper wallets that still hold supply", explain))
    insiders = _group_line(risk.get("insiders"), keep_zero=True)
    lines.append(
        _said(
            "insiders",
            insiders if insiders is not None else "unavailable",
            "insider wallets and the supply they still hold",
            explain,
        )
    )
    bundlers = _bundlers_line(mint, risk.get("bundlers"), api_key, timeout)
    lines.append(
        _said(
            "bundlers",
            bundlers,
            "wallets that bought in a bundle, and the supply they still hold",
            explain,
        )
    )
    lines.append(
        _said("fees_sol", _fees(risk.get("fees")), "total trading fees paid on this coin, in SOL", explain)
    )
    return lines


def _group_line(group: Any, keep_zero: bool = False) -> str | None:
    """count and holding %. None means skip the line (snipers who sold out)."""
    if not isinstance(group, dict):
        return None
    count = _as_int(group.get("count"))
    pct = _as_float(group.get("totalPercentage"))
    if count is None or pct is None:
        return None
    if not keep_zero and (count <= 0 or pct <= 0):
        return None
    return f"{count} holding {_num(pct)}%"


def _bundlers_line(mint: str, group: Any, api_key: str, timeout: float) -> str:
    if isinstance(group, dict):
        count = _as_int(group.get("count"))
        pct = _as_float(group.get("totalPercentage"))
        if count is not None and pct is not None:
            return f"{count} holding {_num(pct)}%"
    # token payload can omit bundlers. one extra call, totals only.
    body = _get_json(
        _BUNDLERS.format(mint=mint),
        timeout,
        {**_HEADERS, "x-api-key": api_key.strip()},
    )
    if not isinstance(body, dict):
        return "unavailable"
    total = _as_int(body.get("total"))
    pct = _as_float(body.get("percentage"))
    if total is None or pct is None:
        return "unavailable"
    return f"{total} holding {_num(pct)}%"


def _fees(fees: Any) -> str:
    if not isinstance(fees, dict):
        return "unavailable"
    total = _as_float(fees.get("total"))
    if total is None:
        return "unavailable"
    return _num(total)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _num(value: float) -> str:
    text = f"{value:.2f}" if abs(value) < 1 else f"{value:.1f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
