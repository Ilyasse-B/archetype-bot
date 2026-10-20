"""
dexscreener read for the coin in focus.

the agent only sees dex paid and active boosts.
links, image, and banner are stored on the coin, not shown.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("archetype.tools.dex")

_UA = "Mozilla/5.0 (compatible; ArchetypeBot/0.1; +https://localhost)"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

_ORDERS = "https://api.dexscreener.com/orders/v1/solana/{mint}"
_PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/{mint}"

# dex type -> the slot we already keep from the metadata uri
_SLOT = {
    "twitter": "twitter",
    "telegram": "telegram",
    "website": "website",
}


def fetch_dex(mint: str, timeout: float = 15.0) -> dict[str, Any]:
    """
    orders plus the pumpswap pair.

    dex_paid is true when an approved token profile is in the orders.
    active_boosts is the live count, not the purchase history.
    a missed call leaves that field None. no second source.
    """
    mint = (mint or "").strip()
    empty: dict[str, Any] = {
        "dex_paid": None,
        "active_boosts": None,
        "links": [],
        "image_url": None,
        "header_url": None,
    }
    if not mint:
        return empty
    try:
        orders, pairs = _both(mint, timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("dex fetch failed %s: %s", mint, exc)
        return empty
    paid = _paid(orders)
    pair = _main_pair(pairs)
    return {
        "dex_paid": paid,
        "active_boosts": _active(pairs),
        "links": _links(pair),
        "image_url": _info_str(pair, "imageUrl"),
        "header_url": _info_str(pair, "header"),
    }


def dex_text(read: dict[str, Any], explain: bool = False) -> str:
    """two lines. notes only the first time the agent asks."""
    paid = read.get("dex_paid")
    boosts = read.get("active_boosts")
    if paid is None and boosts is None:
        return "dex: unavailable"
    paid_s = "unavailable" if paid is None else ("true" if paid else "false")
    boost_s = "unavailable" if not isinstance(boosts, int) else str(boosts)
    lines = [
        "dex:",
        _said("dex paid", paid_s, "true when this coin paid for a dexscreener profile", explain),
        _said("active boosts", boost_s, "how many dexscreener boosts are active right now", explain),
    ]
    return "\n".join(lines)


def merge_dex_links(coin: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    """
    add dex urls that are not already on the coin.

    same link with a trailing slash or twitter.com vs x.com counts as one.
    image and banner are stored and never shown to the agent.
    """
    out = dict(coin or {})
    seen = {_norm(out.get(key)) for key in ("twitter", "telegram", "website")}
    extras = [row for row in (out.get("extra_links") or []) if isinstance(row, dict)]
    for row in extras:
        seen.add(_norm(row.get("url")))
    seen.discard("")
    for link in read.get("links") or []:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        kind = str(link.get("type") or "").strip().lower()
        key = _norm(url)
        if not url or not key or key in seen:
            continue
        seen.add(key)
        slot = _SLOT.get(kind)
        if slot and not _norm(out.get(slot)):
            out[slot] = url
            continue
        extras.append({"type": kind or "link", "url": url})
    if extras:
        out["extra_links"] = extras
    image = read.get("image_url")
    header = read.get("header_url")
    if isinstance(image, str) and image.strip():
        out["imageUrl"] = image.strip()
    if isinstance(header, str) and header.strip():
        out["header"] = header.strip()
    return out


def _both(mint: str, timeout: float) -> tuple[Any, Any]:
    """orders and pairs. neither needs the other."""
    urls = (_ORDERS.format(mint=mint), _PAIRS.format(mint=mint))
    with ThreadPoolExecutor(max_workers=2) as pool:
        orders, pairs = pool.map(lambda url: _get_json(url, timeout), urls)
    return orders, pairs


def _get_json(url: str, timeout: float) -> Any:
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("dex get failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("dex get %s -> %s", url, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def _paid(body: Any) -> bool | None:
    """true when an approved token profile is in the orders. a miss is None."""
    if body is None:
        return None
    rows = body.get("orders") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or "")
        status = str(row.get("status") or "")
        if kind == "tokenProfile" and status == "approved":
            return True
    return False


def _active(body: Any) -> int | None:
    """live boost count. copied onto each pool, so the biggest one is enough."""
    if body is None:
        return None
    rows = body if isinstance(body, list) else []
    if isinstance(body, dict):
        rows = body.get("pairs") or []
    if not isinstance(rows, list):
        return None
    best = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        boosts = row.get("boosts")
        if not isinstance(boosts, dict):
            continue
        amount = boosts.get("active")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            continue
        best = max(best, int(amount))
    return best


def _main_pair(body: Any) -> dict[str, Any] | None:
    """pumpswap pool if there is one. otherwise the first pair that has info."""
    rows = body if isinstance(body, list) else []
    if isinstance(body, dict):
        rows = body.get("pairs") or []
    if not isinstance(rows, list):
        return None
    pairs = [row for row in rows if isinstance(row, dict)]
    for row in pairs:
        if row.get("dexId") == "pumpswap":
            return row
    for row in pairs:
        if isinstance(row.get("info"), dict):
            return row
    return pairs[0] if pairs else None


def _links(pair: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(pair, dict):
        return []
    info = pair.get("info")
    if not isinstance(info, dict):
        return []
    out: list[dict[str, str]] = []
    for row in info.get("socials") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        out.append({"type": _kind(row), "url": url})
    for row in info.get("websites") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        out.append({"type": "website", "url": url})
    return out


def _kind(row: dict[str, Any]) -> str:
    raw = str(row.get("type") or row.get("label") or "").strip().lower()
    if raw in ("x", "twitter"):
        return "twitter"
    if raw in ("tg", "telegram"):
        return "telegram"
    if raw in ("web", "website", "site"):
        return "website"
    return raw or "link"


def _info_str(pair: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(pair, dict):
        return None
    info = pair.get("info")
    if not isinstance(info, dict):
        return None
    val = info.get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _norm(url: Any) -> str:
    """same link, different spelling. twitter.com and x.com match."""
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "twitter.com":
        host = "x.com"
    path = parts.path.rstrip("/").lower()
    return urlunsplit(("https", host, path, "", ""))


def _said(label: str, value: str, note: str, explain: bool) -> str:
    """number on its own, or number plus a short note the first time."""
    if not explain:
        return f"{label}: {value}"
    return f"{label}: {value} ({note})"
